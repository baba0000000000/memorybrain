"""La diarisation dispatchée doit se comporter comme la transcription.

Même contrat (voir hub.py) : sonde préalable, nom de partage, repli local.
Et surtout : le résultat doit être IDENTIQUE quelle que soit la machine —
d'où le filtrage `min_duration` appliqué ici, pas chez le worker.
"""
import unittest
from unittest import mock

from diarization import diarize as dz


class TestDispatchDiarisation(unittest.TestCase):

    def test_pas_de_worker_reste_local(self):
        """Aucun worker -> aucune soumission, et on charge pyannote."""
        with mock.patch.object(dz.hub, "actif", return_value=False), \
             mock.patch.object(dz, "_hub_diariser") as h, \
             mock.patch.object(dz, "charger",
                               side_effect=RuntimeError("pyannote absent")):
            with self.assertRaises(RuntimeError):
                dz.diariser("x.wav")
        h.assert_not_called()

    def test_worker_vivant_utilise_le_hub(self):
        """Le Hub renvoie des LISTES [début, fin, étiquette] : c'est
        _hub_diariser qui les convertit, et on ne charge PAS pyannote."""
        # Témoin d'hôte neutre : `.invalid` est un TLD réservé.
        temoin = "worker.exemple.invalid"
        brut = {"turns": [[0.0, 5.0, "SPEAKER_00"],
                          [5.0, 6.0, "SPEAKER_01"],
                          [6.0, 6.2, "SPEAKER_00"]],   # < 0,4 s : bruit
                "worker": temoin, "device": "cuda"}
        with mock.patch.object(dz.hub, "actif", return_value=True), \
             mock.patch.object(dz.hub, "soumettre", return_value=brut), \
             mock.patch.object(dz, "charger") as ch:
            r = dz.diariser("x.wav")
        ch.assert_not_called()
        self.assertEqual(r["n_tours"], 2)          # le tour de 0,2 s est jeté
        self.assertEqual(r["tours_jetes"], 1)
        self.assertEqual(r["tours"][0]["label"], "SPEAKER_00")
        self.assertEqual(r["device"], "cuda")
        self.assertIn("hub:" + temoin, r["modele"])
        self.assertEqual(r["n_locuteurs"], 2)

    def test_hub_muet_repli_local(self):
        """Le Hub ne rend rien -> on retombe sur pyannote en local."""
        with mock.patch.object(dz.hub, "actif", return_value=True), \
             mock.patch.object(dz, "_hub_diariser", return_value=None), \
             mock.patch.object(dz, "charger",
                               side_effect=RuntimeError("pyannote absent")):
            with self.assertRaises(RuntimeError):
                dz.diariser("x.wav")

    def test_nom_de_partage_pas_chemin_absolu(self):
        """On envoie 'memorybrain-groupes/<fichier>', jamais /home/... .

        C'est ce qui permet au job de tourner AUSSI sur un autre worker : un
        chemin absolu du serveur n'existe pas ailleurs.
        """
        envoye = {}

        class FauxRes:
            def get(self, timeout=None):
                return {"turns": [], "worker": "w"}

        class FauxApp:
            def send_task(self, nom, args=None, kwargs=None):
                envoye["nom"] = nom
                envoye["args"] = args
                return FauxRes()

        import sys
        faux = type("M", (), {"app": FauxApp()})()
        with mock.patch.dict(sys.modules, {"aiinfra_app": faux}):
            dz._hub_diariser("racine/.../groupe_0007.wav")
        self.assertEqual(envoye["nom"], "diarize")
        self.assertEqual(envoye["args"], ["memorybrain-groupes/groupe_0007.wav"])


if __name__ == "__main__":
    unittest.main()