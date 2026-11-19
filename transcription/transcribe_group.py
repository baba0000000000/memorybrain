"""ÉTAPE 2 — TRANSCRIPTION D'UN GROUPE (Whisper V3 large fast).

On transcribe le WAV GROUPÉ (5 fichiers d'un coup), pas chaque chunk
séparément : le RTF diminue quand on transcrit un groupe plutôt qu'un
chunk isolé. Le texte gagne aussi les phrases qui étaient coupées entre
deux chunks (règle 7).

choisi pour son rapport vitesse/qualité (« Whisper V3 large fast »)
Nemotron laissé dans les anciens fichiers : le Nvidia est plus lent ET moins
bon sur ce corpus. Le nom du modèle est dans config.py, jamais ici.

Deux modèles de coût, parce que plusieurs cœurs c'est court :
  - CPU int8 : pas de GPU nécessaire, toujours disponible ;
  - Vulkan fp16 : plus rapide quand la machine s'y prête, mais exige une
    pile compatible et dépanne plus souvent (voir les notes dans config).
La fonction choisit au lancement et journalise ce qu'elle a utilisé.

Règle 18 : le modèle et sa version sont écrits en base à chaque exécution.
Changer de modèle plus tard n'invalide que la transcription, pas le reste.
"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import hub

# whisper charge le modèle en mémoire ; on ne le recharge pas pour chaque
# groupe. Un seul modèle par process, pour ne pas le recharger inutilement.
_MODELE = None
_BACKEND = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version_modele() -> str:
    """Version du modèle, pour la règle 18. Jamais devinée (règle 3)."""
    try:
        from huggingface_hub import model_info
        return model_info(config.MODELE_ASR).sha[:12]
    except Exception:
        return "inconnue"


def choisir_backend() -> str:
    """CPU int8 par défaut. Vulkan si le matériel le permet vraiment."""
    for backend in ("vulkan", "cpu"):
        try:
            import ctranslate2
            types = ctranslate2.get_supported_compute_types(backend)
            if types:
                return backend
        except Exception:
            continue
    return "cpu"


def charger(backend: str | None = None) -> tuple[object, str]:
    """Charge le modèle UNE fois et le garde pour tout le process."""
    global _MODELE, _BACKEND
    if _MODELE is not None:
        return _MODELE, _BACKEND

    from faster_whisper import WhisperModel

    backend = backend or choisir_backend()
    compute = "int8" if backend == "cpu" else "float16"
    _MODELE = WhisperModel(config.MODELE_ASR, device=backend, compute_type=compute)
    _BACKEND = backend
    return _MODELE, backend


# --- Dispatch vers le Compute Hub (workers distants) ----------------------
# Toute la mécanique (sonde, envoi, repli) vit dans hub.py, pour que la
# transcription et la diarisation se comportent EXACTEMENT pareil : c'est
# leur divergence qui avait bloqué la V2.
def _hub_actif() -> bool:
    """Y a-t-il un worker vivant ? Sinon on ne soumet rien du tout."""
    return hub.actif()


def _hub_transcrire(audio: str, langue, beam_size: int) -> dict | None:
    """Confie la transcription au Hub. None si le Hub n'a rien rendu.

    On envoie un NOM DE PARTAGE, jamais un chemin absolu : c'est le worker
    qui le résout chez lui. Le job marche donc quelle que soit la machine
    qui l'exécute — un chemin absolu ne marcherait que sur cette machine.
    """
    out = hub.soumettre(
        "transcribe",
        args=[f"{hub.PARTAGE}/{Path(audio).name}"],
        kwargs={
            "model": config.MODELE_ASR,
            "language": langue,
            # Mêmes réglages qu'en local : sinon un même audio donne un
            # texte différent selon la machine, et comparer les deux n'a
            # plus de sens.
            "options": {
                "vad_filter": True,
                "no_speech_threshold": 0.6,
                "condition_on_previous_text": False,
                "beam_size": beam_size,
            },
        })
    if out is None:
        return None
    segs = []
    for s in out.get("segments") or []:
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        segs.append({"start": round(float(s.get("start", 0.0)), 3),
                     "end": round(float(s.get("end", 0.0)), 3),
                     "text": txt, "mots": None})
    return {"segments": segs, "worker": out.get("worker"),
            "device": out.get("device"), "duree": out.get("audio_duration"),
            "elapsed": out.get("elapsed")}


def transcrire(audio: str, langue: str | None = "fr",
               backend: str | None = None,
               beam_size: int = 5) -> dict:
    """Transcrit un fichier audio. Ne lève jamais (règle 3).

    Renvoie :
      segments : [{start, end, text, mots}], en SECONDES RELATIVES au fichier
      langue   : langue détectée
      duree    : durée du fichier
      backend  : 'cpu' ou 'vulkan'
      rtf      : temps de calcul / durée audio — la vraie mesure de coût
    """
    t0 = time.time()

    # 1) Le Hub d'abord, s'il a un worker vivant.
    #    Un `backend` forcé veut dire « local » : on ne détourne pas.
    if backend is None and _hub_actif():
        h = _hub_transcrire(audio, langue, beam_size)
        # ⚠ Un Hub qui répond VITE et VIDE n'a pas entendu du silence : il a
        # échoué autrement (fichier introuvable de son côté, modèle non
        # chargé…). Un groupe rendu « 0 segment » en une fraction de seconde,
        # alors que le même WAV transcrit en local donne de vraies phrases,
        # est un échec, pas du silence. On ne conclut donc au silence
        # qu'après un essai LOCAL.
        if h is not None and (h["segments"] or not h.get("elapsed")
                              or float(h.get("elapsed") or 0) > 5):
            duree = float(h.get("duree") or 0.0)
            el = h.get("elapsed")
            return {
                "segments": h["segments"],
                "langue": langue,
                "duree": duree,
                "backend": f"hub:{h.get('worker', '?')}",
                "elapsed": el,
                "rtf": round(float(el) / duree, 3) if el and duree else None,
                "modele": config.MODELE_ASR_NOM,
                "modele_version": version_modele(),
                "device": h.get("device"),
            }

    # 2) Sinon, en local. Ce chemin n'est jamais retiré : c'est lui qui
    #    garantit qu'une panne du Hub ne bloque pas la chaîne.
    try:
        model, backend = charger(backend)
    except Exception as e:
        return {"segments": [], "erreur": f"modèle indisponible : {e}"}

    try:
        segments, info = model.transcribe(
            audio, language=langue, beam_size=beam_size,
            # ⚠ vad_filter=False. Le VAD Silero de faster-whisper ne
            # « nettoie » pas les silences : sur des enregistrements au
            # volume faible, il jette la PAROLE et écourte les segments.
            # Sans lui, le texte est complet ; le filtre d'hallucinations et
            # la détection de musique écartent ensuite ce que Whisper invente
            # sur du bruit.
            # Le garde-fou contre les hallucinations reste
            # no_speech_threshold + transcription/hallucinations.py.
            vad_filter=False,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
        )
        out = []
        for s in segments:
            mots = None
            try:
                mots = [{"s": round(w.start, 2), "e": round(w.end, 2),
                         "t": w.word} for w in (s.words or [])]
            except Exception:
                pass
            out.append({"start": round(s.start, 3), "end": round(s.end, 3),
                        "text": (s.text or "").strip(), "mots": mots})
    except Exception as e:
        return {"segments": [], "erreur": f"transcription échouée : {e}"}

    elapsed = time.time() - t0
    duree = float(getattr(info, "duration", 0.0) or 0.0)
    return {
        "segments": out,
        "langue": getattr(info, "language", None),
        "duree": duree,
        "backend": backend,
        "elapsed": round(elapsed, 2),
        "rtf": round(elapsed / duree, 3) if duree else None,
        "modele": config.MODELE_ASR_NOM,
        "modele_version": version_modele(),
    }


def redistribuer(con: sqlite3.Connection, group_id: int, segments: list[dict]) -> dict:
    """Répartit les segments du GROUPE sur les CHUNKS d'origine.

    C'est l'étape qui fait le lien entre le traitement par 5 et la base qui
    reste en 60 s. Chaque segment du groupe est replacé à son temps ABSOLU
    (règle 2), puis rattaché au chunk qui contient ce temps.

    Les temps du groupe sont relatifs à son WAV ; on y ajoute le
    `start_time` du premier chunk (position absolue dans la session).

    Règle 7 : aux frontières, on garde le texte même s'il déborde de
    quelques dixièmes — on n'invente pas de coupure, on accepte le
    débordement et on le note.
    """
    from grouping import group as gmod

    membres = gmod.chunks_du_groupe(con, group_id)
    if not membres:
        return {"erreur": "groupe sans membres"}

    base_absolue = float(membres[0]["start_time"])
    # ⚠ Les `start_time`/`end_time` des chunks NE décrivent PAS le WAV du
    # groupe. Le WAV est la concaténation des fichiers dans l'ordre, donc la
    # position réelle d'un chunk est la SOMME des durées de ses prédécesseurs.
    # Les horodatages sautaient puis se recouvraient : tout segment au-delà
    # d'une certaine position tombait hors de toute borne et était jeté comme
    # « orphelin », et le texte des chunks suivants restait `pending` pour
    # toujours. Les bornes sont donc cumulatives, pas horodatées.
    bornes, _t = [], 0.0
    for m in membres:
        d = float(m["duration_sec"] or 0)
        if d <= 0 and m["end_time"] is not None:
            d = float(m["end_time"]) - float(m["start_time"])
        bornes.append((base_absolue + _t, base_absolue + _t + d, m["chunk_id"],
                       float(m["offset_sec"]), d))
        _t += d

    stats = {"segments": 0, "coupes": 0, "orphelins": 0, "silencieux": 0}
    buffer: dict[int, list] = {}

    for seg in segments:
        txt = seg.get("text", "").strip()
        if not txt:
            continue
        stats["segments"] += 1

        # bornes absolues de ce segment dans la source
        a = base_absolue + seg["start"]
        b = base_absolue + seg["end"]

        # on découpe le segment sur chaque frontière de chunk qu'il traverse
        morceaux = [(a, b, txt)]
        for (cs, ce, _cid, _off, _dur) in bornes:
            suivants = []
            for (x, y, t) in morceaux:
                # une frontière qui tombe à l'INTÉRIEUR du segment le coupe
                # en deux. Tolérance 1 ms : les `end_time` de la base sont
                # calculés à l'ingestion et peuvent différer de quelques
                # centièmes de la durée réelle du fichier. Sans cette
                # tolérance, chaque frontière coupait un segment en un
                # morceau de 4 ms jeté ensuite, et des segments débordaient
                # du chunk suivant de plusieurs secondes.
                # On coupe à chaque frontière située STRICTEMENT à
                # l'intérieur du morceau. L'ancien code produisait
                # `(x, min(y, cs))` + `(max(x, ce), y)` : quand le `end` du
                # chunk tombe APRÈS la fin du segment, le second morceau a une
                # durée négative et disparaît. Résultat : le découpage compte
                # plus de « coupes » que de segments réels, n'écrit AUCUN
                # segment et déclare les chunks muets.
                points = sorted({x, y} | {c for c in (cs, ce)
                                          if x + 0.001 < c < y - 0.001})
                if len(points) == 2:
                    suivants.append((x, y, t))
                else:
                    stats["coupes"] += 1
                    for p0, p1 in zip(points, points[1:]):
                        suivants.append((p0, p1, t))
            morceaux = [(x, y, t) for (x, y, t) in suivants if y - x > 0.05]

        for (x, y, t) in morceaux:
            # Le chunk qui contient le MILIEU du morceau, pas celui dont une
            # borne est proche : un morceau qui commence pile sur une
            # frontière (cas normal après découpage) était attribué au chunk
            # PRÉCÉDENT, puis rogné à zéro seconde par le bornage — donc
            # perdu, et le groupe ressortait vide.
            milieu = (x + y) / 2.0
            cible = None
            for (cs, ce, cid, _off, _dur) in bornes:
                if cs <= milieu < ce:
                    cible = cid
                    break
            if cible is None:
                # aux interstices entre deux chunks : on rattache au plus
                # proche, jamais à un chunk arbitraire (règle 3)
                ecart, proche = None, None
                for (cs, ce, cid, _o, _d) in bornes:
                    d = min(abs(x - cs), abs(x - ce))
                    if ecart is None or d < ecart:
                        ecart, proche = d, cid
                cible = proche
                stats["orphelins"] += 1

            # On borne le morceau aux limites EXACTES du chunk choisi.
            # `end_time` en base est start_time + durée lue à l'ingestion ;
            # il peut différer de quelques centièmes de la durée réelle du
            # fichier concaténé (quelques dixièmes de seconde sur un chunk
            # long).
            # Sans ce bornage, le dernier segment d'un chunk débordait sur
            # le suivant de 3 à 25 s et le contrôle de cohérence le
            # signalait. Un segment qui déborde est pire qu'un segment
            # tronqué : il ment sur la position de la parole.
            for (cs, ce, cid, _o, _d) in bornes:
                if cid == cible:
                    y = min(y, ce)
                    x = max(x, cs)
                    break

            if y - x > 0.05:
                buffer.setdefault(cible, []).append((x, y, t))

    # Nettoyage AVANT réinsertion, et pour TOUS les chunks du groupe — pas
    # seulement ceux qui reçoivent du texte. Sinon un chunk dont l'ancien
    # texte n'est plus produit (hallucination filtrée, silence) gardait ses
    # vieux segments, mêlés aux nouveaux.
    for (_cs, _ce, cid, _o, _d) in bornes:
        con.execute("DELETE FROM segment WHERE chunk_id=?", (cid,))

    ecrits = 0
    for chunk_id, items in buffer.items():
        items.sort(key=lambda z: z[0])
        # Règle 19 : on repart d'une transcription propre de ce chunk. Le
        # DELETE évite les doublons quand on rejoue un groupe avec un autre
        # modèle — sans lui, chaque rejeu ajouterait N segments de plus.
        con.execute("DELETE FROM segment WHERE chunk_id=?", (chunk_id,))
        for idx, (x, y, t) in enumerate(items):
            con.execute(
                "INSERT INTO segment(chunk_id, idx, start_time, end_time, text)"
                " VALUES(?,?,?,?,?)", (chunk_id, idx, round(x, 3), round(y, 3), t))
            ecrits += 1
        con.execute("UPDATE chunk SET status='transcribed' WHERE chunk_id=?",
                    (chunk_id,))
        con.execute(
            "INSERT OR REPLACE INTO pipeline_step(chunk_id, step, status, model,"
            " model_version, pipeline_version, started_at, finished_at)"
            " VALUES(?,'transcription','done',?,?,?,?,?)",
            (chunk_id, config.MODELE_ASR_NOM, version_modele(), "2.0.0",
             now_iso(), now_iso()))

    # Un chunk du groupe qui n'a reçu AUCUN texte n'est pas « en attente » :
    # Whisper a lu tout le WAV et n'y a trouvé aucune parole (silence, bruit,
    # musique sans voix). Le laisser `pending` faisait croire à des centaines
    # de chunks jamais traités, et relançait la transcription
    # du groupe indéfiniment.
    sans_parole = [cid for (_cs, _ce, cid, _o, _d) in bornes
                   if cid not in buffer]
    for cid in sans_parole:
        con.execute(
            "UPDATE chunk SET status='sans_parole',"
            " note=COALESCE(note,'') || ' | aucune parole dans le WAV'"
            " WHERE chunk_id=? AND status='pending'", (cid,))
        con.execute(
            "INSERT OR REPLACE INTO pipeline_step(chunk_id, step, status,"
            " model, model_version, pipeline_version, started_at,"
            " finished_at, error) VALUES(?,'transcription','done',?,?,?,?,?"
            ",'aucune parole detectee')",
            (cid, config.MODELE_ASR_NOM, version_modele(), "2.0.0",
             now_iso(), now_iso()))
    stats["sans_parole"] = len(sans_parole)

    con.commit()
    stats["ecrits"] = ecrits
    stats["chunks"] = len(buffer)
    return stats


def transcrire_groupe(con: sqlite3.Connection, group_id: int,
                      backend: str | None = None,
                      trace: bool = True) -> dict:
    """Enchaîne : transcription du WAV groupé → redistribution → base.

    Idempotent (règle 19) : si le statut du groupe est déjà `transcrit`,
    la fonction ne refait rien.
    """
    from grouping import group as gmod

    g = con.execute("SELECT * FROM chunk_group WHERE group_id=?",
                    (group_id,)).fetchone()
    if g is None:
        return {"erreur": f"groupe {group_id} inexistant"}
    if g["statut"] in ("transcrit", "traite"):
        # ⚠ Le statut du GROUPE ne suffit pas à dire que ses chunks sont
        # transcrits : un groupe peut être marqué « transcrit » alors que son
        # texte s'arrête avant la fin du WAV, parce que le WAV a été
        # reconstruit après coup et pas le statut. Un seul chunk encore
        # `pending` = le groupe n'est pas fini.
        reste = con.execute(
            "SELECT COUNT(*) FROM group_member m JOIN chunk c"
            " ON c.chunk_id=m.chunk_id WHERE m.group_id=?"
            " AND c.status='pending'", (group_id,)).fetchone()[0]
        if reste == 0:
            return {"statut": "déjà_transcrit", "group_id": group_id}
        print(f"    groupe {group_id} : statut 'transcrit' mais {reste} chunk(s)"
              f" pending -> on refait", flush=True)

    audio = g["audio_path"]
    if not audio:
        return {"erreur": "groupe sans audio (reconstruit avec audio=False ?)"}

    # Même piège que `chunk.original_file` : `chunk_group.audio_path` a été
    # écrit sous l'ancienne racine du projet.
    # Le nom du WAV, lui, est stable — on le retrouve dans `work/groupes`
    # et on corrige la base au passage.
    p_audio = Path(audio)
    if not p_audio.is_file():
        trouve = None
        for rac in (Path(config.WORK_GROUPES),
                    Path(config.WORK_GROUPES).parent / "groupes"):
            if (rac / p_audio.name).is_file():
                trouve = rac / p_audio.name
                break
        if trouve is None:
            return {"statut": "échec", "group_id": group_id,
                    "erreur": f"WAV du groupe introuvable : {p_audio.name}"}
        con.execute("UPDATE chunk_group SET audio_path=? WHERE group_id=?",
                    (str(trouve), group_id))
        con.commit()
        p_audio = trouve
    audio = str(p_audio)

    r = transcrire(audio, backend=backend)
    if r.get("erreur"):
        return {"statut": "échec", "group_id": group_id, "erreur": r["erreur"]}

    stats = redistribuer(con, group_id, r["segments"])

    con.execute(
        "UPDATE chunk_group SET statut='transcrit',"
        " note = COALESCE(note,'') || ? WHERE group_id=?",
        (f" | transcription RTF {r.get('rtf')} backend {r.get('backend')} "
         f"{stats.get('ecrits')} segments", group_id))
    con.commit()

    if trace:
        _ecrire_trace(group_id, g, r, stats)

    return {"statut": "ok", "group_id": group_id,
            "rtf": r.get("rtf"), "backend": r.get("backend"),
            "duree_audio": r.get("duree"), "elapsed": r.get("elapsed"),
            "langage": r.get("langue"), **stats}


def _ecrire_trace(group_id: int, g, r: dict, stats: dict) -> None:
    """Écrit un livrable MARKDOWN lisible — le propriétaire doit pouvoir le lire.

    Règle 17 : le Markdown est la sortie humaine, la base est l'état
    technique. Un livrable par étape, daté, dans work/traces/.
    """
    config.WORK_TRACE.mkdir(parents=True, exist_ok=True)
    chemin = config.WORK_TRACE / f"groupe_{group_id:04d}_transcription.md"
    lignes = [
        f"# Transcription — groupe {group_id}",
        "",
        f"- **Session** : {g['session_id']}",
        f"- **Début (absolu)** : {g['start_time']}",
        f"- **Chunks regroupés** : {g['n_chunks']} × {g['taille']}",
        f"- **Durée audio** : {r.get('duree')} s",
        f"- **Modèle** : {r.get('modele')} ({r.get('modele_version')})",
        f"- **Backend** : {r.get('backend')} — RTF {r.get('rtf')}",
        f"- **Calcul** : {r.get('elapsed')} s pour {r.get('duree')} s d'audio",
        f"- **Langue** : {r.get('langue')}",
        "",
        "## Redistribution",
        "",
        f"- segments du groupe : {stats.get('segments')}",
        f"- segments écrits en base : {stats.get('ecrits')}",
        f"- coupes aux frontières de chunks : {stats.get('coupes')}",
        f"- rattachements au plus proche : {stats.get('orphelins')}",
        f"- chunks touchés : {stats.get('chunks')}",
        "",
        "## Texte",
        "",
    ]
    for seg in r.get("segments", []):
        lignes.append(f"`[{seg['start']:8.2f} → {seg['end']:8.2f}]` {seg['text']}")
    lignes.append("")
    chemin.write_text("\n".join(lignes), encoding="utf-8")
