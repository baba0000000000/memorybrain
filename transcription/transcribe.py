"""Transcription avec timestamps — Phase 1 de la V2.

C'est la pièce qui manquait à la V1 : elle écrivait du texte sans
horodatage, donc aucun souvenir ne pouvait être replacé dans le temps.

Le calcul est fait par le Compute Hub (Celery/Redis sur les workers
disponibles), donc 100 % local, aucun coût cloud. Si le hub est mort, on
ne devine rien : le chunk reste `pending` et on passe au suivant
(règle 3).

Positions (§2, règle 2) :
  les segments du hub sont RELATIFS au chunk qu'il a transcrit
  (il ne sait pas d'où vient le fichier). On y ajoute donc le
  start_time du chunk pour obtenir la position ABSOLUE dans la source.
  Sans ça, tous les chunks se chevaucheraient sur 0-60 s.
"""
import ast
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import hub

PIPELINE_VERSION = "2.0.0"
# Un seul emplacement pour le Hub : celui de hub.py.
HUB_DIR = hub.HUB_DIR
HUB_PY = HUB_DIR / "venv" / "bin" / "python"
HUB_SUBMIT = HUB_DIR / "submit.py"

# Worker qui voit les fichiers du corpus. Celery répartit par défaut sur
# tous les workers connectés ; un worker qui ne partage pas la racine du
# projet ne peut pas lire un chemin local. Surchargeable par
# MEMORYBRAIN_WORKER, neutre par défaut (la machine locale).
WORKER_CIBLE = os.environ.get("MEMORYBRAIN_WORKER", "localhost")

# Le worker résout 'nom-partage/fichier' via AIINFRA_SHARED_ROOTS, qui ne
# connaît qu'UN dossier par nom de partage. Or le corpus vit dans TROIS
# dossiers. Un seul nom pour les trois est impossible : le fichier serait
# cherché au mauvais endroit.
#
# Ils doivent rester IDENTIQUES des deux côtés, sinon le worker ne résout
# rien et renvoie « fichier introuvable ».
#
# On ne déplace pas les fichiers du corpus (§17) : chaque dossier a son nom.
HUB_ROOTS = {
    "memorybrain-wav": [config.V1_AUDIO],
    "memorybrain-done": [config.V1_DONE],
    "memorybrain-inbox": [config.SYNC_INBOX],
}

TIMEOUT = 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mark_step(con: sqlite3.Connection, chunk_id: int, step: str, status: str,
              model: str | None = None, error: str | None = None) -> None:
    con.execute(
        "INSERT INTO pipeline_step(chunk_id, step, status, model, model_version,"
        " pipeline_version, started_at, finished_at, error)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(chunk_id, step) DO UPDATE SET"
        "   status=excluded.status, model=excluded.model,"
        "   model_version=excluded.model_version, error=excluded.error,"
        "   finished_at=excluded.finished_at",
        (chunk_id, step, status, model, None, PIPELINE_VERSION,
         now_iso(), now_iso(), error))


def _hub_call(audio: Path, model: str) -> dict | None:
    """Appelle le Compute Hub. None si indisponible ou si la réponse est
    illisible — jamais d'exception (règle 3).

    WORKER CIBLÉ = celui qui voit les fichiers. Celery répartit par défaut
    sur tous les workers connectés, or le worker qui n'a pas la racine du
    projet ne voit pas le fichier : le job y part, y reste « active », et
    la chaîne ne produit plus rien alors que tout paraît normal.

    On passe donc --worker <WORKER_CIBLE> (MEMORYBRAIN_WORKER), qui met en
    pause la file des autres workers le temps du lot.

    On tente chaque racine partagée correspondant au dossier réel du
    fichier, puis EN DERNIER un chemin absolu.

    L'ordre compte : le chemin absolu ne marche que si le worker qui reçoit
    le job est sur CETTE machine. Les noms de partage sont résolus par le
    worker lui-même et fonctionnent quelle que soit la machine.
    """
    if not HUB_PY.exists() or not HUB_SUBMIT.exists():
        return None

    cibles: list[str] = []
    for partage, racines in HUB_ROOTS.items():
        if any((r / audio.name).exists() for r in racines):
            cibles.append(f"{partage}/{audio.name}")
    cibles.append(str(audio))

    for cible in cibles:
        try:
            r = subprocess.run(
                [str(HUB_PY), str(HUB_SUBMIT), "--model", model,
                 "--worker", WORKER_CIBLE, cible],
                capture_output=True, text=True, timeout=TIMEOUT)
        except Exception:
            return None
        for line in r.stdout.splitlines():
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    out = ast.literal_eval(s)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(out, dict) and not out.get("error"):
                    return out
    return None


def _chercher_meme_nom(nom: str) -> Path | None:
    """Retrouve un audio déplacé par Syncthing, par son NOM.

    Le nom encode l'horodatage : il reste valable même si le fichier a
    changé de dossier. On ne déplace rien, on ne copie rien — on corrige
    seulement le pointeur en base (règle 17 : les audios ne bougent pas).

    Le nom doit correspondre EXACTEMENT. Un préfixe partiel donnerait
    le mauvais fichier, donc pas de glob ici.
    """
    for racines in HUB_ROOTS.values():
        for r in racines:
            candidat = r / nom
            if candidat.exists():
                return candidat
    return None


def transcribe_chunk(con: sqlite3.Connection, chunk_id: int,
                     model: str = config.MODELE_ASR) -> dict:
    """Transcrit UN chunk et remplit segment + pipeline_step.

    AVANT de soumettre, on vérifie que l'audio est toujours là. Syncthing
    fait bouger les fichiers : un chunk ingéré depuis syncthing-inbox peut
    avoir été déplacé dans pipeline/done depuis. Le pointeur en base
    devient alors faux et la transcription échoue sur « audio absent ».

    Symptôme observé : une partie des chunks pointait vers un dossier
    où le fichier n'était plus. Le contenu est intact, seul le chemin a
    changé.

    On ne déplace AUCUN audio. On cherche le même NOM dans les racines
    connues et on corrige le pointeur. Le nom encode l'horodatage, il
    reste le même quelle que soit sa position.

    Renvoie un dict de statut. Ne lève jamais.
    """
    row = con.execute("SELECT * FROM chunk WHERE chunk_id=?", (chunk_id,)).fetchone()
    if row is None:
        return {"statut": "absent"}

    audio = Path(row["original_file"])
    if not audio.exists():
        retrouve = _chercher_meme_nom(audio.name)
        if retrouve is not None:
            con.execute("UPDATE chunk SET original_file=? WHERE chunk_id=?",
                        (str(retrouve), chunk_id))
            con.commit()
            audio = retrouve
        else:
            mark_step(con, chunk_id, "transcription", "failed", model,
                      f"audio absent : {audio}")
            con.commit()
            return {"statut": "audio_absent"}

    deja = con.execute(
        "SELECT status, model FROM pipeline_step WHERE chunk_id=? AND step='transcription'",
        (chunk_id,)).fetchone()
    if deja and deja["status"] == "done" and deja["model"] == model:
        return {"statut": "déjà_fait", "chunk_id": chunk_id}

    mark_step(con, chunk_id, "transcription", "pending", model)
    con.commit()

    out = _hub_call(audio, model)
    if out is None:
        mark_step(con, chunk_id, "transcription", "failed", model,
                  "Compute Hub indisponible ou réponse illisible")
        con.commit()
        return {"statut": "hub_indisponible"}

    segs = out.get("segments") or []
    texte = ""
    # un chemin ABSENT ne doit pas devenir Path("") = le dossier courant :
    # lire ça lèverait IsADirectoryError (règle 3)
    txt = out.get("output")
    txt_path = Path(txt) if txt else None
    if txt_path and txt_path.is_file():
        texte = txt_path.read_text(encoding="utf-8", errors="replace").strip()
    else:
        # pas de .txt : on reconstruit le texte depuis les segments, c'est
        # la même chose et ça évite de dépendre d'un fichier temporaire
        texte = " ".join((s.get("text") or "").strip() for s in segs).strip()

    # Un chunk SILENCIEUX est normal (silence, bruit de fond) : on
    # le marque terminé sans segments. Mais un texte non vide SANS
    # segments, c'est un résultat inutilisable → on refuse (règle 3
    # ne veut pas dire « accepter un résultat faux »).
    if not segs and texte:
        mark_step(con, chunk_id, "transcription", "failed", model,
                  f"texte de {len(texte)} caractères mais 0 segment horodaté")
        con.commit()
        return {"statut": "pas_de_segments"}

    base = float(row["start_time"] or 0.0)
    con.execute("DELETE FROM segment WHERE chunk_id=?", (chunk_id,))
    for i, s in enumerate(segs):
        # ABSOLU : on ajoute la position du chunk (règle 2)
        start = round(base + float(s.get("start", 0.0)), 3)
        end = round(base + float(s.get("end", 0.0)), 3)
        con.execute(
            "INSERT INTO segment(chunk_id, idx, start_time, end_time, text)"
            " VALUES(?,?,?,?,?)",
            (chunk_id, i, start, end, (s.get("text") or "").strip()))

    # le .md lisible, une ligne par segment avec son heure (§22 transcripts/)
    md = config.TRANSCRIPTS / f"{audio.stem}.md"
    try:
        config.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
        lignes = [
            "---",
            f"chunk_id: {chunk_id}",
            f"source: {audio.name}",
            f"recorded_at: {row['recorded_at']}",
            f"start_time: {row['start_time']}",
            f"model: {out.get('model', model)}",
            f"worker: {out.get('worker', '?')}",
            f"pipeline: {PIPELINE_VERSION}",
            "---",
            "",
            f"# {row['recorded_at']}",
            "",
        ]
        for s in segs:
            start = base + float(s.get("start", 0.0))
            lignes.append(f"**[{_hhmmss(start)}]** {s.get('text', '').strip()}")
        if not segs:
            lignes.append("*(silence — aucune parole détectée)*")
        md.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    except OSError:
        md = None

    con.execute(
        "INSERT INTO artifact(chunk_id, kind, path, model, pipeline_version, created_at)"
        " VALUES(?,'transcript',?,?,?,?)"
        " ON CONFLICT(chunk_id, kind) DO UPDATE SET path=excluded.path,"
        " model=excluded.model, pipeline_version=excluded.pipeline_version",
        (chunk_id, (str(md.relative_to(config.V2_DIR)) if md else None),
         out.get("model", model), PIPELINE_VERSION, now_iso()))

    # ⚠ le dédoublonnage (plus haut) compare le modèle DEMANDÉ : la
    # marque « fait » doit donc stocker ce même modèle, sinon le test
    # ne reconnaît jamais son propre travail et retranscrit à chaque
    # passage (bug silencieux constaté). Le modèle
    # réellement renvoyé par le hub reste dans `artifact.model`.
    mark_step(con, chunk_id, "transcription", "done", model)
    con.execute("UPDATE chunk SET status='transcribed' WHERE chunk_id=?", (chunk_id,))
    con.commit()
    return {"statut": "ok", "chunk_id": chunk_id, "segments": len(segs),
            "chars": len(texte), "worker": out.get("worker"),
            "elapsed": out.get("elapsed"), "md": str(md) if md else None}


def _hhmmss(sec: float) -> str:
    """Heure locale d'un timestamp ABSOLU : 'AAAA-MM-JJ HH:MM:SS'.

    sec est un epoch absolu, pas une durée depuis le début du chunk
    (les positions sont absolues, règle 2). D'où le fromtimestamp.
    Le jour est nécessaire : sans lui, un enregistrement qui franchit
    minuit affiche 00:00:00 et la date de début est perdue.
    """
    return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")


def transcribe_pending(con: sqlite3.Connection, limit: int = 10,
                       model: str = config.MODELE_ASR, progress=None,
                       court_d_abord: bool = True) -> dict:
    """Transcrit N chunks non transcrits.

    `court_d_abord=True` traite d'abord les chunks les plus courts. Ce n'est
    pas un détail : sans ce tri, le premier lot tomberait sur les chunks
    longs et la file n'atteindrait les fichiers courts qu'après un temps
    prohibitif. Les chunks courts étant les plus nombreux, ils valident la
    chaîne plus vite.

    L'ordre reste déterministe et l'historique reste lisible : une fois le
    lot court terminé, un passage en ordre chronologique (`court_d_abord=False`)
    reprend où il s'est arrêté. Aucun chunk n'est perdu ni traité deux fois.

    Le tri par durée ne change QUE l'ordre de traitement, jamais les
    données : chaque chunk garde ses propres timestamps.
    """
    ordre = "c.duration_sec, c.recorded_at" if court_d_abord else "c.recorded_at, c.chunk_index"
    chunks = con.execute(
        "SELECT c.chunk_id FROM chunk c"
        " LEFT JOIN pipeline_step p"
        "   ON p.chunk_id = c.chunk_id AND p.step = 'transcription'"
        " WHERE (p.status IS NULL OR p.status != 'done')"
        f" ORDER BY {ordre} LIMIT ?", (limit,)).fetchall()

    compte = {"ok": 0, "déjà_fait": 0, "hub_indisponible": 0, "audio_absent": 0,
              "pas_de_segments": 0, "absent": 0}
    for i, r in enumerate(chunks, 1):
        res = transcribe_chunk(con, r["chunk_id"], model)
        compte[res["statut"]] = compte.get(res["statut"], 0) + 1
        if progress:
            progress(i, len(chunks), res)
    return compte