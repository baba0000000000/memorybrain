"""Le dispatch vers le Compute Hub ne doit JAMAIS bloquer la chaîne.

Cas rencontré : le Hub a bloqué la V2, un worker recevait des jobs dont il ne
pouvait pas lire le fichier, et le job restait « active » pour toujours pendant
que tout paraissait normal. Ces tests verrouillent les trois comportements qui
empêchent que ça recommence.
"""
import unittest
from unittest import mock

from transcription import transcribe_group as tg


class TestDispatchHub(unittest.TestCase):

    def test_pas_de_worker_aucun_envoi(self):
        """Aucun worker vivant -> on ne soumet RIEN.

        C'est le garde-fou principal : sans lui, un job part et attend
        indéfiniment un worker qui ne viendra pas.
        """
        with mock.patch.object(tg, "_hub_actif", return_value=False), \
             mock.patch.object(tg, "_hub_transcrire") as h, \
             mock.patch.object(tg, "charger",
                               side_effect=RuntimeError("pas de modèle en test")):
            r = tg.transcrire("x.wav")
        h.assert_not_called()
        self.assertIn("erreur", r)          # on est bien parti en local

    def test_worker_vivant_utilise_le_hub(self):
        """Un worker répond -> on prend SA transcription, sans charger le
        modèle local (sinon les deux machines feraient le même travail)."""
        # Témoin d'hôte neutre : `.invalid` est un TLD réservé, il ne désigne
        # aucune machine.
        temoin = "worker.exemple.invalid"
        fake = {"segments": [{"start": 0.0, "end": 1.0, "text": "bonjour",
                              "mots": None}],
                "worker": temoin, "device": "cuda",
                "duree": 10.0, "elapsed": 2.0}
        with mock.patch.object(tg, "_hub_actif", return_value=True), \
             mock.patch.object(tg, "_hub_transcrire", return_value=fake), \
             mock.patch.object(tg, "charger") as ch:
            r = tg.transcrire("x.wav")
        ch.assert_not_called()
        self.assertEqual(r["backend"], "hub:" + temoin)
        self.assertEqual(r["device"], "cuda")
        self.assertEqual(r["segments"][0]["text"], "bonjour")
        self.assertEqual(r["rtf"], 0.2)

    def test_hub_muet_repli_local(self):
        """Le Hub ne rend rien (panne, délai, erreur) -> on refait en LOCAL.

        Le Hub ACCÉLÈRE ; il ne bloque pas et ne perd pas de résultat.
        """
        with mock.patch.object(tg, "_hub_actif", return_value=True), \
             mock.patch.object(tg, "_hub_transcrire", return_value=None), \
             mock.patch.object(tg, "charger",
                               side_effect=RuntimeError("stop")):
            r = tg.transcrire("x.wav")
        self.assertIn("erreur", r)          # chemin local atteint

    def test_backend_force_ne_detourne_pas(self):
        """`backend` explicite = on veut du LOCAL : on ne consulte pas le Hub.

        Utile pour mesurer le coût CPU sans être détourné sur une autre
        machine (comparaison local / Hub).
        """
        with mock.patch.object(tg, "_hub_actif") as actif, \
             mock.patch.object(tg, "charger",
                               side_effect=RuntimeError("stop")):
            tg.transcrire("x.wav", backend="cpu")
        actif.assert_not_called()

    def test_chemin_envoye_est_un_nom_de_partage(self):
        """On envoie 'memorybrain-groupes/<fichier>', jamais un chemin absolu.

        Un chemin absolu ne marche que sur la machine qui l'a émis : le job
        qui tombe sur un autre worker échouerait. Le nom de partage, lui, est
        résolu par le worker chez lui.
        """
        envoye = {}

        class FauxRes:
            def get(self, timeout=None):
                return {"segments": [], "worker": "w", "audio_duration": 1.0,
                        "elapsed": 0.1}

        class FauxApp:
            def send_task(self, nom, args=None, kwargs=None):
                envoye["args"] = args
                return FauxRes()

        import sys
        with mock.patch.dict(sys.modules, {"aiinfra_app":
                                           type("M", (), {"app": FauxApp()})()}):
            tg._hub_transcrire("/chemin/absolu/groupe_0001.wav", "fr", 5)
        self.assertEqual(envoye["args"], ["memorybrain-groupes/groupe_0001.wav"])


if __name__ == "__main__":
    unittest.main()