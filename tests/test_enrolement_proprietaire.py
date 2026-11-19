"""Tests de l'enrôlement du profil 0 (le propriétaire) — étape 4b.

Défaut RÉEL corrigé : le propriétaire n'était pas reconnu.

Le premier groupe traité a un répertoire vocal VIDE. L'ancien code créait
alors un « nouveau profil » (1) pour la voix dominante, au lieu de la
donner au propriétaire. Résultat : le propriétaire n'était jamais enrôlé, et
chaque groupe refabriquait un profil.

Contrat à tenir :
  - tant que le profil 0 n'a AUCUNE empreinte, la voix dominante du groupe
    EST le propriétaire : on la lui attribue et on l'enrôle ;
  - une fois enrôlé, la MÊME voix dans un groupe suivant correspond au
    profil 0 (cosine ≈ 1) et ne crée AUCUN nouveau profil.
"""
import pathlib
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
from speakers import rattacher  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="mb_proprietaire_"))
        self.con = db.connect(self.tmpdir / "t.sqlite")
        db.set_schema_version(self.con)
        self.con.execute(
            "INSERT INTO source(source_id,kind,label,uri,first_seen)"
            " VALUES(1,'TEST','t','x','2024-01-01')")
        self.con.execute(
            "INSERT INTO session(session_id,source_id,created_at)"
            " VALUES(1,1,'2024-01-01')")
        self._vrai_empreinte = rattacher.empreinte

    def tearDown(self):
        rattacher.empreinte = self._vrai_empreinte
        self.con.close()
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path(str(self.tmpdir / "t.sqlite") + s)
            if p.exists():
                p.unlink()

    def _groupe(self, gid, label="SPEAKER_00", n_tours=5, duree=12.0):
        for k in range(5):
            self.con.execute(
                "INSERT INTO chunk(chunk_id,session_id,source_id,recorded_at,"
                "chunk_index,start_time,end_time,duration_sec,original_file,"
                "hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (100 + gid * 10 + k, 1, 1, "2024-01-01", k, 0.0, 60.0, 60.0,
                 f"c{k}.wav", f"h{gid}{k}", "ok", "2024-01-01"))
        self.con.execute(
            "INSERT INTO chunk_group(group_id,session_id,taille,n_chunks,"
            "start_time,end_time,duration_sec,audio_path,fingerprint,statut,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (gid, 1, 5, 5, 0.0, 300.0, 300.0, "x.wav", f"fp{gid}", "ok",
             "2024-01-01"))
        for k in range(5):
            self.con.execute(
                "INSERT INTO group_member(group_id,chunk_id,rang,offset_sec)"
                " VALUES(?,?,?,?)", (gid, 100 + gid * 10 + k, k, k * 60.0))
        for i in range(n_tours):
            self.con.execute(
                "INSERT INTO turn(group_id,idx,start_time,end_time,label_local)"
                " VALUES(?,?,?,?,?)",
                (gid, i, i * duree, i * duree + duree, label))
        self.con.commit()

    def _empreinte_fixe(self, vecteur):
        rattacher.empreinte = lambda *a, **k: (vecteur, int(vecteur.shape[0]))


class TestEnrolementProprietaire(Base):
    def test_voix_dominante_du_1er_groupe_devient_profil_0(self):
        self._groupe(1)
        v = np.ones(256, dtype="float32")
        self._empreinte_fixe(v)
        r = rattacher.rattacher_au_repertoire(self.con, 1, "x.wav",
                                              trace=False)
        self.assertEqual(r["voix"]["SPEAKER_00"]["statut"], "PROPRIETAIRE_ENROLE")
        self.assertEqual(r["voix"]["SPEAKER_00"]["profil"], config.PROFIL_PROPRIETAIRE)
        # l'empreinte est bien enregistrée POUR LE PROFIL 0
        rows = self.con.execute(
            "SELECT speaker_id FROM speaker_embedding").fetchall()
        self.assertEqual([row[0] for row in rows], [config.PROFIL_PROPRIETAIRE])
        # aucun profil parasite n'a été créé
        ids = [row[0] for row in self.con.execute(
            "SELECT speaker_id FROM speaker")]
        self.assertEqual(ids, [config.PROFIL_PROPRIETAIRE])
        # les tours portent le profil 0
        sid = self.con.execute(
            "SELECT DISTINCT speaker_id FROM turn WHERE group_id=1"
        ).fetchone()[0]
        self.assertEqual(sid, config.PROFIL_PROPRIETAIRE)

    def test_meme_voix_ne_cree_pas_de_profil_au_2e_groupe(self):
        v = np.ones(256, dtype="float32")
        self._empreinte_fixe(v)
        self._groupe(1)
        rattacher.rattacher_au_repertoire(self.con, 1, "x.wav", trace=False)
        self._groupe(2)
        r = rattacher.rattacher_au_repertoire(self.con, 2, "x.wav",
                                              trace=False)
        self.assertEqual(r["voix"]["SPEAKER_00"]["statut"], "MATCH")
        self.assertEqual(r["voix"]["SPEAKER_00"]["profil"], config.PROFIL_PROPRIETAIRE)
        ids = sorted(row[0] for row in self.con.execute(
            "SELECT speaker_id FROM speaker"))
        self.assertEqual(ids, [config.PROFIL_PROPRIETAIRE])


if __name__ == "__main__":
    unittest.main()
