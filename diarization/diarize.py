"""ÉTAPE 3 — DIARISATION (pyannote speaker-diarization-community-1).

Cette étape répond à la question « qui parle, et quand ? ». Elle ne
répond PAS à « qui est-ce ? » — ça vient à l'étape 4 (identification).

La diarisation tourne sur CPU ou GPU selon la plateforme. Sans CUDA
disponible, c'est le CPU qui travaille : c'est l'étape la plus lente de la
chaîne, nettement plus coûteuse que la transcription. Son coût réel est
mesuré à l'exécution et journalisé avec le groupe.

⚠ LE PIÈGE PRINCIPAL DE CETTE ÉTAPE. pyannote numérote ses locuteurs À
CHAQUE FICHIER : SPEAKER_00, SPEAKER_01... Ces numéros n'ont AUCUN sens
en dehors du fichier traité. La même voix peut être SPEAKER_00 dans un
groupe et SPEAKER_01 dans le suivant. On stocke donc :
  - `label_local` : le numéro de pyannote, valable pour CE groupe seul ;
  - `speaker_id`   : le profil réel, rempli plus tard (étape 4). Tant
                     qu'il est vide c'est UNKNOWN, jamais un numéro
                     inventé (règle 3).

Sans cette séparation, la V1 avait produit des profils qui changeaient
d'un fichier à l'autre : même voix, deux identités.
"""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import hub

# Pyannote est lourd : le pipeline se charge UNE fois par process, puis est
# réutilisé pour tous les groupes.
_PIPELINE = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version_modele() -> str:
    try:
        from huggingface_hub import model_info
        return model_info(config.MODELE_DIARISATION).sha[:12]
    except Exception:
        return "inconnue"


def charger():
    """Charge le pipeline pyannote une fois. Ne lève pas (règle 3)."""
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    try:
        from pyannote.audio import Pipeline
        _PIPELINE = Pipeline.from_pretrained(config.MODELE_DIARISATION)
        return _PIPELINE
    except Exception as e:
        raise RuntimeError(f"pyannote indisponible : {e}") from e


def _hub_diariser(audio: str) -> dict | None:
    """Confie la diarisation au Hub. None si le Hub n'a rien rendu.

    Même contrat que la transcription (voir hub.py) : sonde préalable,
    nom de partage, repli local. La diarisation est l'étape la PLUS LENTE
    de la chaîne : c'est elle qui a le plus à gagner
    à être répartie sur les deux machines.
    """
    out = hub.soumettre("diarize", args=[f"{hub.PARTAGE}/{Path(audio).name}"])
    if out is None:
        return None
    # Le Hub renvoie des listes [début, fin, étiquette] (compact), pas des
    # dicts : on les remet dans la forme attendue par enregistrer_tours.
    tours = [{"start": float(t[0]), "end": float(t[1]), "label": t[2]}
             for t in out.get("turns") or []]
    tours.sort(key=lambda t: t["start"])
    return {"tours": tours, "worker": out.get("worker"),
            "device": out.get("device")}


def diariser(audio: str, min_duration: float = 0.4) -> dict:
    """Diarise un fichier. Renvoie les tours de parole.

    `min_duration` : un tour de 0,2 s est du bruit ou une toux, pas une
    intervention. La V1 gardait tout et polluait les profils avec des
    profils d'une demi-seconde. On ne garde que les tours vraiment
    exploitables, et on compte ce qu'on a jeté.

    Le Hub est essayé d'abord (les deux machines) ; s'il ne rend rien, on
    diarise en local. Le filtrage `min_duration` est appliqué ICI, donc
    le résultat est identique quelle que soit la machine qui a calculé.
    """
    t_hub = time.time()
    if hub.actif():
        h = _hub_diariser(audio)
        if h is not None:
            gardes = [t for t in h["tours"]
                      if t["end"] - t["start"] >= min_duration]
            elapsed = time.time() - t_hub
            duree = gardes[-1]["end"] if gardes else 0.0
            return {
                "tours": gardes,
                "n_tours": len(gardes),
                "tours_jetes": len(h["tours"]) - len(gardes),
                "n_locuteurs": len({t["label"] for t in gardes}),
                "locuteurs": sorted({t["label"] for t in gardes}),
                "elapsed": round(elapsed, 2),
                "rtf": round(elapsed / duree, 3) if duree else None,
                "modele": f"pyannote-community-1 (hub:{h.get('worker', '?')})",
                "modele_version": version_modele(),
                "device": h.get("device"),
            }

    pipe = charger()
    t0 = time.time()
    try:
        sortie = pipe(audio)
    except Exception as e:
        return {"tours": [], "erreur": f"diarisation échouée : {e}"}

    # pyannote >= 4.0 renvoie une Annotation ; les versions anciennes
    # renvoyaient un objet avec .speaker_diarization. On gère les deux.
    ann = getattr(sortie, "speaker_diarization", sortie)

    tours = []
    jetes = 0
    for segment, _, label in ann.itertracks(yield_label=True):
        d = segment.end - segment.start
        if d < min_duration:
            jetes += 1
            continue
        tours.append({"start": round(segment.start, 3),
                      "end": round(segment.end, 3),
                      "label": label})

    tours.sort(key=lambda t: t["start"])
    elapsed = time.time() - t0
    duree = tours[-1]["end"] if tours else 0.0
    return {
        "tours": tours,
        "n_tours": len(tours),
        "tours_jetes": jetes,
        "n_locuteurs": len({t["label"] for t in tours}),
        "locuteurs": sorted({t["label"] for t in tours}),
        "elapsed": round(elapsed, 2),
        "rtf": round(elapsed / duree, 3) if duree else None,
        "modele": "pyannote-community-1",
        "modele_version": version_modele(),
    }


def enregistrer_tours(con: sqlite3.Connection, group_id: int,
                       tours: list[dict]) -> dict:
    """Écrit les tours du groupe en base, en temps ABSOLUS (règle 2).

    pyannote donne des temps relatifs à son fichier. On y ajoute le
    `start_time` absolu du premier chunk du groupe, exactement comme pour
    la transcription : les deux étapes partagent donc la même ligne de base
    temporelle, sinon les tours et les segments ne se recouvrent jamais
    et le matching locuteur/texte ne peut rien faire.
    """
    from grouping import group as gmod

    membres = gmod.chunks_du_groupe(con, group_id)
    if not membres:
        return {"erreur": "groupe sans membres"}
    base_absolue = float(membres[0]["start_time"])

    con.execute("DELETE FROM turn WHERE group_id=?", (group_id,))
    for idx, t in enumerate(tours):
        con.execute(
            "INSERT OR REPLACE INTO turn(group_id, idx, start_time, end_time,"
            " label_local, speaker_id, confiance) VALUES(?,?,?,?,?,?,?)",
            (group_id, idx, round(base_absolue + t["start"], 3),
             round(base_absolue + t["end"], 3), t["label"], None, "UNKNOWN"))
    con.commit()
    return {"tours": len(tours),
            "locuteurs": sorted({t["label"] for t in tours}),
            "base_absolue": base_absolue,
            "tours_liste": [
                {"start": round(base_absolue + t["start"], 3),
                 "end": round(base_absolue + t["end"], 3),
                 "label": t["label"]} for t in tours]}


def diariser_groupe(con: sqlite3.Connection, group_id: int,
                    trace: bool = True) -> dict:
    """Diarise le WAV du groupe et enregistre les tours.

    Idempotent (règle 19) : un groupe déjà diarisé n'est pas refait.
    """
    g = con.execute("SELECT * FROM chunk_group WHERE group_id=?",
                    (group_id,)).fetchone()
    if g is None:
        return {"erreur": f"groupe {group_id} inexistant"}
    if not g["audio_path"]:
        return {"erreur": "groupe sans audio"}

    deja = con.execute("SELECT COUNT(*) FROM turn WHERE group_id=?",
                       (group_id,)).fetchone()[0]
    # Le marqueur écrit plus bas est « diarisation … ». « diarise » (avec un e)
    # n'y est PAS contenu (« diari-s-a ») : tester les deux formes, sinon le
    # garde ne se déclenche jamais et TOUS les groupes sont recalculés à
    # chaque run — bien plus que les seuls groupes neufs.
    note = g["note"] or ""
    if deja and ("diarisation" in note or "diarise" in note):
        return {"statut": "déjà_diarisé", "group_id": group_id, "tours": deja}

    r = diariser(g["audio_path"])
    if r.get("erreur"):
        return {"statut": "échec", "group_id": group_id, "erreur": r["erreur"]}

    ecrit = enregistrer_tours(con, group_id, r["tours"])

    con.execute(
        "UPDATE chunk_group SET note = COALESCE(note,'') || ? WHERE group_id=?",
        (f" | diarisation {r['n_tours']} tours, {r['n_locuteurs']} voix, "
         f"RTF {r.get('rtf')}", group_id))
    con.commit()

    if trace:
        _ecrire_trace(group_id, g, r, ecrit)

    return {"statut": "ok", "group_id": group_id,
            "tours": r["n_tours"], "tours_jetes": r["tours_jetes"],
            "locuteurs": r["locuteurs"], "rtf": r.get("rtf"),
            "elapsed": r.get("elapsed"), **ecrit}


def _ecrire_trace(group_id: int, g, r: dict, ecrit: dict) -> None:
    config.WORK_TRACE.mkdir(parents=True, exist_ok=True)
    chemin = config.WORK_TRACE / f"groupe_{group_id:04d}_diarisation.md"
    base = ecrit.get("base_absolue", 0.0)
    lignes = [
        f"# Diarisation — groupe {group_id}",
        "",
        f"- **Session** : {g['session_id']}",
        f"- **Base temporelle absolue** : {base}",
        f"- **Modèle** : {r.get('modele')} ({r.get('modele_version')})",
        f"- **Calcul** : {r.get('elapsed')} s — RTF {r.get('rtf')}",
        f"- **Tours gardés** : {r.get('n_tours')}",
        f"- **Tours jetés (< 0,4 s)** : {r.get('tours_jetes')}",
        f"- **Voix détectées** : {', '.join(r.get('locuteurs', []))}",
        "",
        "> Les étiquettes SPEAKER_xx sont locales à ce groupe. Elles ne",
        "> deviennent des profils qu'après l'étape d'identification.",
        "",
        "| # | début | fin | voix (local) | durée |",
        "|---|-------|-----|--------------|-------|",
    ]
    for idx, t in enumerate(ecrit.get("tours_liste", [])):
        lignes.append(f"| {idx} | {t['start']} | {t['end']} | "
                     f"{t['label']} | {t['end'] - t['start']:.1f} s |")
    lignes.append("")
    chemin.write_text("\n".join(lignes), encoding="utf-8")


def couverture(con: sqlite3.Connection, group_id: int) -> dict:
    """Mesure ce que la diarisation couvre vraiment du texte.

    C'est le contrôle qui compte : pyannote peut ne couvrir que 40 % du
    groupe si l'audio est surtout du silence ou de la musique. On veut
    savoir quelle PARTIE de la parole a été attribuée à quelqu'un, parce
    que c'est elle qui sera transformée en phrases attribuées.
    """
    from grouping import group as gmod

    membres = gmod.chunks_du_groupe(con, group_id)
    if not membres:
        return {"erreur": "groupe sans membres"}

    segments = con.execute(
        "SELECT s.start_time, s.end_time, s.text FROM segment s"
        " JOIN group_member m ON m.chunk_id = s.chunk_id"
        " WHERE m.group_id=? AND s.text <> '' ORDER BY s.start_time",
        (group_id,)).fetchall()
    if not segments:
        return {"segments": 0, "couverts": 0.0, "couverture": 0.0}

    tours = con.execute(
        "SELECT start_time, end_time FROM turn WHERE group_id=?"
        " ORDER BY start_time", (group_id,)).fetchall()

    # on ne recompte pas les recouvrements entre tours qui se touchent
    unites = []
    for t in tours:
        if unites and t[0] <= unites[-1][1]:
            unites[-1] = (unites[-1][0], max(unites[-1][1], t[1]))
        else:
            unites.append((t[0], t[1]))

    def chevauche(a, b, unites):
        return any(max(a, u) < min(b, v) for (u, v) in unites)

    total = sum(t[1] - t[0] for t in segments)
    couverts = sum((t[1] - t[0]) for t in segments
                   if chevauche(t[0], t[1], unites))
    return {
        "segments": len(segments),
        "duree_parlee": round(total, 1),
        "duree_attribuee": round(couverts, 1),
        "couverture": round(couverts / total * 100, 1) if total else 0.0,
        "n_tours": len(tours),
    }
