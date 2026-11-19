"""Tests de la détection des MARQUEURS ORAUX (étage 1).

Le locuteur annonce lui-même à voix haute l'ouverture ou la fermeture
d'une idée, ou demande d'ajouter une tâche. Un marqueur est un signal
EXPLICITE : il doit primer sur la classification et permettre d'extraire
une idée d'un groupe même classé UNKNOWN.

Défaut d'origine : une consigne dictée en clair n'était pas extraite,
faute de mots-clés reconnus.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from extraction import marqueurs  # noqa: E402


class TestMarqueurs(unittest.TestCase):
    def test_bloc_idee_simple(self):
        t = "début de l'idée on devrait ranger les notes par projet fin de l'idée"
        r = marqueurs.extraire(t)
        self.assertEqual(len(r["idees"]), 1)
        self.assertEqual(r["idees"][0]["texte"],
                         "on devrait ranger les notes par projet")

    def test_apostrophe_courbe_et_espaces(self):
        t = "début de l’idée  garder   le ton du propriétaire  fin de l’idée"
        r = marqueurs.extraire(t)
        self.assertEqual(len(r["idees"]), 1)
        self.assertIn("garder", r["idees"][0]["texte"])

    def test_todo_variante_avec_s(self):
        # variante avec le « s » de « rajoutes »
        t = ("rajoutes comme tâche d'appeler le plombier"
             "\n\nAprès, autre chose.")
        r = marqueurs.extraire(t)
        self.assertEqual(len(r["todos"]), 1)
        self.assertIn("le plombier", r["todos"][0]["texte"])
        # le paragraphe suivant n'est pas aspiré
        self.assertNotIn("Après", r["todos"][0]["texte"])

    def test_debut_sans_fin_prend_jusquau_bout(self):
        t = "début de l'idée une idée jamais fermée par le propriétaire"
        r = marqueurs.extraire(t)
        self.assertEqual(len(r["idees"]), 1)
        self.assertIn("jamais fermée", r["idees"][0]["texte"])

    def test_aucun_marqueur(self):
        t = "une phrase quelconque sans rien d'annonce"
        self.assertFalse(marqueurs.a_des_marqueurs(t))
        r = marqueurs.extraire(t)
        self.assertEqual(r, {"idees": [], "todos": []})

    def test_plusieurs_blocs(self):
        t = ("début de l'idée première fin de l'idée bla bla "
             "début de l'idée deuxième fin de l'idée")
        r = marqueurs.extraire(t)
        self.assertEqual([b["texte"] for b in r["idees"]],
                         ["première", "deuxième"])

    def test_description_du_systeme_nest_pas_une_tache(self):
        # Faux positif : le locuteur DÉCRIT un mécanisme, il ne
        # donne pas de consigne. « nouvelle tâche » a été retiré pour ça.
        t = ("le programme crée une commande s'il y a une nouvelle "
             "tâche : c'est une description du mécanisme.")
        self.assertFalse(marqueurs.a_des_marqueurs(t))

    def test_transcript_complet(self):
        """Un transcript diarisé complet doit livrer la tâche dictée."""
        t = ("**Locuteur 1** : on parlait du déménagement\n\n"
             "**Locuteur 2** : oui\n\n"
             "**Locuteur 1** : ajoute une tâche : appeler le plombier\n")
        r = marqueurs.extraire(t)
        self.assertEqual(len(r["todos"]), 1)
        self.assertIn("le plombier", r["todos"][0]["texte"].lower())


if __name__ == "__main__":
    unittest.main()
