"""Ajouter un échantillon de la voix du propriétaire au profil unique.

C'est le levier le plus efficace pour la robustesse : chaque micro a sa
signature (téléphone à 3 m, webcam, casque, micro haut-parleur). Une
empreinte par condition vaut mieux que cinquante empreintes du même
passage — c'est pourquoi l'outil découpe l'enregistrement en tranches et
n'en garde que les meilleures, jamais des doublons.

Usage :
    python3 outils/enroler_proprietaire.py chemin/vers/webcam.wav --condition webcam
    python3 outils/enroler_proprietaire.py chemin/vers/casque.m4a --condition casque --duree 6

Le format d'entrée n'est pas un problème : soundfile lit le WAV, ffmpeg
convertit le reste (m4a, mp3, ogg) sans qu'on installe quoi que ce soit.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import numpy as np  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
from speakers import proprietaire as bx  # noqa: E402


def _lire(chemin: str) -> tuple[np.ndarray, int]:
    import soundfile as sf
    try:
        x, sr = sf.read(chemin, dtype="float32")
        return x, sr
    except Exception:
        # m4a/mp3/ogg : ffmpeg fait la conversion dans un fichier temporaire
        tmp = os.path.join(tempfile.mkdtemp(), "conversion.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", chemin,
                        "-ac", "1", "-ar", "16000", tmp], check=True)
        return sf.read(tmp, dtype="float32")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fichiers", nargs="+")
    ap.add_argument("--condition", default="manuel",
                    help="d'où vient le son (telephone, webcam, casque…)")
    ap.add_argument("--duree", type=float, default=6.0,
                    help="longueur d'une tranche (s)")
    a = ap.parse_args()

    con = db.connect(config.DB_PATH)
    total = 0
    for f in a.fichiers:
        x, sr = _lire(os.path.expanduser(f))
        if x.ndim > 1:
            x = x.mean(axis=1)
        tranche = int(a.duree * sr)
        pris = rejet = 0
        for d in range(0, max(1, len(x) - tranche + 1), tranche // 2):
            if bx.enroler(con, x[d:d + tranche], sr, None, a.condition):
                pris += 1
            else:
                rejet += 1
        total += pris
        print(f"{os.path.basename(f)} : {pris} tranche(s) gardée(s), "
              f"{rejet} rejetée(s) (trop différentes ou doublons)")
    e = bx.etat(con)
    print(f"profil le propriétaire : noyau de {e['empreintes_noyau']} empreintes "
          f"(cos moyen {e['cos_moyen']}, min {e['cos_min']})")
    con.close()
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
