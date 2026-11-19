"""Tests des sessions.

Lancer :  python3 -m pytest tests/test_sessions.py
"""
import sys
import tempfile
import unittest
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
from sessions import sessions  # noqa: E402


def iso(base: datetime, secondes: float) -> str:
    return (base + timedelta(seconds=secondes)).isoformat(timespec="seconds")


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.dossier = Path(tempfile.mkdtemp(prefix="mb_sess_"))
        self.con = db.connect(self.dossier / "t.sqlite")
        db.set_schema_version(self.con)
        self.con.execute(
            "INSERT INTO source(kind, label, uri, first_seen)"
            " VALUES('microphone','Téléphone','test',datetime('now'))")
        self.con.commit()
        self.base = datetime(2024, 1, 15, 9, 0, 0)

    def tearDown(self):
        self.con.close()

    def _chunks(self, ecarts: Sequence[float]) -> list[int]:
        ids = []
        t = 0.0
        for i, e in enumerate(ecarts):
            t += e
            ts = iso(self.base, t)
            self.con.execute(
                "INSERT INTO chunk(session_id, source_id, recorded_at,"
                " chunk_index, start_time, end_time, duration_sec,"
                " original_file, hash, status, note, created_at)"
                " VALUES(NULL,1,?,?,?,?,?,?,?,?,NULL,datetime('now'))",
                (ts, i, datetime.fromisoformat(ts).timestamp(),
                 datetime.fromisoformat(ts).timestamp() + 60, 60.0,
                 f"/x/{i}.wav", f"h{i}", "pending"))
            ids.append(self.con.execute(
                "SELECT last_insert_rowid()").fetchone()[0])
        self.con.commit()
        return ids

    # ---------- la règle ----------
    def test_01_ecart_60s_meme_session(self):
        """Le cas réel le plus fréquent : 1 chunk/minute."""
        self._chunks([60] * 10)
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["sessions"], 1, "10 chunks à 60 s = UNE session")

    def test_02_ecart_300s_meme_session(self):
        """Ancienne génération : chunks de 5 min, toujours en continu."""
        self._chunks([300] * 6)
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["sessions"], 1, "6 chunks à 300 s = UNE session")

    def test_03_ecart_700s_nouvelle_session(self):
        """Plus de 10 min : le téléphone s'est arrêté."""
        self._chunks([60, 60, 700, 60, 60])
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["sessions"], 2)

    def test_04_frontiere_600s(self):
        """600 s exactement : dans la même session (seuil <= 600)."""
        self._chunks([60, 600])
        self.assertEqual(sessions.build_sessions(self.con)["sessions"], 1)

    def test_05_frontiere_601s(self):
        self._chunks([60, 601])
        self.assertEqual(sessions.build_sessions(self.con)["sessions"], 2)

    def test_06_mixte_plusieurs_generations(self):
        """Le cas réel : 60 s puis 300 s, sans interruption."""
        self._chunks([60, 60, 60, 300, 300, 300])
        self.assertEqual(sessions.build_sessions(self.con)["sessions"], 1,
                         "changer de génération de chunks ≠ nouvelle session")

    # ---------- les cas dégradés ----------
    def test_07_session_dun_seul_chunk(self):
        """Un chunk seul : on ne sait PAS si c'est une session (règle 4)."""
        self._chunks([0])
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["sessions"], 1)
        note = self.con.execute("SELECT note FROM session").fetchone()[0]
        self.assertIn("un seul chunk", note,
                      "l'incertitude doit être écrite, pas cachée")

    def test_08_chunk_sans_position_reste_orphelin(self):
        """Nom illisible : pas de position, donc pas de session (règle 3)."""
        self.con.execute(
            "INSERT INTO chunk(session_id, source_id, recorded_at,"
            " chunk_index, start_time, end_time, duration_sec,"
            " original_file, hash, status, note, created_at)"
            " VALUES(NULL,1,'1970-01-01T00:00:00',NULL,NULL,NULL,NULL,"
            "'/x/inconnu.wav','hinconnu','pending','date inconnue',datetime('now'))")
        self._chunks([60, 60])
        self.con.commit()
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["orphelins"], 1,
                         "le chunk sans date doit rester orphelin, pas perdu")
        self.assertEqual(r["sessions"], 1)

    def test_09_session_trop_longue_coupee(self):
        """Plus de 24 h d'affilée = on a manqué des données, pas une session."""
        ecarts = [60] * 30 + [70000] + [60] * 5
        self._chunks(ecarts)
        r = sessions.build_sessions(self.con)
        self.assertGreaterEqual(r["sessions"], 2)

    # ---------- règle 19 ----------
    def test_10_rejeu_idempotent(self):
        self._chunks([60] * 8 + [900] + [60] * 4)
        r1 = sessions.build_sessions(self.con)
        r2 = sessions.build_sessions(self.con)
        self.assertEqual(r1["sessions"], r2["sessions"], "rejeu = doublons")
        self.assertEqual(r2["orphelins"], 0)

    def test_11_tous_les_chunks_rattaches(self):
        self._chunks([60] * 5 + [900] + [60] * 5)
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["chunks"], 11)
        self.assertEqual(r["orphelins"], 0, "un chunk s'est perdu")

    def test_12_base_vide(self):
        r = sessions.build_sessions(self.con)
        self.assertEqual(r["sessions"], 0)

    # ---------- restitution ----------
    def test_13_resume_chronologique(self):
        self._chunks([60] * 3)
        sessions.build_sessions(self.con)
        summ = sessions.session_summary(self.con)
        self.assertEqual(len(summ), 1)
        self.assertEqual(summ[0]["chunks"], 3)
        self.assertLessEqual(summ[0]["started_at"], summ[0]["ended_at"])

    def test_14_timeline_avec_segments(self):
        ids = self._chunks([60, 60])
        sessions.build_sessions(self.con)
        sid = self.con.execute(
            "SELECT session_id FROM chunk WHERE chunk_id=?", (ids[0],)).fetchone()[0]
        self.con.execute(
            "INSERT INTO segment(chunk_id, idx, start_time, end_time, text,"
            " speaker_id) VALUES(?,0,?,?,'bonjour',NULL)",
            (ids[0], 60.0, 61.0))
        self.con.commit()
        tl = sessions.session_timeline(self.con, sid)
        self.assertEqual(len(tl["chunks"]), 2)
        self.assertEqual(tl["chunks"][0]["segments"][0]["text"], "bonjour")
        # ordre chronologique garanti
        starts = [c["start_time"] for c in tl["chunks"]]
        self.assertEqual(starts, sorted(starts))

    def test_15_session_inexistante(self):
        self.assertEqual(sessions.session_timeline(self.con, 999), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)