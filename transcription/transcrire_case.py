"""ÉTAPE 1 — TRANSCRIRE UNE CASE DE 5 MINUTES.

Une case s'appelle `AAAA-MM-JJ_13h45.wav` et contient exactement
13:45:00 → 13:50:00 (voir `blocs.py`). Donc :

    heure réelle = début de la case + position dans le fichier

C'est la seule conversion du programme. Il n'y a plus de recoupement aux
frontières de chunks, plus de redistribution, plus de table d'offsets : un
segment de Whisper EST un segment, avec son heure vraie.

Le `chunk_id` reste rempli — non pas pour calculer quoi que ce soit, mais
pour savoir de quel enregistrement du téléphone vient la phrase (traçabilité
et provenance, règle 17). On le trouve par une simple recherche : quel chunk
contient le milieu du segment. Si le segment est à cheval sur deux chunks,
il appartient à celui où il commence — on ne le coupe plus, couper faisait
disparaître des mots (un tiers du texte, sur les premiers essais).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config
import blocs
from transcription import transcribe_group as tg


def _chunk_du_milieu(con: sqlite3.Connection, milieu: float):
    """Le chunk qui contient ce moment ; sinon le plus proche (règle 3)."""
    r = con.execute(
        "SELECT chunk_id FROM chunk WHERE start_time IS NOT NULL"
        " AND duration_sec IS NOT NULL"
        " AND start_time <= ? AND start_time + duration_sec >= ?"
        " ORDER BY chunk_id LIMIT 1", (milieu, milieu)).fetchone()
    if r:
        return r["chunk_id"]
    r = con.execute(
        "SELECT chunk_id FROM chunk WHERE start_time IS NOT NULL"
        " AND duration_sec IS NOT NULL"
        " ORDER BY ABS(start_time - ?) LIMIT 1", (milieu,)).fetchone()
    return r["chunk_id"] if r else None


def transcrire_case(con: sqlite3.Connection, t_debut: float,
                    forcer: bool = False) -> dict:
    """Transcrit une case et écrit ses segments en heure absolue.

    Idempotent : on efface les segments des chunks de cette case avant de
    réécrire, donc rejouer ne duplique rien (règle 19).
    """
    chemin = blocs.chemin_case(t_debut)
    if not chemin.is_file():
        return {"case": blocs.nom_case(t_debut), "statut": "absente"}

    marque = con.execute("SELECT statut FROM bloc WHERE start_time=?",
                         (t_debut,)).fetchone()
    if marque and marque["statut"] in ("transcrite", "voix", "fini") and not forcer:
        return {"case": blocs.nom_case(t_debut), "statut": "déjà_transcrite"}

    r = tg.transcrire(str(chemin))
    if r.get("erreur"):
        return {"case": blocs.nom_case(t_debut), "statut": "échec",
                "erreur": r["erreur"]}

    # ⚠ On efface les segments DE CETTE PLAGE D'HEURE, pas « ceux des chunks
    # de cette case ». Un chunk à cheval sur deux cases appartient aux deux :
    # l'effacer depuis la case suivante supprimait les phrases déjà écrites
    # depuis la case précédente : un chunk transcrit par la case d'avant,
    # puis effacé par celle d'après.
    con.execute("DELETE FROM segment WHERE start_time >= ? AND start_time < ?",
                (t_debut, t_debut + blocs.DUREE_CASE))

    ecrits = 0
    par_chunk: dict[int, int] = {}
    for seg in r["segments"]:
        texte = (seg.get("text") or "").strip()
        if not texte:
            continue
        t0 = blocs.heure_absolue(t_debut, seg["start"])
        t1 = blocs.heure_absolue(t_debut, seg["end"])
        cid = _chunk_du_milieu(con, (t0 + t1) / 2.0)
        if cid is None:
            continue
        idx = par_chunk.get(cid, 0)
        par_chunk[cid] = idx + 1
        con.execute(
            "INSERT INTO segment(chunk_id, idx, start_time, end_time, text)"
            " VALUES(?,?,?,?,?)", (cid, idx, t0, t1, texte))
        ecrits += 1

    con.execute("UPDATE bloc SET statut='transcrite', duree=?"
                " WHERE start_time=?", (blocs.DUREE_CASE, t_debut))
    for cid in par_chunk:
        con.execute("UPDATE chunk SET status='transcribed' WHERE chunk_id=?", (cid,))
    con.commit()

    return {"case": blocs.nom_case(t_debut), "statut": "ok",
            "segments": ecrits, "chunks": len(par_chunk),
            "rtf": r.get("rtf"), "elapsed": r.get("elapsed"),
            "backend": r.get("backend"), "langue": r.get("langue")}


def transcrire_cases(con: sqlite3.Connection, debuts: list[float],
                     forcer: bool = False, progress=None) -> dict:
    out = []
    for i, d in enumerate(sorted(debuts), 1):
        r = transcrire_case(con, d, forcer=forcer)
        out.append(r)
        if progress:
            progress(i, len(out), r)
    return {"cases": out,
            "segments": sum(x.get("segments", 0) for x in out),
            "elapsed": round(sum(x.get("elapsed") or 0 for x in out), 1)}
