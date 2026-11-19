"""Migration « une seule voix » : on ne garde que le profil du propriétaire.

Ce que fait ce script, dans l'ordre, et RIEN D'AUTRE :

  1. sauvegarde la base (database/backups/) avant de toucher quoi que ce soit ;
  2. ajoute `segment.audio_kind` si elle manque (musique / parole) ;
  3. supprime les profils parasites et leurs empreintes ;
  4. remet à NULL les attributions qui pointaient vers ces profils — les
     voix concernées ne sont pas des interlocuteurs, c'est du contenu ;
  5. supprime les empreintes du profil 0 qui ne ressemblent pas au noyau
     (voix de média avalées par l'ancien profil) et affiche le résultat.

⚠ Aucun audio n'est touché : ni `audio/`, ni `work/groupes/`, ni les chunks.
   Règle : on ne touche pas à l'audio existant.

Usage :
    python3 outils/migrer_voix_proprietaire.py            # applique
    python3 outils/migrer_voix_proprietaire.py --sec      # simulation
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import config  # noqa: E402
from speakers import proprietaire as bx  # noqa: E402


def _sauvegarde() -> str:
    os.makedirs(config.DATABASE_BACKUP, exist_ok=True)
    dest = os.path.join(
        config.DATABASE_BACKUP,
        f"memorybrain-avant-voix-unique-{datetime.now():%Y%m%d_%H%M%S}.sqlite")
    shutil.copy2(config.DB_PATH, dest)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", action="store_true", help="simulation")
    a = ap.parse_args()

    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row

    profils = con.execute("SELECT COUNT(*) FROM speaker").fetchone()[0]
    embs = con.execute("SELECT COUNT(*) FROM speaker_embedding").fetchone()[0]
    tours = con.execute("SELECT COUNT(*) FROM turn").fetchone()[0]
    segs = con.execute("SELECT COUNT(*) FROM segment").fetchone()[0]
    print(f"avant : {profils} profils, {embs} empreintes, {tours} tours, "
          f"{segs} segments")

    cols = {r[1] for r in con.execute("PRAGMA table_info(segment)")}
    if a.sec:
        print("[simulation] aucune écriture")
        con.close()
        return 0

    chemin = _sauvegarde()
    print(f"sauvegarde : {chemin}")

    if "audio_kind" not in cols:
        con.execute("ALTER TABLE segment ADD COLUMN audio_kind TEXT")
        print("colonne segment.audio_kind ajoutée")

    # 3. profils parasites
    n_emb = con.execute("DELETE FROM speaker_embedding WHERE speaker_id<>?",
                        (config.PROFIL_PROPRIETAIRE,)).rowcount
    n_spk = con.execute("DELETE FROM speaker WHERE speaker_id<>?",
                        (config.PROFIL_PROPRIETAIRE,)).rowcount
    # 4. attributions orphelines
    n_turn = con.execute("UPDATE turn SET speaker_id=NULL,"
                         " confiance='UNKNOWN' WHERE speaker_id IS NOT NULL"
                         " AND speaker_id<>?", (config.PROFIL_PROPRIETAIRE,)).rowcount
    n_seg = con.execute("UPDATE segment SET speaker_id=NULL WHERE"
                        " speaker_id IS NOT NULL AND speaker_id<>?",
                        (config.PROFIL_PROPRIETAIRE,)).rowcount
    con.commit()
    print(f"supprimés : {n_spk} profils, {n_emb} empreintes ; "
          f"désattribués : {n_turn} tours, {n_seg} segments")

    # 5. nettoyage du profil 0 : les empreintes hors noyau partent.
    lignes = con.execute(
        "SELECT embedding_id, vecteur, dim FROM speaker_embedding"
        " WHERE speaker_id=? ORDER BY embedding_id",
        (config.PROFIL_PROPRIETAIRE,)).fetchall()
    import numpy as np
    V = np.array([np.frombuffer(r["vecteur"], dtype="float32")[:r["dim"]]
                  / np.linalg.norm(np.frombuffer(r["vecteur"],
                                                 dtype="float32")[:r["dim"]])
                  for r in lignes])
    garde = np.ones(len(V), dtype=bool)
    for _ in range(6):
        c = V[garde].mean(axis=0)
        c /= np.linalg.norm(c)
        nouveau = (V @ c) >= 0.55
        if nouveau.sum() == garde.sum() or nouveau.sum() < 3:
            break
        garde = nouveau
    hors = [lignes[i]["embedding_id"] for i in range(len(lignes)) if not garde[i]]
    if hors:
        con.executemany("DELETE FROM speaker_embedding WHERE embedding_id=?",
                        [(i,) for i in hors])
        con.commit()
    bx.oublier_cache()
    e = bx.etat(con)
    print(f"profil le propriétaire : {len(hors)} empreinte(s) contaminée(s) écartée(s), "
          f"noyau = {e['empreintes_noyau']} "
          f"(cos moyen {e['cos_moyen']}, min {e['cos_min']})")
    apres = con.execute("SELECT COUNT(*) FROM speaker").fetchone()[0]
    print(f"après : {apres} profil(s) en base")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
