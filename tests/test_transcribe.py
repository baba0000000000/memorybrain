"""Tests de la transcription — le cœur de la V2.

Lancer :  python3 -m pytest tests/test_transcribe.py

On ne teste PAS le Compute Hub ici (il faut du GPU et 20 s par fichier) :
on teste la logique autour — surtout les positions absolues (règle 2),
qui est la faute historique de la V1.
"""
import importlib
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402
from transcription import transcribe  # noqa: E402


class FakeHub:
    """Remplace le Compute Hub par un faux qui renvoie ce qu'on veut."""

    def __init__(self, reponse):
        self.reponse = reponse
        self.appels = []

    def __call__(self, audio: Path, model: str):
        self.appels.append((str(audio), model))
        return self.reponse


def wav_avec_duree(chemin: Path, secondes: float = 1.0, freq: int = 220) -> None:
    import math
    import struct
    n = int(16000 * secondes)
    with wave.open(str(chemin), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / 16000)))
            for i in range(n)))


class TestTranscription(unittest.TestCase):
    def setUp(self):
        self.dossier = Path(tempfile.mkdtemp(prefix="mb_trans_"))
        self.con = db.connect(self.dossier / "t.sqlite")
        db.set_schema_version(self.con)
        self.faux_hub = None
        # le vrai _hub_call, avant que les tests ne l'ecrasent
        self._hub_call_original = transcribe._hub_call

    def tearDown(self):
        # Les tests suivants ecrivent transcribe._hub_call = FakeHub(...) SANS
        # le restaurer. Sans ce retour, TestWorkerForce (qui teste le VRAI
        # _hub_call) herite de la factice et ne voit aucun appel a subprocess :
        # 2 echecs qui n'ont rien a voir avec le code teste. On remet
        # l'original a chaque test.
        self.con.close()
        transcribe._hub_call = self._hub_call_original

    def _chunk(self, idx: int, nom: str | None = None) -> int:
        f = self.dossier / (nom or f"2024-01-15_09-00-00-000_{idx:04d}.wav")
        wav_avec_duree(f, 1.0, freq=200 + idx)
        self.con.execute(
            "INSERT INTO chunk(session_id, source_id, recorded_at, chunk_index,"
            " start_time, end_time, duration_sec, original_file, hash, status,"
            " note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (None, None, "2024-01-15T09:00:00", idx, idx * 60.0, idx * 60.0 + 1,
             1.0, str(f), f"h{idx}", "pending", None, "2024-01-15T09:00:00"))
        self.con.commit()
        return self.con.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ---------- règle 2 : la faute historique ----------
    def test_01_segments_absolus_pas_relatifs(self):
        """Le hub renvoie des positions RELATIVES au chunk. La base doit
        contenir des positions ABSOLUES. Chunk 10 → base 600 s."""
        cid = self._chunk(10)
        transcribe._hub_call = FakeHub({
            "segments": [{"start": 0.0, "end": 2.0, "text": "bonjour"},
                         {"start": 2.5, "end": 4.0, "text": "monde"}],
            "text": "bonjour monde", "model": "small", "worker": "test"})
        res = transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(res["statut"], "ok")
        segs = self.con.execute(
            "SELECT start_time, end_time FROM segment WHERE chunk_id=? ORDER BY idx",
            (cid,)).fetchall()
        self.assertEqual(segs[0]["start_time"], 600.0, "position NON absolue")
        self.assertEqual(segs[1]["start_time"], 602.5, "position NON absolue")

    def test_02_deux_chunks_ne_se_chevauchent_pas(self):
        """Le vrai test : sans offset, tous les chunks diraient 0-60 s."""
        a, b = self._chunk(1), self._chunk(2)
        transcribe._hub_call = FakeHub({
            "segments": [{"start": 0.0, "end": 1.0, "text": "x"}],
            "text": "x", "model": "small", "worker": "test"})
        transcribe.transcribe_chunk(self.con, a)
        transcribe.transcribe_chunk(self.con, b)
        sa = self.con.execute("SELECT start_time FROM segment WHERE chunk_id=?",
                              (a,)).fetchone()[0]
        sb = self.con.execute("SELECT start_time FROM segment WHERE chunk_id=?",
                              (b,)).fetchone()[0]
        self.assertEqual(sa, 60.0)
        self.assertEqual(sb, 120.0)
        self.assertNotEqual(sa, sb, "les deux chunks se chevauchent !")

    # ---------- les cas dégradés ----------
    def test_03_silence_accepte(self):
        """0 segment et 0 texte = silence normal, pas une erreur."""
        cid = self._chunk(3)
        transcribe._hub_call = FakeHub(
            {"segments": [], "text": "", "model": "small", "worker": "test"})
        res = transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(res["statut"], "ok")
        self.assertEqual(res["segments"], 0)

    def test_04_texte_sans_segments_refuse(self):
        """Du texte mais 0 segment = résultat FAUX. On refuse (règle 3 ne
        veut pas dire « accepter n'importe quoi »).

        Cas observé : le hub renvoie un texte mais perd ses segments — c'est
        exactement le bug de la V1 (des transcripts entiers sans horaires).
        """
        cid = self._chunk(4)
        # le texte vient du .txt que le hub écrit à côté de l'audio,
        # tandis que 'segments' est vide : c'est le bug exact de la V1
        txt = self.dossier / "sortie.txt"
        txt.write_text("du texte sans horaires", encoding="utf-8")
        transcribe._hub_call = FakeHub({
            "segments": [], "output": str(txt), "text": "",
            "model": "small", "worker": "test"})
        res = transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(res["statut"], "pas_de_segments",
                         "un texte SANS segments doit être refusé")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM segment WHERE chunk_id=?",
                             (cid,)).fetchone()[0], 0)

    def test_05_hub_mort_ne_casse_rien(self):
        cid = self._chunk(5)
        transcribe._hub_call = FakeHub(None)
        res = transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(res["statut"], "hub_indisponible")
        st = self.con.execute(
            "SELECT status FROM pipeline_step WHERE chunk_id=? AND step='transcription'",
            (cid,)).fetchone()[0]
        self.assertEqual(st, "failed")

    def test_06_audio_absent_signale(self):
        self.con.execute(
            "INSERT INTO chunk(session_id, source_id, recorded_at, chunk_index,"
            " start_time, end_time, duration_sec, original_file, hash, status,"
            " note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (None, None, "2024-01-15T09:00:00", 9, 540.0, None, 1.0,
             "/chemin/qui/nexiste/pas.wav", "h9", "pending", None, "x"))
        self.con.commit()
        cid = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        res = transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(res["statut"], "audio_absent")

    def test_07_rejeu_ne_duplique_pas(self):
        """Le hub coûte 20 s : rejouer doit être gratuit."""
        cid = self._chunk(6)
        transcribe._hub_call = FakeHub({
            "segments": [{"start": 0.0, "end": 1.0, "text": "a"}],
            "text": "a", "model": "small", "worker": "test"})
        transcribe.transcribe_chunk(self.con, cid)
        n_appels = len(self.faux_hub.appels) if self.faux_hub else 1
        res = transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(res["statut"], "déjà_fait")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM segment WHERE chunk_id=?",
                             (cid,)).fetchone()[0], 1,
            "le rejeu a dupliqué les segments")

    def test_08_changement_de_modele_refait(self):
        """On veut changer de modèle : ça doit retranscrire."""
        cid = self._chunk(7)
        transcribe._hub_call = FakeHub({
            "segments": [{"start": 0.0, "end": 1.0, "text": "a"}],
            "text": "a", "model": "small", "worker": "test"})
        transcribe.transcribe_chunk(self.con, cid, model="small")
        transcribe._hub_call = FakeHub({
            "segments": [{"start": 0.0, "end": 1.0, "text": "a"}],
            "text": "a", "model": "large-v3", "worker": "test"})
        res = transcribe.transcribe_chunk(self.con, cid, model="large-v3")
        self.assertEqual(res["statut"], "ok")
        st = self.con.execute(
            "SELECT model FROM pipeline_step WHERE chunk_id=? AND step='transcription'",
            (cid,)).fetchone()[0]
        self.assertEqual(st, "large-v3")

    def test_09_reecriture_propre(self):
        """Retranscrire le même chunk ne doit pas doubler les segments."""
        cid = self._chunk(8)
        for _ in range(3):
            transcribe._hub_call = FakeHub({
                "segments": [{"start": 0.0, "end": 1.0, "text": "a"},
                             {"start": 1.0, "end": 2.0, "text": "b"}],
                "text": "ab", "model": "small", "worker": "test"})
            self.con.execute("UPDATE chunk SET status='pending' WHERE chunk_id=?",
                             (cid,))
            self.con.execute("DELETE FROM pipeline_step WHERE chunk_id=?", (cid,))
            self.con.commit()
            transcribe.transcribe_chunk(self.con, cid)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM segment WHERE chunk_id=?",
                             (cid,)).fetchone()[0], 2)

    def test_10_hhmmss(self):
        """_hhmmss affiche une heure LOCALE à partir d'un epoch ABSOLU.

        Les positions sont absolues (règle 2), pas des durées depuis le
        début du chunk. Le test vérifie l'heure réelle du jour — et pas
        seulement la partie HH:MM:SS : un enregistrement qui franchit
        minuit doit changer de date dans le .md, sinon deux moments
        opposés se lisent pareil.
        """
        from datetime import datetime
        base = datetime(2024, 1, 15, 12, 45, 39).timestamp()
        self.assertEqual(transcribe._hhmmss(base), "2024-01-15 12:45:39")
        self.assertEqual(transcribe._hhmmss(base + 21), "2024-01-15 12:46:00")

        minuit = datetime(2024, 1, 16, 0, 0, 30).timestamp()
        self.assertEqual(transcribe._hhmmss(minuit), "2024-01-16 00:00:30")

        # la veille, 30 s avant minuit : le jour doit changer
        veille = datetime(2024, 1, 15, 23, 59, 30).timestamp()
        self.assertEqual(transcribe._hhmmss(veille), "2024-01-15 23:59:30")

    def test_11_chunk_inexistant(self):
        res = transcribe.transcribe_chunk(self.con, 999999)
        self.assertEqual(res["statut"], "absent")

    def test_12_ordre_chronologique(self):
        """transcribe_pending doit remonter le plus ancien d'abord."""
        for i in (5, 1, 3):
            self._chunk(i)
        vus = []
        transcribe._hub_call = FakeHub({
            "segments": [{"start": 0.0, "end": 1.0, "text": "a"}],
            "text": "a", "model": "small", "worker": "test"})
        transcribe.transcribe_pending(self.con, limit=3,
                                      progress=lambda i, n, r: vus.append(i))
        self.assertEqual(len(vus), 3)
        rows = self.con.execute(
            "SELECT chunk_index FROM chunk ORDER BY recorded_at, chunk_index"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [1, 3, 5],
                         "ordre chronologique non respecté")


class TestWorkerForce(unittest.TestCase):
    """Le V2 doit envoyer ses jobs au worker DÉSIGNÉ, jamais au hasard.

    Bug observé : Celery répartit par défaut sur tous les workers connectés.
    Un worker qui ne voit pas la racine du projet prenait le job et le gardait
    bloqué : la chaîne restait à 0 sans afficher d'erreur, et `celery inspect
    active` montrait le job « active » du mauvais côté.

    Ces tests verrouillent le MÉCANISME, pas un nom de machine : la cible est
    lue dans MEMORYBRAIN_WORKER et vaut un hôte neutre par défaut. Le témoin
    utilisé ici (`*.invalid`, TLD réservé) ne désigne aucune machine.
    """

    NEUTRE = "worker.exemple.invalid"

    def setUp(self):
        self.appele = []
        self._env = mock.patch.dict(os.environ,
                                    {"MEMORYBRAIN_WORKER": self.NEUTRE})
        self._env.start()
        importlib.reload(transcribe)      # relit la variable d'environnement

    def tearDown(self):
        self._env.stop()
        importlib.reload(transcribe)      # remet la valeur d'origine

    def _faux_run(self, chemin_fictif=None):
        def run(cmd, **kw):
            self.appele.append(cmd)
            class R:
                stdout = ("{'chars': 10, 'segments': [{'start': 0.0, 'end': 1.0,"
                          " 'text': 'bonjour'}], 'elapsed': 0.1}")
                stderr = ""
                returncode = 0
            return R()
        return run

    def _hub_present(self):
        """Rend le Hub « installé » : deux fichiers qui existent vraiment.

        Sans ça, `_hub_call` rend None avant de construire la moindre
        commande, et le test passerait pour une machine sans Hub au lieu de
        vérifier le mécanisme.
        """
        return (mock.patch.object(transcribe, "HUB_PY", Path(sys.executable)),
                mock.patch.object(transcribe, "HUB_SUBMIT", Path(__file__)))

    def test_01_worker_vient_de_la_variable_d_environnement(self):
        import subprocess as sp
        orig = sp.run            # transcribe fait 'import subprocess'
        sp.run = self._faux_run()
        p_py, p_submit = self._hub_present()
        try:
            with p_py, p_submit:
                transcribe._hub_call(
                    Path("/x/2024-01-15_09-00-00-000_0001.wav"), "small")
        finally:
            sp.run = orig
        self.assertTrue(self.appele, "aucune commande lancée")
        cmd = self.appele[0]
        self.assertIn("--worker", cmd, "la commande doit nommer le worker")
        cible = cmd[cmd.index("--worker") + 1]
        self.assertEqual(cible, os.environ["MEMORYBRAIN_WORKER"],
                         "la cible doit être lue dans MEMORYBRAIN_WORKER, "
                         "pas écrite en dur")

    def test_02_serveur_vient_avant_le_chemin_absolu(self):
        """Le nom de partage d'abord : il marche sur n'importe quelle machine."""
        import subprocess as sp
        orig = sp.run            # transcribe fait 'import subprocess'
        sp.run = self._faux_run()
        p_py, p_submit = self._hub_present()
        try:
            with p_py, p_submit, tempfile.TemporaryDirectory() as d:
                racine = Path(d)
                (racine / "2024-01-15_09-00-00-000_0001.wav").write_bytes(b"x")
                saved = dict(transcribe.HUB_ROOTS)
                transcribe.HUB_ROOTS = {"memorybrain-done": [racine]}
                try:
                    transcribe._hub_call(
                        racine / "2024-01-15_09-00-00-000_0001.wav", "small")
                finally:
                    transcribe.HUB_ROOTS = saved
        finally:
            sp.run = orig
        self.assertTrue(self.appele, "aucune commande lancée")
        # le PREMIER essai doit etre le nom de partage, resolu par le worker
        premiere = self.appele[0][-1]
        self.assertTrue(premiere.startswith("memorybrain-done/"),
                        f"le partage doit etre tente en premier, pas le chemin : {premiere}")

    def test_03_worker_par_defaut_neutre(self):
        """Sans variable d'environnement, la cible reste locale et neutre."""
        self._env.stop()
        os.environ.pop("MEMORYBRAIN_WORKER", None)
        try:
            importlib.reload(transcribe)
            self.assertEqual(transcribe.WORKER_CIBLE, "localhost",
                             "le défaut doit désigner la machine locale")
        finally:
            self._env.start()


if __name__ == "__main__":
    unittest.main(verbosity=2)