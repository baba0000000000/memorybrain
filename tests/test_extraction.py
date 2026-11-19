"""Tests de l'extraction (étape 8) — règle « marqueur > classification ».

Décisions et défauts observés :
  - un marqueur oral PRIME sur la classification
    UNKNOWN dont il a délimité un passage doit produire un item ;
  - un groupe UNKNOWN SANS marqueur ne produit RIEN (garde-fou) ;
  - relancer l'étape ne doit pas DUPLIQUER les items (idempotence).
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import extraction.extract as ex  # noqa: E402

AVEC_MARQUEUR = (
    "**Locuteur 1** : bonjour on parle de choses\n\n"
    "**Locuteur 1** : ajoute une tâche : appeler le plombier "
    "demain matin\n\n")
SANS_MARQUEUR = "**Locuteur 1** : il fait beau et la journée commence\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="mb_extract_"))
        self.con = db.connect(self.tmp / "t.sqlite")
        db.set_schema_version(self.con)
        self._sauve = (ex.serveur_disponible, ex.appeler_llm, ex.transcript_texte)
        self.texte = AVEC_MARQUEUR
        ex.serveur_disponible = lambda **k: True
        ex.transcript_texte = lambda con, gid, **k: self.texte
        ex.appeler_llm = lambda prompt: {
            "texte": '{"items": [{"type": "todo", "texte": '
                     '"appeler le plombier"}]}',
            "modele": "faux"}
        self.con.execute(
            "INSERT INTO source(source_id,kind,label,uri,first_seen)"
            " VALUES(1,'TEST','t','x','2024-01-01')")
        self.con.execute(
            "INSERT INTO session(session_id,source_id,created_at)"
            " VALUES(1,1,'2024-01-01')")
        self.con.execute(
            "INSERT INTO chunk_group(group_id,session_id,taille,n_chunks,"
            "start_time,end_time,duration_sec,audio_path,fingerprint,statut,"
            "created_at) VALUES(2,1,5,5,0.0,300.0,300.0,'x.wav','fp','ok',"
            "'2024-01-01')")
        self.con.execute(
            "INSERT INTO speaker(speaker_id,name,status,created_at)"
            " VALUES(0,'le propriétaire','confirmed','2024-01-01')")
        self.con.execute(
            "INSERT INTO classification(group_id, classe, created_at)"
            " VALUES(2, 'UNKNOWN', '2024-01-01')")
        self.con.commit()

    def tearDown(self):
        (ex.serveur_disponible, ex.appeler_llm,
         ex.transcript_texte) = self._sauve
        self.con.close()
        for s in ("", "-wal", "-shm"):
            p = pathlib.Path(str(self.tmp / "t.sqlite") + s)
            if p.exists():
                p.unlink()


class TestExtraction(Base):
    def _items(self):
        return self.con.execute(
            "SELECT kind, texte, chemin FROM extraction WHERE group_id=2"
        ).fetchall()

    def test_marqueur_prime_sur_classification_unknown(self):
        r = ex.extraire_groupe(self.con, 2)
        self.assertEqual(r["statut"], "ok")
        self.assertEqual(r["ecrits"], 1)
        self.assertEqual(r["origine"], "marqueur")
        self.assertEqual(r["classe"], "UNKNOWN")
        items = self._items()
        self.assertEqual(items[0][0], "TODO")

    def test_relance_ne_duplique_pas(self):
        ex.extraire_groupe(self.con, 2)
        ex.extraire_groupe(self.con, 2)
        ex.extraire_groupe(self.con, 2)
        self.assertEqual(len(self._items()), 1,
                         "une relance avec --force a dupliqué les items")

    def test_groupe_qui_redevient_ignore_nettoye_les_anciens_items(self):
        # Défaut : un faux TODO a survécu au retrait du
        # marqueur fautif, parce que « ignoré » sortait AVANT le nettoyage.
        ex.extraire_groupe(self.con, 2)
        self.assertEqual(len(self._items()), 1)
        self.texte = SANS_MARQUEUR
        r = ex.extraire_groupe(self.con, 2)
        self.assertEqual(r["statut"], "ignoré")
        self.assertEqual(len(self._items()), 0,
                         "l'ancien item a survécu au passage en « ignoré »")

    def test_sans_marqueur_et_unknown_ne_produit_rien(self):
        self.texte = SANS_MARQUEUR
        r = ex.extraire_groupe(self.con, 2)
        self.assertEqual(r["statut"], "ignoré")
        self.assertEqual(len(self._items()), 0)


if __name__ == "__main__":
    unittest.main()
