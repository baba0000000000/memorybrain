#!/usr/bin/env python3
"""Retrouver le FICHIER d'un groupe (et l'inverse).

Pourquoi ce script existe : le pipeline raisonne en `group_id` (9, 108, 206…)
mais les WAV sur le disque s'appellent `s2_AAAA-MM-JJ_HH-MM-SS_5ch.wav`.
Aucun des deux ne contient l'autre. La table `chunk_group` est le SEUL lien.

Usage :
    python3 outils/groupe.py 108          # groupe 108 -> quel fichier ?
    python3 outils/groupe.py 9 179 206    # plusieurs groupes
    python3 outils/groupe.py --fichier s2_AAAA-MM-JJ_HH-MM-SS_5ch.wav
    python3 outils/groupe.py --vides      # tous les groupes SANS parole (0 segment)
    python3 outils/groupe.py --liste      # la table complète
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
BASE = RACINE / "database" / "memorybrain.sqlite"

REQ = """
SELECT g.group_id, g.session_id, g.start_time, g.end_time, g.audio_path,
       g.statut, g.n_chunks, g.duration_sec,
       COUNT(s.segment_id) AS seg,
       COALESCE(SUM(LENGTH(TRIM(COALESCE(s.text,'')))),0) AS chars
FROM chunk_group g
LEFT JOIN group_member m ON m.group_id = g.group_id
LEFT JOIN segment s      ON s.chunk_id   = m.chunk_id
WHERE {filtre}
GROUP BY g.group_id
ORDER BY g.group_id
"""


def _date(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


def _nom(g) -> str:
    return (g["audio_path"] or "").split("/")[-1] or "(pas de wav)"


def _affiche(g) -> None:
    print(f"groupe {g['group_id']}  (session {g['session_id']})")
    print(f"  fichier  : {_nom(g)}")
    print(f"  debut    : {_date(g['start_time'])}")
    print(f"  duree    : {g['duration_sec']} s   ({g['n_chunks']} chunks)")
    print(f"  parole   : {g['seg']} segments, {g['chars']} caracteres")
    print(f"  statut   : {g['statut']}")
    p = Path(g["audio_path"] or "")
    print(f"  present  : {'oui' if p.is_file() else 'NON (a la poubelle ?)'}")
    print(f"  chemin   : {g['audio_path'] or '-'}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="groupe <-> fichier")
    ap.add_argument("groupes", nargs="*", type=int)
    ap.add_argument("--fichier", help="nom de fichier -> numero de groupe")
    ap.add_argument("--vides", action="store_true", help="groupes sans parole (0 segment)")
    ap.add_argument("--liste", action="store_true", help="table complete")
    a = ap.parse_args()

    if not BASE.is_file():
        print(f"base absente : {BASE}", file=sys.stderr)
        return 1
    con = sqlite3.connect(BASE)
    con.row_factory = sqlite3.Row

    if a.fichier:
        nom = a.fichier.split("/")[-1]
        rows = con.execute(REQ.format(filtre="g.audio_path LIKE ?"), (f"%{nom}%",)).fetchall()
        if not rows:
            print(f"aucun groupe pour le fichier {nom}")
            return 1
        for g in rows:
            _affiche(g)
        return 0

    if a.vides:
        rows = con.execute(REQ.format(filtre="1=1")).fetchall()
        vides = [g for g in rows if g["seg"] == 0]
        print(f"{len(vides)} groupes SANS parole (0 segment) sur {len(rows)}\n")
        print(f"{'groupe':>7}  {'fichier':<40} {'debut':<20} {'present'}")
        for g in vides:
            p = Path(g["audio_path"] or "")
            print(f"{g['group_id']:>7}  {_nom(g):<40} {_date(g['start_time']):<20} "
                  f"{'oui' if p.is_file() else 'NON'}")
        return 0

    if a.groupes:
        q = REQ.format(filtre=f"g.group_id IN ({','.join('?'*len(a.groupes))})")
        rows = con.execute(q, a.groupes).fetchall()
        for g in rows:
            _affiche(g)
        manquants = set(a.groupes) - {g["group_id"] for g in rows}
        for m in sorted(manquants):
            print(f"groupe {m} : INTROUVABLE")
        return 0

    if a.liste:
        rows = con.execute(REQ.format(filtre="1=1")).fetchall()
        print(f"{'groupe':>7}  {'sess':>4}  {'debut':<20} {'seg':>4}  {'fichier'}")
        for g in rows:
            print(f"{g['group_id']:>7}  {g['session_id']:>4}  {_date(g['start_time']):<20} "
                  f"{g['seg']:>4}  {_nom(g)}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
