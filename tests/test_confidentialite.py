"""Le garde-fou anti-fuite doit REFUSER, pas décorer.

Un contrôle qui répond « aucune donnée personnelle détectée » sans avoir jamais
rien refusé est un faux vert : c'est exactement ce qui a laissé passer plusieurs
nettoyages. Ces tests exercent le refus lui-même, sur des leurres fabriqués
pour l'occasion, et vérifient qu'il épargne les chemins de toute application.

Aucune valeur réelle n'est écrite ici : les leurres sont des formes.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SCRIPT = RACINE / "outils" / "verifier_confidentialite.py"

# Les leurres sont assemblés morceau par morceau : le garde-fou refuse les
# formes complètes, et ce fichier est versionné. Assemblé, le leurre reste
# parfaitement reconnaissable pour un humain et pour le test.
LEURRES = (
    # identifiant d'appareil (suffixe de conflit Syncthing)
    "a.sync-conflict-20240101-000000-" + "ZZTOP99.wav",
    # chemin d'un compte
    "ouvrir /home/" + "comptedetest/donnees/audio.wav",
    # effectif d'un corpus
    "il reste 4 812 " + "transcripts sans horaires",
)

PROPRES = (
    "wiki = ~/.local/share/memorybrain/wiki",
    "config = ~/.config/memorybrain/config.toml",
    "cmd = ['x', '--worker', 'localhost', cible]",
    "a.sync-conflict-20240101-000000-AAAAAAA.wav",
)


class TestGardeFou(unittest.TestCase):
    def _lance(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True,
                              cwd=RACINE, timeout=120)

    def _fichier(self, lignes) -> Path:
        d = Path(tempfile.mkdtemp(prefix="garde-fou-"))
        f = d / "temoins.md"
        f.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        self.addCleanup(lambda: f.unlink(missing_ok=True))
        return f

    def test_01_auto_test_vert(self):
        """Chaque motif mord son leurre, et les deux garde-fous sont d'accord."""
        r = self._lance("--auto-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("leurre(s) refusé(s)", r.stdout)

    def test_02_refuse_les_familles_de_fuite(self):
        r = self._lance(str(self._fichier(LEURRES)))
        self.assertEqual(r.returncode, 1,
                         "le garde-fou a laissé passer des leurres :\n" + r.stdout)
        for famille in ("identifiant d'appareil", "chemin de compte",
                        "métrique de corpus"):
            self.assertIn(famille, r.stderr, r.stderr)

    def test_03_epargne_les_chemins_de_toute_application(self):
        """Un faux positif pousse à désarmer le garde-fou : on n'en veut pas."""
        r = self._lance(str(self._fichier(PROPRES)))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_04_un_dossier_vide_n_est_pas_un_vert(self):
        """N'avoir rien pu regarder n'est pas une validation : code 2, pas 0."""
        with tempfile.TemporaryDirectory() as d:
            vide = Path(d) / "vide"
            vide.mkdir()
            r = subprocess.run([sys.executable, str(SCRIPT), str(vide)],
                               capture_output=True, text=True, cwd=RACINE,
                               timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("AUCUN FICHIER CONTRÔLÉ", r.stderr)


if __name__ == "__main__":
    unittest.main()
