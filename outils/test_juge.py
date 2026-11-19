"""Prépare le test « jugement humain à l'aveugle »

On tire N extraits prédits « le propriétaire » et N prédits « pas le propriétaire » (voix de média),
on les exporte dans un ordre mélangé sous des noms neutres, et on garde la
réponse de la machine dans un CSV à part — que le propriétaire ne lit qu'après avoir
donné son verdict.

    python3 outils/test_juge.py --n 6           # 6 + 6 extraits
    python3 outils/test_juge.py --n 6 --duree 8

Sortie : work/test_juge/clip_XX.mp3 + work/test_juge/reponses.csv
"""
import argparse
import csv
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

SORTIE = Path(str(config.V2_DIR)) / "work" / "test_juge"


def groupes_du_chunk(con) -> dict:
    return {c: (g, a, s) for c, g, a, s in con.execute(
        "SELECT m.chunk_id, g.group_id, g.audio_path, g.start_time"
        " FROM group_member m JOIN chunk_group g ON g.group_id=m.group_id")}


def extraire(wav: Path, debut: float, duree: float, dest: Path) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-nostdin", "-ss", f"{max(0, debut):.2f}",
         "-t", f"{duree:.2f}", "-i", str(wav), "-af",
         "loudnorm=I=-16:TP=-1.5:LRA=11", "-b:a", "128k", str(dest)],
        capture_output=True)
    return r.returncode == 0 and dest.is_file() and dest.stat().st_size > 2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="extraits par catégorie")
    ap.add_argument("--duree", type=float, default=6.0)
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    info = groupes_du_chunk(con)

    def tirer(condition: str, tri: str):
        return con.execute(
            "SELECT s.segment_id, s.chunk_id, s.start_time, s.end_time, s.text"
            " FROM segment s JOIN chunk c ON c.chunk_id=s.chunk_id"
            f" WHERE {condition} AND s.text IS NOT NULL AND s.end_time-s.start_time >= 2.5"
            # Un extrait d'un mot (« Merci. », « la ligne 3 ») ne se juge pas :
            # le propriétaire n'a rien à y reconnaître. On ne tire que des phrases.
            " AND LENGTH(TRIM(s.text)) >= 60"
            f" ORDER BY {tri} LIMIT 400").fetchall()

    # Prédits le propriétaire : speaker_id 0. Prédits « pas le propriétaire » : identifiés comme
    # autre voix. On évite la musique (audio_kind) et les hallucinations.
    proprietaire = tirer("s.speaker_id = 0 AND COALESCE(s.audio_kind,'parole') = 'parole'",
                 "s.segment_id")
    autre = tirer("(s.speaker_id IS NULL OR s.speaker_id <> 0)"
                  " AND COALESCE(s.audio_kind,'parole') = 'parole'", "s.segment_id")
    random.seed()
    random.shuffle(proprietaire)
    random.shuffle(autre)

    SORTIE.mkdir(parents=True, exist_ok=True)
    for vieux in SORTIE.glob("clip_*.mp3"):
        vieux.unlink()

    lignes, n = [], 0
    for etiquette, lot in (("proprietaire", proprietaire), ("autre", autre)):
        pris = 0
        for seg_id, chunk_id, debut_abs, fin_abs, texte in lot:
            if pris >= args.n:
                break
            if chunk_id not in info:
                continue
            _gid, audio_path, g_start = info[chunk_id]
            wav = Path(str(config.WORK_GROUPES)) / os.path.basename(audio_path or "")
            if not wav.is_file():
                continue
            relatif = float(debut_abs) - float(g_start)
            if relatif < 0:
                continue
            duree = min(args.duree, float(fin_abs) - float(debut_abs) + 0.6)
            nom = f"clip_{n + 1:02d}.mp3"
            if not extraire(wav, relatif - 0.3, duree, SORTIE / nom):
                continue
            lignes.append({"extrait": nom, "prediction": etiquette,
                           "segment": seg_id, "texte": texte[:200]})
            n += 1
            pris += 1

    random.shuffle(lignes)
    with open(SORTIE / "reponses.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["extrait", "prediction", "segment", "texte"])
        w.writeheader()
        w.writerows(sorted(lignes, key=lambda z: z["extrait"]))

    print(f"{len(lignes)} extraits dans {SORTIE}")
    print("fiche de correction : reponses.csv (à n'ouvrir qu'après avoir voté)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
