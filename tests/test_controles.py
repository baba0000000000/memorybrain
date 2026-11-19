"""Tests des contrôles par étape (§27).

Objectif : prouver qu'un contrôle DÉTECTE un vrai défaut, pas seulement
qu'il répond « OK » sur une base vide. Chaque test casse volontairement
la donnée et exige un rapport rouge.

Lancer :  python3 -m pytest tests/test_controles.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
from verification import controles  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mb_ctrl_"))
        self.tmp = self.tmpdir / "t.sqlite"
        self.con = db.connect(self.tmp)
        db.set_schema_version(self.con)   # crée la table meta (§27)
        self.con.execute(
            "INSERT INTO source(source_id,kind,label,uri,first_seen)"
            " VALUES(1,'TEST','t','x','2024-01-01')")
        self.con.execute(
            "INSERT INTO session(session_id,source_id,created_at)"
            " VALUES(1,1,'2024-01-01')")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for s in ("", "-wal", "-shm"):
            p = Path(str(self.tmp) + s)
            if p.exists():
                p.unlink()

    def _chunk(self, i, dur=60.0):
        self.con.execute(
            "INSERT INTO chunk(chunk_id,session_id,source_id,recorded_at,chunk_index,"
            "start_time,end_time,duration_sec,original_file,hash,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, 1, 1, "2024-01-01", i, 0.0, dur, dur,
             f"c{i}.wav", f"h{i}", "ok", "2024-01-01"))

    def _groupe_5(self, gid=1):
        """Un groupe CORRECT de 5 chunks de 60 s (WAV présents)."""
        wav = self.tmpdir / f"g{gid}.wav"
        wav.write_bytes(b"RIFF")
        # FK ON : les chunks, puis le groupe, PUIS les membres.
        for k in range(5):
            self._chunk(100 + k)
        self.con.execute(
            "INSERT INTO chunk_group(group_id,session_id,taille,n_chunks,start_time,"
            "end_time,duration_sec,audio_path,fingerprint,statut,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (gid, 1, 5, 5, 0.0, 300.0, 300.0, str(wav), "fp", "ok", "2024-01-01"))
        off = 0.0
        for k in range(5):
            self.con.execute(
                "INSERT INTO group_member(group_id,chunk_id,rang,offset_sec)"
                " VALUES(?,?,?,?)", (gid, 100 + k, k, off))
            off += 60.0
        self.con.commit()

    def _meta(self, etape, gid, val="ok"):
        self.con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (f"etape:{etape}:{gid}", val))
        self.con.commit()


class TestGroupement(Base):
    def test_groupe_correct_passe(self):
        self._groupe_5()
        r = controles.controler_groupement(self.con)
        self.assertTrue(r["ok"], r["problemes"])

    def test_groupe_a_4_chunks_est_rouge(self):
        self._groupe_5()
        self.con.execute("DELETE FROM group_member WHERE group_id=1 AND rang=4")
        self.con.commit()
        r = controles.controler_groupement(self.con)
        self.assertFalse(r["ok"])
        self.assertTrue(any("incorrects" in p for p in r["problemes"]))

    def test_wav_absent_est_rouge(self):
        self._groupe_5()
        (self.tmpdir / "g1.wav").unlink()
        r = controles.controler_groupement(self.con)
        self.assertFalse(r["ok"])
        self.assertTrue(any("WAV absent" in p for p in r["problemes"]))


class TestNonFait(Base):
    def test_etape_non_lancee_nest_pas_un_defaut(self):
        """Le point clé : « pas encore fait » ne doit PAS être un rouge."""
        self._groupe_5()
        r = controles.controler_transcription(self.con)
        self.assertTrue(r["ok"])
        self.assertEqual(r["non_faits"], 1)
        self.assertEqual(r["faits"], 0)


class TestAlignement(Base):
    def test_perte_de_mots_est_rouge(self):
        """LE contrôle : du texte présent sans tours = mots perdus."""
        self._groupe_5()
        self.con.execute(
            "INSERT INTO segment(chunk_id,idx,start_time,end_time,text)"
            " VALUES(100,0,0.0,5.0,'bonjour le monde ceci est un test')")
        self.con.commit()
        self._meta("alignement", 1)
        r = controles.controler_alignement(self.con)
        self.assertFalse(r["ok"])
        self.assertGreater(r["mesures"][1]["mots_perdus"], 0)


class TestExtraction(Base):
    def _item(self, gid, llm):
        self.con.execute(
            "INSERT INTO extraction(group_id,kind,texte,chemin,llm,created_at)"
            " VALUES(?,?,?,?,?,?)", (gid, "IDEE", "x", None, llm, "2024-01-01"))
        self.con.commit()

    def _classe(self, gid, classe):
        self.con.execute(
            "INSERT INTO classification(group_id,classe,created_at)"
            " VALUES(?,?,?)", (gid, classe, "2024-01-01"))
        self.con.commit()

    def test_media_avec_item_est_rouge(self):
        self._groupe_5()
        self._classe(1, "MEDIA")
        self._meta("extraction", 1)
        self._item(1, "chocolatine-2-4b")
        r = controles.controler_extraction(self.con)
        self.assertFalse(r["ok"])
        self.assertTrue(any("garde-fou" in p for p in r["problemes"]))

    def test_mauvais_modele_est_rouge(self):
        self._groupe_5()
        self._classe(1, "PERSONNEL")
        self._meta("extraction", 1)
        self._item(1, "gemma-3-4b-it")
        r = controles.controler_extraction(self.con)
        self.assertFalse(r["ok"])
        self.assertTrue(any("gemma" in p for p in r["problemes"]))

    def test_groupe_sain_passe(self):
        self._groupe_5()
        self._classe(1, "PERSONNEL")
        self._meta("extraction", 1)
        self._item(1, "chocolatine-2-4b")
        r = controles.controler_extraction(self.con)
        self.assertTrue(r["ok"], r["problemes"])


if __name__ == "__main__":
    unittest.main()
