"""Tests de l'étape 5 (alignement) — la partition stricte des mots.

Chaque test protège contre un défaut déjà observé :

- le contrôle « 0 mot perdu » a attrapé UN mot perdu (`ça`) et DEUX mots
  ajoutés. Cause : l'ancien
  découpage laissait deux portions voisines SE PARTAGER un mot, et la
  rustine de dédoublonnage à la fusion mangeait alors de vraies
  répétitions.

Le contrat à tenir : le texte d'un segment est une PARTITION STRICTE —
chaque mot rendu exactement une fois, ni perdu, ni dupliqué.
"""
import pathlib
import sys
import unittest
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from speakers import align  # noqa: E402


def _mots(items):
    return Counter(" ".join(i["texte"] for i in items).split())


class TestPartition(unittest.TestCase):
    def test_segment_decoupe_conserve_chaque_mot(self):
        """Un segment traversé par 3 tours rend tous ses mots, une fois."""
        tours = [(0.0, 2.0, "A"), (2.0, 4.0, "B"), (4.0, 6.0, "A")]
        seg = (0.0, 6.0, "un deux trois quatre cinq six")
        pieces = align.aligner_segment(tours, seg)
        self.assertEqual(_mots(pieces), Counter(seg[2].split()))
        self.assertEqual(sum(_mots(pieces).values()), 6)

    def test_mot_repete_a_la_frontiere_n_est_pas_mange(self):
        """« … ça » suivi de « ça sera… » garde les DEUX « ça ».

        C'est exactement le bug visé : deux morceaux distincts finissaient et
        commençaient par le même mot, et l'ancienne rustine en supprimait un.
        """
        items = [
            {"start": 0.0, "end": 2.0, "texte": "il faut que ça",
             "label": "A", "ratio": 0.5},
            {"start": 2.0, "end": 4.0, "texte": "ça sera la première",
             "label": "A", "ratio": 0.5},
        ]
        fusion = align._fusionner_interventions(items)
        texte = " ".join(f["texte"] for f in fusion)
        self.assertEqual(texte.split().count("ça"), 2)
        self.assertEqual(len(texte.split()), 8)

    def test_aucun_mot_duplique_par_le_decoupage(self):
        """Fenêtres très serrées : pas de mot en double."""
        tours = [(0.0, 1.0, "A"), (1.0, 1.2, "B"), (1.2, 2.0, "A")]
        seg = (0.0, 2.0, "a b c d e f g h")
        pieces = align.aligner_segment(tours, seg)
        self.assertEqual(_mots(pieces), Counter(seg[2].split()))


if __name__ == "__main__":
    unittest.main()
