"""Tests du parseur de noms — règle 2 et règle 3.

Lancer :  python3 -m pytest tests/test_naming.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingestion.naming import parse, strip_sync_conflict, is_audio  # noqa: E402


class TestNomsConnus(unittest.TestCase):
    def test_01_generation_1min_avec_millisecondes(self):
        """Format récent : 6 champs. Doit donner l'heure exacte."""
        r = parse("2024-01-15_18-12-27-753_0000.wav")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["generation"], "v6_ms")
        self.assertEqual(r["recorded_at"], "2024-01-15T18:12:27.753000")
        self.assertEqual(r["chunk_index"], 0)
        self.assertEqual(r["duration_guess"], 60.0)

    def test_02_generation_4champs(self):
        """Format ancien : 4 champs, sans millisecondes."""
        r = parse("2024-01-01_08-00-00_0000.wav")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["generation"], "v4")
        self.assertEqual(r["recorded_at"], "2024-01-01T08:00:00")
        self.assertEqual(r["chunk_index"], 0)
        self.assertIsNone(r["duration_guess"], "durée à mesurer, jamais devinée")

    def test_03_index_preserve(self):
        r = parse("2024-01-15_10-00-00-224_0304.wav")
        self.assertEqual(r["chunk_index"], 304)
        self.assertEqual(r["recorded_at"], "2024-01-15T10:00:00.224000")

    def test_04_ordre_chronologique_depuis_le_nom(self):
        """Deux chunks consécutifs : l'ordre du nom = l'ordre du temps."""
        a = parse("2024-01-15_18-12-27-753_0000.wav")
        b = parse("2024-01-15_18-13-27-812_0001.wav")
        self.assertLess(a["recorded_at"], b["recorded_at"])
        ecart = (int(b["recorded_at"][17:19]) - int(a["recorded_at"][17:19])) or 60
        self.assertIn(ecart, (60, 61), "l'écart réel entre deux chunks est ~60 s")


class TestSyncConflict(unittest.TestCase):
    """Des fichiers ont survécu à un conflit Syncthing (règle 3).

    La FORME du nom compte, pas les valeurs :
      <nom>.sync-conflict-AAAAMMJJ-HHMMSS-AAAAAAA.wav
    (date compacte du conflit, puis un code d'appareil de 7 caractères).
    Les témoins ci-dessous sont fabriqués : aucun appareil réel n'est nommé.
    """

    def test_05_conflit_format_reel(self):
        nom = "2024-01-01_08-00-00_0000.sync-conflict-20240101-000000-AAAAAAA.wav"
        r = parse(nom)
        self.assertTrue(r["sync_conflict"], "le suffixe doit être reconnu")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["recorded_at"], "2024-01-01T08:00:00",
                         "l'heure du conflit est bien celle de l'audio")

    def test_06_conflit_avec_millisecondes(self):
        nom = "2024-01-02_08-00-00-841_0494.sync-conflict-20240102-000000-AAAAAAA.wav"
        r = parse(nom)
        self.assertTrue(r["sync_conflict"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["chunk_index"], 494)
        self.assertEqual(r["recorded_at"], "2024-01-02T08:00:00.841000")

    def test_06b_conflit_sur_nom_4_champs(self):
        """Certains conflits portent l'ancien format à 4 champs."""
        nom = "2024-01-03_08-00-00_0133.sync-conflict-20240101-000000-AAAAAAA.wav"
        r = parse(nom)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["generation"], "v4")
        self.assertEqual(r["chunk_index"], 133)

    def test_07_strip_sync_conflict(self):
        nom = "a.sync-conflict-20240101-000000-AAAAAAA"
        self.assertEqual(strip_sync_conflict(nom), ("a", True))
        self.assertEqual(strip_sync_conflict("a"), ("a", False))

    def test_07b_pas_de_confusion_avec_un_nom_normal(self):
        """Un nom sans conflit ne doit PAS être signalé comme conflit."""
        self.assertEqual(strip_sync_conflict("2024-01-01_08-00-00_0000")[1], False)


class TestDoublonsSyncthing(unittest.TestCase):
    """Les copies « --N » : l'audio est identique, le nom d'origine compte."""

    def test_08_copie_doublon(self):
        r = parse("2024-01-01_09-00-00_0012--1.wav")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["duplicate"], 1)
        self.assertEqual(r["chunk_index"], 12, "l'index d'origine est conservé")
        self.assertEqual(r["recorded_at"], "2024-01-01T09:00:00")

    def test_08b_doublon_avec_suffixe_debug(self):
        """Cas réel : ...-fixed--1.wav"""
        r = parse("2024-01-01_10-00-00_0030-fixed--1.wav")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["duplicate"], 1)
        self.assertEqual(r["chunk_index"], 30)


class TestNomsInconnus(unittest.TestCase):
    """Règle 3 : un nom illisible ne doit RIEN faire perdre.

    Ce sont des noms créés à la main : leur date est inconnue et DOIT le
    rester (rien n'est inventé, règle 3).
    """

    def test_08_noms_illisibles_refuses(self):
        for nom in ("test-spike.wav", "test accentué éàç.wav", "dup.wav"):
            r = parse(nom)
            self.assertFalse(r["ok"], nom)
            self.assertEqual(r["recorded_at"], "", "aucune date inventée")

    def test_09_date_impossible(self):
        r = parse("2024-13-45_99-99-99-999_0001.wav")
        self.assertFalse(r["ok"])
        self.assertEqual(r["recorded_at"], "")

    def test_10_suffixe_debug(self):
        """Deux transcripts V1 s'appellent ...-red et ...-fixed."""
        r = parse("2024-01-01_11-00-00_0030-red.wav")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["chunk_index"], 30)
        self.assertIn("debug", r["reason"])

    def test_11_parse_ne_leve_jamais(self):
        """Aucun nom, quelle que soit sa forme, ne doit faire planter."""
        for nom in ("", ".", "..", "x", "a" * 300, "2026", "2024-01-15",
                    "0000-00-00_00-00-00-000_0000.wav", "🎤.wav", None):
            with self.subTest(nom=nom):
                try:
                    r = parse(nom if nom is not None else "")
                    self.assertIn("ok", r)
                except Exception as e:
                    self.fail(f"parse({nom!r}) a levé {e!r} — règle 3 violée")


class TestExtensions(unittest.TestCase):
    def test_12_is_audio(self):
        for n in ("a.wav", "b.ogg", "c.mp3", "d.m4a", "e.ogg", "f.flac"):
            self.assertTrue(is_audio(n), n)
        for n in ("a.md", "b.json", "c.txt", "d"):
            self.assertFalse(is_audio(n), n)


class TestCorpusReel(unittest.TestCase):
    """Le test qui compte : le parseur contre le corpus présent sur le disque.

    Skippé automatiquement si le corpus n'est pas présent, pour que la
    suite reste exécutable sur une autre machine. Aucun seuil ne dit la
    taille du corpus : le contrat est un TAUX de reconnaissance, pas un
    volume — un seuil absolu ferait échouer tout contributeur avec un
    corpus plus petit.
    """

    V1_DONE = Path("corpus-v1/pipeline/done")

    def test_13_taux_de_reconnaissance_du_corpus(self):
        if not self.V1_DONE.exists():
            self.skipTest("corpus V1 absent")
        noms = [p.name for p in self.V1_DONE.iterdir() if is_audio(p.name)]
        self.assertGreater(len(noms), 0, "le dossier du corpus est vide")
        ok = sum(1 for n in noms if parse(n)["ok"])
        # 99,9 % : seuls des fichiers de test manuel doivent échouer
        self.assertGreater(ok / len(noms), 0.999,
                           f"{len(noms) - ok} fichiers non reconnus")

    def test_14_les_conflits_syncthing_sont_tous_recuperes(self):
        """Des enregistrements présents sur le disque mais absents de la
        base V1 sont quand même récupérés (règle 3)."""
        if not self.V1_DONE.exists():
            self.skipTest("corpus V1 absent")
        noms = [p.name for p in self.V1_DONE.iterdir() if "sync-conflict" in p.name]
        self.assertGreater(len(noms), 0, "aucun conflit dans ce corpus")
        perdus = [n for n in noms if not parse(n)["ok"]]
        self.assertEqual(perdus, [], "aucun conflit ne doit être perdu")


if __name__ == "__main__":
    unittest.main(verbosity=2)
