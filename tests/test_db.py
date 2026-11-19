"""Tests du schéma V2 — règle 3, 4, 17, 19.

Lancer :  python3 -m pytest tests/test_db.py
Aucune dépendance externe : unittest de la stdlib.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mb_db_")) / "t.sqlite"
        self.con = db.connect(self.tmp)
        db.set_schema_version(self.con)

    def tearDown(self):
        self.con.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.tmp) + suffix)
            if p.exists():
                p.unlink()

    def tables(self):
        return {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    # ---------- structure ----------
    def test_01_tables_presentes(self):
        attendu = {"source", "session", "chunk", "artifact",
                   "segment", "pipeline_step", "speaker", "meta"}
        self.assertTrue(attendu <= self.tables(), attendu - self.tables())

    def test_02_schema_idempotente(self):
        """Relire le schéma ne doit ni échouer ni rien casser (§19)."""
        db.connect(self.tmp).executescript(db.DDL)
        db.set_schema_version(self.con)
        self.assertTrue({"chunk", "segment"} <= self.tables())

    def test_03_version_schema_enregistree(self):
        v = self.con.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(v, db.SCHEMA_VERSION)

    # ---------- règle 3 : l'incertain ne bloque rien ----------
    def test_04_chunk_sans_session_ni_source_known(self):
        """§3/§8 : session_id NULL ne doit pas empêcher l'enregistrement."""
        self.con.execute(
            "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
            " VALUES(?,?,?,?)",
            ("2024-01-15T10:00:00", "a.wav", "h1", "now"))
        self.con.commit()
        row = self.con.execute("SELECT session_id, source_id FROM chunk").fetchone()
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["source_id"])

    def test_05_segment_sans_locuteur_known(self):
        """speaker_id NULL = UNKNOWN, la ligne doit rester lisible."""
        self.con.execute(
            "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
            " VALUES(?,?,?,?)", ("2024-01-15T10:00:00", "a.wav", "h1", "now"))
        self.con.execute(
            "INSERT INTO segment(chunk_id,idx,start_time,end_time,text)"
            " VALUES(1,0,0.0,4.16,'bonjour')")
        self.con.commit()
        row = self.con.execute("SELECT speaker_id, text FROM segment").fetchone()
        self.assertIsNone(row["speaker_id"])
        self.assertEqual(row["text"], "bonjour")

    def test_06_type_source_unknown_par_defaut(self):
        self.con.execute(
            "INSERT INTO source(kind,label,first_seen) VALUES('UNKNOWN','x','n')")
        row = self.con.execute("SELECT kind FROM source").fetchone()
        self.assertEqual(row["kind"], "UNKNOWN")

    # ---------- règle 4 / §4 : profil vocal ≠ nom ----------
    def test_07_renommer_un_profil_conserve_son_id(self):
        self.con.execute(
            "INSERT INTO speaker(name,status,created_at)"
            " VALUES('speaker_0001','UNKNOWN','n')")
        sid = self.con.execute(
            "SELECT speaker_id FROM speaker WHERE name='speaker_0001'").fetchone()[0]
        self.con.execute(
            "UPDATE speaker SET name='Locuteur 1', status='confirmed' WHERE speaker_id=?",
            (sid,))
        row = self.con.execute(
            "SELECT speaker_id, status FROM speaker WHERE name='Locuteur 1'").fetchone()
        self.assertEqual(row["speaker_id"], sid, "l'ID vocal ne doit pas bouger")
        self.assertEqual(row["status"], "confirmed")

    # ---------- règle 19 : idempotence ----------
    def test_08_hash_unique_refuse_le_doublon(self):
        self.con.execute(
            "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
            " VALUES(?,?,?,?)", ("2024-01-15T10:00:00", "a.wav", "same", "n"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
                " VALUES(?,?,?,?)", ("2024-01-15T10:01:00", "a.wav", "same", "n"))

    def test_09_etape_rejouee_ne_duplique_pas(self):
        self.con.execute(
            "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
            " VALUES(?,?,?,?)", ("2024-01-15T10:00:00", "a.wav", "h1", "n"))
        for _ in range(3):
            self.con.execute(
                "INSERT OR IGNORE INTO pipeline_step(chunk_id,step,status,model,pipeline_version)"
                " VALUES(1,'transcription','done','faster-whisper','2.0.0')")
        n = self.con.execute(
            "SELECT COUNT(*) FROM pipeline_step WHERE chunk_id=1").fetchone()[0]
        self.assertEqual(n, 1)

    def test_10_artifact_unique_par_type(self):
        self.con.execute(
            "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
            " VALUES(?,?,?,?)", ("2024-01-15T10:00:00", "a.wav", "h1", "n"))
        self.con.execute(
            "INSERT INTO artifact(chunk_id,kind,path,created_at)"
            " VALUES(1,'transcript','t.md','n')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO artifact(chunk_id,kind,path,created_at)"
                " VALUES(1,'transcript','autre.md','n')")

    def test_11_cascade_chunk_supprime_segments(self):
        self.con.execute(
            "INSERT INTO chunk(recorded_at,original_file,hash,created_at)"
            " VALUES(?,?,?,?)", ("2024-01-15T10:00:00", "a.wav", "h1", "n"))
        self.con.execute(
            "INSERT INTO segment(chunk_id,idx,start_time,end_time,text)"
            " VALUES(1,0,0.0,1.0,'x')")
        self.con.commit()
        self.con.execute("DELETE FROM chunk WHERE chunk_id=1")
        self.con.commit()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM segment").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
