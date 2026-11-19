"""Tests de migration — une base ancienne ne doit JAMAIS perdre de données.

C'est le bug réel rencontré : la base de production avait été créée avant
l'ajout de `note` et de `start_time` nullable, et l'ingestion échouait.

Lancer :  python3 -m pytest tests/test_migrate.py
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

# Le schéma VÉRITICAL d'avant : pas de `note`, start_time NOT NULL.
ANCIEN = """
CREATE TABLE chunk (
    chunk_id       INTEGER PRIMARY KEY,
    session_id     INTEGER,
    source_id      INTEGER,
    recorded_at    TEXT NOT NULL,
    chunk_index    INTEGER,
    start_time     REAL NOT NULL DEFAULT 0,
    end_time       REAL,
    duration_sec   REAL,
    original_file  TEXT NOT NULL,
    hash           TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL
);
CREATE TABLE segment (
    segment_id     INTEGER PRIMARY KEY,
    chunk_id       INTEGER NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    idx            INTEGER,
    start_time     REAL,
    end_time       REAL,
    text           TEXT,
    speaker_id     INTEGER
);
"""


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.dossier = Path(tempfile.mkdtemp(prefix="mb_mig_"))
        self.chemin = self.dossier / "vieux.sqlite"

        # on fabrique une base AVEC le vieux schéma + des données
        con = sqlite3.connect(self.chemin)
        con.executescript(ANCIEN)
        con.execute("PRAGMA foreign_keys=ON")
        for i in range(5):
            con.execute(
                "INSERT INTO chunk(recorded_at, chunk_index, start_time,"
                " end_time, duration_sec, original_file, hash, status,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"2024-01-15T09:0{i}:00", i, i * 60.0, i * 60.0 + 60, 60.0,
                 f"/x/{i}.wav", f"h{i}", "transcribed", "2024-01-15"))
            cid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            for j in range(3):
                con.execute(
                    "INSERT INTO segment(chunk_id, idx, start_time, end_time, text)"
                    " VALUES(?,?,?,?,?)",
                    (cid, j, i * 60.0 + j, i * 60.0 + j + 1, f"texte {i}-{j}"))
        con.commit()
        con.close()

    def tearDown(self):
        for p in self.dossier.glob("*"):
            p.unlink()
        self.dossier.rmdir()

    def _avant(self):
        con = sqlite3.connect(self.chemin)
        n_ch = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
        n_seg = con.execute("SELECT COUNT(*) FROM segment").fetchone()[0]
        col = {r[1]: r for r in con.execute("PRAGMA table_info(chunk)")}
        con.close()
        return n_ch, n_seg, col

    def test_01_migration_ne_perd_ni_chunk_ni_segment(self):
        n_ch, n_seg, avant = self._avant()
        self.assertNotIn("note", avant)
        self.assertEqual(avant["start_time"][3], 1, "start_time doit être NOT NULL avant")

        db.connect(self.chemin)

        con = sqlite3.connect(self.chemin)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0], n_ch,
                         "des chunks ont été perdus")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM segment").fetchone()[0], n_seg,
                         "DES TRANSCRIPTS ONT ÉTÉ PERDUS (cascade)")
        con.close()

    def test_02_colonne_note_ajoutee(self):
        db.connect(self.chemin)
        col = {r[1] for r in sqlite3.connect(self.chemin)
               .execute("PRAGMA table_info(chunk)")}
        self.assertIn("note", col)

    def test_03_start_time_devient_nullable(self):
        db.connect(self.chemin)
        col = {r[1]: r for r in sqlite3.connect(self.chemin)
               .execute("PRAGMA table_info(chunk)")}
        self.assertEqual(col["start_time"][3], 0,
                         "start_time doit accepter NULL (règle 3)")

    def test_04_insertion_nullable_apres_migration(self):
        """Le but de la migration : un nom illisible entre enfin."""
        con = db.connect(self.chemin)
        cur = con.execute(
            "INSERT INTO chunk(session_id, source_id, recorded_at, chunk_index,"
            " start_time, end_time, duration_sec, original_file, hash, status,"
            " note, created_at) VALUES(NULL,NULL,'1970-01-01T00:00:00',NULL,"
            "NULL,NULL,NULL,'/x/inconnu.wav','hX','pending','date inconnue','n')")
        self.assertTrue(cur.lastrowid)
        con.close()

    def test_05_migration_idempotente(self):
        db.connect(self.chemin)
        changes = db.connect(self.chemin) and None
        con = db.connect(self.chemin)
        self.assertEqual(db.migrate(con), [], "une 2e migration ne doit rien faire")
        con.close()

    def test_06_hash_unique_conserve(self):
        """La règle 19 doit SURVIVRE à la migration : un hash déjà
        présent doit toujours être refusé."""
        db.connect(self.chemin)
        con = sqlite3.connect(self.chemin)
        # h0 existe déjà (mis en place par setUp) : l'insertion doit échouer
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO chunk(recorded_at, original_file, hash, created_at)"
                " VALUES('2024-01-15T10:00:00','/x/dup.wav','h0','n')")
        # un hash nouveau passe
        con.execute(
            "INSERT INTO chunk(recorded_at, original_file, hash, created_at)"
            " VALUES('2024-01-15T10:00:00','/x/neuf.wav','h_neuf','n')")
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM chunk WHERE hash='h_neuf'"
                        ).fetchone()[0], 1)
        con.close()

    def test_07_preservation_du_texte(self):
        db.connect(self.chemin)
        con = sqlite3.connect(self.chemin)
        textes = [r[0] for r in con.execute("SELECT text FROM segment ORDER BY segment_id")]
        self.assertEqual(len(textes), 15)
        self.assertIn("texte 4-2", textes)
        con.close()

    def test_08_base_neuf_pas_touchée(self):
        """Une base déjà à jour ne doit subir aucune modification."""
        neuf = self.dossier / "neuf.sqlite"
        con = db.connect(neuf)
        con.close()
        self.assertEqual(db.migrate(db.connect(neuf)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)