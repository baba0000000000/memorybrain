"""Corrections à l'écoute — elles améliorent le profil durablement.

Principe : chaque vérification humaine alimente le modèle de vérification —
une correction « ce n'est pas moi » vaut mieux qu'un réglage deviné.

Pourquoi c'est la bonne idée, et pas un bricolage
-------------------------------------------------
Les scores se recouvrent dès que la voix du propriétaire est enregistrée de
loin : une voix lointaine et celle d'un autre homme se CHEVAUCHENT :
aucun seuil ne peut les séparer. Ce n'est donc pas un problème de réglage,
c'est un manque de référence dans SES conditions (téléphone, pièce, loin).
Sa correction à l'écoute est la seule source qui apporte cette information.

Ce que le module fait
---------------------
  - `ajouter()`  : prend une phrase, calcule son empreinte, l'enregistre
    dans `speaker_embedding` avec le tag `|confirme`, et note la correction
    dans `work/corrections-voix.tsv` (traçabilité) ;
  - `lister()`   : montre les corrections déjà enregistrées.

Garde-fous (appris d'un désastre de profils en surnombre)
----------------------------------------------
  - on n'enrôle QUE ce que le propriétaire confirme (« c'est moi »)
    supposition du programme ;
  - une empreinte trop proche d'une existante (cosinus ≥ 0,90) n'est pas
    ajoutée en double : 50 copies du même passage n'apprennent rien ;
  - une correction « ce n'est pas moi » n'est JAMAIS enrôlée : elle sert de
    contre-exemple pour mesurer le seuil.

Usage :
    python3 outils/corrections_voix.py --heure 09:30:00 --verdict oui
    python3 outils/corrections_voix.py --heure 09:31:12 --verdict non --note "radio"
    python3 outils/corrections_voix.py --lister
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import blocs
from speakers import reference as ref
from speakers import voix

JOURNAL = Path(config.WORK) / "corrections-voix.tsv"
TAG = "wespeaker-resnet34|confirme"


def _norme(v):
    import numpy as np
    return v / (np.linalg.norm(v) + 1e-9)


def _segment_a(con: sqlite3.Connection, heure: str, jour: str | None = None):
    """Retrouve la phrase qui commence à cette heure (aujourd'hui par défaut)."""
    j = jour or datetime.date.today().isoformat()
    d = datetime.datetime.fromisoformat(f"{j}T{heure}")
    t = d.timestamp()
    return con.execute(
        "SELECT segment_id, chunk_id, start_time, end_time, text FROM segment"
        " WHERE ABS(start_time - ?) < 1.0 ORDER BY ABS(start_time - ?) LIMIT 1",
        (t, t)).fetchone()


def _audio_du_segment(con: sqlite3.Connection, seg):
    """Le son de la phrase, lu dans sa case (position = heure réelle)."""
    import numpy as np
    import soundfile as sf

    t_debut = blocs.debut_case(seg["start_time"])
    chemin = blocs.chemin_case(t_debut)
    if not chemin.is_file():
        return None, None
    x, sr = sf.read(str(chemin), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    a = max(0.0, seg["start_time"] - t_debut)
    b = min(len(x) / sr, seg["end_time"] - t_debut)
    if b - a < config.MIN_EXTRAIT_EMPREINTE:      # phrase courte : contexte
        manque = (config.MIN_EXTRAIT_EMPREINTE - (b - a)) / 2.0
        a = max(0.0, a - manque)
        b = min(len(x) / sr, b + manque)
    return x[int(a * sr):int(b * sr)], sr


def ajouter(con: sqlite3.Connection, heure: str, verdict: str,
            note: str = "", jour: str | None = None,
            jusqu_a: str | None = None) -> dict:
    """Enregistre la correction du propriétaire, et enrôle l'empreinte si « oui »."""
    import numpy as np

    seg = _segment_a(con, heure, jour)
    if seg is None:
        return {"statut": "segment_introuvable", "heure": heure}

    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if not JOURNAL.exists():
        JOURNAL.write_text(
            "heure\tverdict\ttexte\tnote\tdate\n", encoding="utf-8")
    JOURNAL.write_text(
        JOURNAL.read_text(encoding="utf-8")
        + f"{heure}\t{verdict}\t{seg['text'][:120]}\t{note}\t"
          f"{datetime.datetime.now():%Y-%m-%d %H:%M}\n", encoding="utf-8")

    if verdict != "oui":
        return {"statut": "noté_non", "heure": heure, "texte": seg["text"][:80]}

    if jusqu_a:
        # n'enrôler que le début de la phrase : utile quand la fin est une
        # autre voix (le propriétaire puis le narrateur dans le même segment)
        hh, mm, ss = (int(x) for x in jusqu_a.split(":"))
        base = (datetime.datetime.fromisoformat(jour) if jour
                else datetime.datetime.now().replace(hour=0, minute=0,
                                                     second=0, microsecond=0))
        d = base + datetime.timedelta(hours=hh, minutes=mm, seconds=ss)
        limite = d.timestamp()
        seg = dict(seg)
        seg["end_time"] = min(float(seg["end_time"]), limite)

    morceau, sr = _audio_du_segment(con, seg)
    if morceau is None:
        return {"statut": "audio_introuvable", "heure": heure}
    e = voix.empreinte(morceau, sr)
    if e is None:
        return {"statut": "empreinte_impossible", "heure": heure}
    e = _norme(np.asarray(e, dtype="float32"))

    base = ref.matrice(con, rafraichir=True)
    if float((base @ e).max()) >= 0.90:
        return {"statut": "deja_connue", "heure": heure}

    con.execute(
        "INSERT INTO speaker_embedding(speaker_id, group_id, vecteur, dim,"
        " model, created_at) VALUES(?,?,?,?,?,datetime('now'))",
        (config.PROFIL_PROPRIETAIRE, None, e.astype("float32").tobytes(), len(e), TAG))
    con.commit()
    ref.oublier()
    return {"statut": "enrolee", "heure": heure, "texte": seg["text"][:80],
            "profil": int(ref.matrice(con, rafraichir=True).shape[0])}


def lister(con: sqlite3.Connection) -> dict:
    n = con.execute(
        "SELECT COUNT(*) FROM speaker_embedding WHERE speaker_id=?"
        " AND model LIKE '%|confirme%'", (config.PROFIL_PROPRIETAIRE,)).fetchone()[0]
    lignes = []
    if JOURNAL.is_file():
        lignes = JOURNAL.read_text(encoding="utf-8").strip().splitlines()
    return {"empreintes_confirmees": n, "journal": lignes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heure", help="HH:MM:SS de la phrase")
    ap.add_argument("--verdict", choices=["oui", "non"])
    ap.add_argument("--note", default="")
    ap.add_argument("--jour", default=None)
    ap.add_argument("--jusqu", help="HH:MM:SS : n'enrôler que jusqu'à cette heure")
    ap.add_argument("--lister", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row

    if a.lister or not (a.heure and a.verdict):
        out = lister(con)
        print(f"empreintes confirmées par le propriétaire : {out['empreintes_confirmees']}")
        for l in out["journal"][:40]:
            print("  " + l)
        con.close()
        return 0

    r = ajouter(con, a.heure, a.verdict, a.note, a.jour, a.jusqu)
    print(r)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
