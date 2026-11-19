"""Tests de l'ingestion — règle 2, 3, 17, 19.

Lancer :  python3 -m pytest tests/test_ingest.py
"""
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402
from ingestion import ingest, naming  # noqa: E402


def wav_avec_duree(chemin: Path, secondes: float = 1.0, taux: int = 16000,
                   particularite: bytes = b"") -> None:
    """Crée un WAV.

    `particularite` rend chaque fichier DIFFÉRENT bit à bit. Sans ça, deux
    appels produisent le même hash et l'ingestion les déduplique — ce qui
    est le comportement correct (règle 19), mais pas ce que ces tests
    veulent vérifier. Les vrais enregistrements ont, eux, des contenus
    différents.
    """
    import math
    import struct
    n = int(taux * secondes)
    # une sinusoïde d'une fréquence dépendant de `particularite` : deux
    # fichiers sont donc distincts SANS être du bruit aléatoire (déterministe)
    freq = 220 + (particularite[0] if particularite else 0)
    echantillons = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * (i / taux))))
        for i in range(n))
    with wave.open(str(chemin), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taux)
        w.writeframes(echantillons)


class TestIngestion(unittest.TestCase):
    def setUp(self):
        self.dossier = Path(tempfile.mkdtemp(prefix="mb_ingest_"))
        self.db_path = self.dossier / "t.sqlite"
        self.con = db.connect(self.db_path)
        db.set_schema_version(self.con)
        self.src = ingest.register_source(self.con)

    def tearDown(self):
        self.con.close()
        for p in self.dossier.rglob("*"):
            if p.is_file():
                p.unlink()
        self.dossier.rmdir()

    def chunk(self, **kw):
        r = dict(self.con.execute("SELECT * FROM chunk").fetchone())
        return r

    # ---------- règle 2 : position absolue ----------
    def test_01_position_absolue_depuis_le_timestamp(self):
        """start_time = l'heure réelle du chunk, lue dans le nom.

        PAS index x 60 : le compteur d'index redémarre à 0 à chaque
        session (l'écart heure - index*60 peut atteindre plusieurs heures).
        """
        from datetime import datetime
        # (index, heure_réeLLE dans le nom) : l'index 108 est horodaté 09:08,
        # pas 108 minutes après le début — c'est tout le sujet du test
        attendus = [
            (0,   "2024-01-15_09-00-00-000_0000.wav"),
            (1,   "2024-01-15_09-01-00-000_0001.wav"),
            (7,   "2024-01-15_09-07-00-000_0007.wav"),
            (108, "2024-01-15_09-08-00-000_0108.wav"),
        ]
        for idx, nom in attendus:
            iso = (f"2024-01-15T{nom[11:13]}:{nom[14:16]}:00")
            f = self.dossier / nom
            wav_avec_duree(f, 1.0, particularite=bytes([idx]))
            cid, st = ingest.ingest_one(self.con, f, self.src)
            self.assertEqual(st, "new")
            row = self.con.execute(
                "SELECT start_time, recorded_at FROM chunk WHERE chunk_id=?",
                (cid,)).fetchone()
            self.assertEqual(row["recorded_at"], iso)
            self.assertEqual(row["start_time"],
                             datetime.fromisoformat(iso).timestamp())

    def test_02_index_ne_sert_pas_de_duree(self):
        """Le décalage : index*60 s n'est pas l'heure du chunk.

        Deux chunks du témoin portent les index 102 et 118 (soit 16 min
        d'écart si on calculait index*60) mais leurs noms disent 20 min
        et 30 s. Le second serait placé au mauvais endroit.
        """
        from datetime import datetime
        f1 = self.dossier / "2024-01-01_10-00-00-000_0102.wav"
        f2 = self.dossier / "2024-01-01_10-20-30-000_0118.wav"
        wav_avec_duree(f1, 1.0, particularite=b"\x01")
        wav_avec_duree(f2, 1.0, particularite=b"\x02")
        c1, _ = ingest.ingest_one(self.con, f1, self.src)
        c2, _ = ingest.ingest_one(self.con, f2, self.src)
        s1 = self.con.execute("SELECT start_time FROM chunk WHERE chunk_id=?",
                              (c1,)).fetchone()[0]
        s2 = self.con.execute("SELECT start_time FROM chunk WHERE chunk_id=?",
                              (c2,)).fetchone()[0]
        # l'écart attendu se DÉDUIT des noms ci-dessus : rien n'est recopié
        ecart = (datetime.fromisoformat("2024-01-01T10:20:30")
                 - datetime.fromisoformat("2024-01-01T10:00:00")).total_seconds()
        self.assertAlmostEqual(s2 - s1, ecart, delta=1,
                               msg="l'écart entre les deux chunks est perdu")
        # l'ancien calcul aurait donne (118 - 102) * 60 s, pas l'écart des noms
        self.assertNotAlmostEqual(s2 - s1, (118 - 102) * 60.0, delta=1)

    # ---------- règle 19 : idempotence ----------
    def test_03_rejeu_ne_duplique_pas(self):
        f = self.dossier / "2024-01-15_09-00-00-000_0000.wav"
        wav_avec_duree(f, 1.0)
        for _ in range(3):
            ingest.ingest_one(self.con, f, self.src)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0], 1)

    def test_04_doublon_syncthing_detecte_par_hash(self):
        """--1 et --2 ont le MEME contenu : un seul chunk (règle 19)."""
        base = "2024-01-01_08-00-00_0000"
        for nom in (f"{base}.wav", f"{base}--1.wav", f"{base}--2.wav"):
            wav_avec_duree(self.dossier / nom, 1.0)
        ids = set()
        for nom in (f"{base}.wav", f"{base}--1.wav", f"{base}--2.wav"):
            cid, st = ingest.ingest_one(self.con, self.dossier / nom, self.src)
            ids.add(cid)
        self.assertEqual(len(ids), 1, "trois copies = un seul chunk")

    def test_05_scan_idempotent(self):
        for i in range(5):
            wav_avec_duree(self.dossier / f"2024-01-15_09-00-0{i}-000_000{i}.wav",
                           1.0, particularite=bytes([i]))
        r1 = ingest.scan(self.con, self.dossier, self.src)
        r2 = ingest.scan(self.con, self.dossier, self.src)
        self.assertEqual(r1["new"], 5)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(r2["known"], 5)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0], 5)

    # ---------- règle 3 : rien n'est perdu ----------
    def test_06_nom_illisible_mais_fichier_conserve(self):
        """Un nom non reconnu entre quand même, sans date inventée."""
        f = self.dossier / "enregistrement-inconnu.wav"
        wav_avec_duree(f, 1.0, particularite=b"\xa6")
        cid, st = ingest.ingest_one(self.con, f, self.src)
        self.assertEqual(st, "new", "le fichier ne doit PAS être jeté")
        row = self.con.execute(
            "SELECT recorded_at, note FROM chunk WHERE chunk_id=?", (cid,)).fetchone()
        self.assertEqual(row["recorded_at"], "1970-01-01T00:00:00",
                         "placeholder = aucune date inventée")
        self.assertIn("non reconnu", row["note"])

    def test_07_conflit_syncthing_trace_dans_la_base(self):
        """Un fichier arrivé en conflit Syncthing : la trace reste visible."""
        f = self.dossier / "2024-01-03_08-00-00_0115.sync-conflict-20240101-000000-AAAAAAA.wav"
        wav_avec_duree(f, 1.0, particularite=b"\x07")
        cid, st = ingest.ingest_one(self.con, f, self.src)
        row = self.con.execute(
            "SELECT note, original_file FROM chunk WHERE chunk_id=?", (cid,)).fetchone()
        self.assertEqual(st, "new")
        self.assertIn("sync-conflict", row["note"],
                      "le conflit Syncthing doit rester identifiable")
        self.assertIn("sync-conflict", row["original_file"],
                      "le vrai nom du fichier est conservé")

    def test_08_ingest_ne_leve_jamais(self):
        """Un fichier corrompu ne doit pas faire tomber le scan."""
        (self.dossier / "2024-01-15_09-00-00-000_0000.wav").write_bytes(b"pas un wav")
        (self.dossier / "note.txt").write_text("rien")
        r = ingest.scan(self.con, self.dossier, self.src)
        self.assertEqual(r["total"], 1, "le .txt est ignoré")
        self.assertEqual(r["skipped"], 0)
        row = self.con.execute("SELECT duration_sec FROM chunk").fetchone()
        self.assertIsNone(row["duration_sec"], "durée illisible = None, pas 0")

    def test_09_dossier_absent(self):
        r = ingest.scan(self.con, self.dossier / "n_existe_pas", self.src)
        self.assertIn("erreur", r)
        self.assertEqual(r["total"], 0)

    # ---------- règle 17 : l'audio ne bouge jamais ----------
    def test_10_audio_deplace(self):
        """L'ingestion ne doit RIEN déplacer ni supprimer (§17)."""
        f = self.dossier / "2024-01-15_09-00-00-000_0000.wav"
        wav_avec_duree(f, 1.0, particularite=b"\x10")
        avant = sorted(p.name for p in self.dossier.iterdir())
        ingest.ingest_one(self.con, f, self.src)
        self.assertEqual(sorted(p.name for p in self.dossier.iterdir()), avant,
                         "l'ingestion a touché aux fichiers !")

    def test_11_source_idempotente(self):
        a = ingest.register_source(self.con)
        b = ingest.register_source(self.con)
        self.assertEqual(a, b)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM source").fetchone()[0], 1)

    # ---------- durées multi-formats ----------
    def test_12_duree_wav(self):
        f = self.dossier / "2024-01-15_09-00-00-000_0000.wav"
        wav_avec_duree(f, 2.5, particularite=b"\x12")
        d = ingest.audio_duration(f)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d or 0.0, 2.5, places=2)

    def test_13_duree_ogg(self):
        """Une partie du corpus est en OGG/Opus, pas WAV."""
        f = self.dossier / "2024-01-15_16-00-00_0000.ogg"
        wav_avec_duree(f.with_suffix(".wav"), 3.0)
        import shutil
        shutil.copy(f.with_suffix(".wav"), f)
        d = ingest.audio_duration(f)
        self.assertIsNotNone(d, "l'Ogg doit être lu aussi")
        self.assertAlmostEqual(d or 0.0, 3.0, places=2)

    def test_14_fichier_illisible_duree_none(self):
        f = self.dossier / "casse.wav"
        f.write_bytes(b"RIFFxxxxWAVEjunk")
        self.assertIsNone(ingest.audio_duration(f))


if __name__ == "__main__":
    unittest.main(verbosity=2)