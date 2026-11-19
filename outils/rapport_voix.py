"""Bilan chiffré de l'étape voix, lu dans la base — pas dans un rapport.

Usage :
    python3 outils/rapport_voix.py            # tout le corpus
    python3 outils/rapport_voix.py --csv f.csv

Répond aux seules questions qui comptent : combien de mots du propriétaire
ont été retrouvés, combien sont attribués à un autre, combien restent sans
locuteur.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

# ORDRES DE GRANDEUR, pas des mesures : ils servent à annoncer une durée de
# traitement AVANT de la lancer, et dépendent entièrement du matériel. Le
# préfixe le dit, pour qu'on ne les cite jamais comme un résultat.
ORDRE_MS_EMPREINTE = 26            # par fenêtre de 2,5 s
ORDRE_MS_MUSIQUE = 707             # par fenêtre de 10 s
ORDRE_S_PYANNOTE_PAR_HEURE = 35 * 60


def rapport(con: sqlite3.Connection) -> dict:
    q = con.execute
    total = q("SELECT COUNT(*) FROM chunk_group").fetchone()[0]
    duree = q("SELECT SUM(duration_sec) FROM chunk_group").fetchone()[0] or 0

    # segments et parole attribuée
    seg = q("SELECT COUNT(*) FROM segment").fetchone()[0]
    proprietaire = q("SELECT COUNT(*) FROM segment WHERE speaker_id=?",
             (config.PROFIL_PROPRIETAIRE,)).fetchone()[0]
    mus = q("SELECT COUNT(*) FROM segment WHERE audio_kind='musique'").fetchone()[0]
    hall = q("SELECT COUNT(*) FROM segment WHERE audio_kind='hallucination'").fetchone()[0]
    sans = q("SELECT COUNT(*) FROM segment WHERE speaker_id IS NULL").fetchone()[0]

    # groupes
    g_proprietaire = q("""SELECT COUNT(DISTINCT m.group_id) FROM segment s
                  JOIN group_member m ON m.chunk_id=s.chunk_id
                  WHERE s.speaker_id=?""", (config.PROFIL_PROPRIETAIRE,)).fetchone()[0]
    g_mus = q("""SELECT COUNT(DISTINCT m.group_id) FROM segment s
                 JOIN group_member m ON m.chunk_id=s.chunk_id
                 WHERE s.audio_kind='musique'""").fetchone()[0]
    profils = q("SELECT COUNT(*) FROM speaker").fetchone()[0]

    # parole propriétaire en minutes
    proprietaire_s = q("""SELECT SUM(s.end_time-s.start_time) FROM segment s
                  WHERE s.speaker_id=?""", (config.PROFIL_PROPRIETAIRE,)).fetchone()[0] or 0
    mus_s = q("""SELECT SUM(s.end_time-s.start_time) FROM segment s
                 WHERE s.audio_kind='musique'""").fetchone()[0] or 0

    # calcul : fenêtres de 2,5 s pas 1,25 s sur la parole seulement
    heures = duree / 3600
    n_fenetres = int(proprietaire_s / config.PAS_VOIX / 2)     # deux fenêtres par segment
    voix_s = n_fenetres * ORDRE_MS_EMPREINTE / 1000
    musique_s = (proprietaire_s + mus_s) / 10 * ORDRE_MS_MUSIQUE / 1000
    calcul_h = (voix_s + musique_s) / 3600
    pyannote_h = heures * ORDRE_S_PYANNOTE_PAR_HEURE / 3600

    return dict(
        groupes=total, heures=round(heures, 2), segments=seg,
        segments_proprietaire=proprietaire, segments_musique=mus, segments_hallucination=hall,
        segments_sans_voix=sans, profils=profils,
        groupes_avec_proprietaire=g_proprietaire, groupes_avec_musique=g_mus,
        proprietaire_minutes=round(proprietaire_s / 60, 1), musique_minutes=round(mus_s / 60, 1),
        calcul_voix_min=round(calcul_h * 60, 1),
        calcul_pyannote_h=round(pyannote_h, 1),
        facteur=round(pyannote_h / calcul_h, 1) if calcul_h else None,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    d = rapport(con)
    for k, v in d.items():
        print(f"{k:24} {v}")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(d))
            w.writeheader()
            w.writerow(d)
        print(f"\n-> {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
