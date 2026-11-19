"""Le test de bout en bout de la chaîne.

La méthode : on valide chaque étape séparément, puis on les enchaîne
(1, puis 1+2, puis 2+3, puis 1+2+3, et ainsi de suite). Chaque palier
utilise des **groupes neufs**, jamais ceux du palier précédent, sinon on
valide le programme sur des cas qu'il a déjà vus.

Ce script enchaîne ces paliers et note ce qui a marché et ce qui a échoué
dans un rapport lisible.

    python3 tester_chaine.py            # tous les paliers
    python3 tester_chaine.py --palier 3  # juste le palier 3
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import db

from classification import classify
from diarization import diarize
from grouping import group as gmod
from speakers import align, identify
from transcription import transcribe_group as tg

RAPPORT = config.V2_DIR / "work" / "RAPPORT-TESTS.md"


def _base_prete() -> None:
    config.ensure_dirs()


def _ecrire(rapport: list[str], texte: str) -> None:
    rapport.append(texte)
    RAPPORT.parent.mkdir(parents=True, exist_ok=True)
    RAPPORT.write_text("\n".join(rapport), encoding="utf-8")
    print(texte, flush=True)


def _marque_etape(con, gid: int, etape: str, statut: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"test:{etape}:{gid}", statut))


def _groupes_neufs(con, n: int) -> list[int]:
    """Les `n` groupes encore jamais touchés par ce script de test.

    Aucun groupe déjà validé n'est repris : on prend les groupes dont aucune
    étape n'a encore été validée, donc chaque palier travaille sur du
    matériel neuf.
    """
    deja = set()
    for r in con.execute("SELECT key FROM meta WHERE key LIKE 'test:%'"):
        gid = int(r["key"].rsplit(":", 1)[-1])
        deja.add(gid)
    rows = con.execute("SELECT group_id FROM chunk_group ORDER BY group_id").fetchall()
    neufs = [r["group_id"] for r in rows if r["group_id"] not in deja]
    return neufs[:n]


def _etape(con, gid: int, nom: str, fn) -> dict:
    """Exécute une étape, chronomètre, et ne lève jamais."""
    t0 = time.time()
    try:
        r = fn(con, gid)
        ok = "erreur" not in r
    except Exception as e:
        r = {"erreur": str(e)[:200]}
        ok = False
    r["_ok"] = ok
    r["_s"] = round(time.time() - t0, 1)
    # meta.value est une colonne TEXT : on écrit des chaînes, sinon
    # SQLite stocke un booléen et le relire donne 1/0 au lieu de ok/ko.
    _marque_etape(con, gid, nom, "ok" if ok else "ko")
    return r


def _markdown(con, gid: int) -> str:
    noms = {r["speaker_id"]: r["name"] for r in con.execute(
        "SELECT speaker_id, name FROM speaker WHERE name IS NOT NULL")}
    noms.setdefault(config.PROFIL_PROPRIETAIRE, "le propriétaire")
    a = align.aligner_groupe(con, gid)
    return align.en_markdown(a["interventions"], noms)


# ===== LES PALIERS =====================================================
# Chaque palier ajoute UNE étape de plus, sur un groupe neuf. C'est ce
# que le propriétaire demande : valider une étape, puis deux, puis trois…
PALIERS = {
    1: [("transcription", lambda c, g: tg.transcrire_groupe(c, g))],
    2: [("transcription", lambda c, g: tg.transcrire_groupe(c, g)),
        ("classification", lambda c, g: classify.classer_groupe(c, g))],
    3: [("transcription", lambda c, g: tg.transcrire_groupe(c, g)),
        ("classification", lambda c, g: classify.classer_groupe(c, g)),
        ("diarisation", lambda c, g: diarize.diariser_groupe(c, g))],
    4: [("transcription", lambda c, g: tg.transcrire_groupe(c, g)),
        ("classification", lambda c, g: classify.classer_groupe(c, g)),
        ("diarisation", lambda c, g: diarize.diariser_groupe(c, g)),
        ("profils", lambda c, g: identify.attribuer_profils(c, g))],
    5: [("transcription", lambda c, g: tg.transcrire_groupe(c, g)),
        ("classification", lambda c, g: classify.classer_groupe(c, g)),
        ("diarisation", lambda c, g: diarize.diariser_groupe(c, g)),
        ("profils", lambda c, g: identify.attribuer_profils(c, g)),
        ("alignement", lambda c, g: align.aligner_groupe(c, g))],
}


def lancer(paliers: list[int], n_groupes: int = 1,
           avec_extraction: bool = True) -> int:
    _base_prete()
    con = db.connect(config.DB_PATH)

    rapport: list[str] = [
        "# Tests de la chaîne MemoryBrain V2",
        "",
        f"Date : {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}",
        f"Groupe de {config.GROUPE_TAILLE} fichiers · "
        f"Whisper {config.MODELE_ASR_NOM} · LLM {config.MODELE_LLM}",
        "",
        "Méthode : chaque palier tourne sur un groupe **neuf**, jamais sur",
        "celui du palier précédent. On ajoute une étape à la fois.",
        "",
    ]

    echecs = 0
    for p in paliers:
        etapes = PALIERS[p]
        _ecrire(rapport, f"\n## Palier {p} — {len(etapes)} étape(s) : "
                         + " → ".join(n for n, _ in etapes))

        groupes = _groupes_neufs(con, n_groupes)
        if not groupes:
            _ecrire(rapport, "  **aucun groupe neuf disponible** — "
                             "relancer `--groupement` pour en créer")
            continue

        for gid in groupes:
            _ecrire(rapport, f"\n### Groupe {gid} (neuf)")
            res: dict = {}
            for nom, fn in etapes:
                r = _etape(con, gid, f"p{p}:{nom}", fn)
                res[nom] = r
                detail = ""
                for k in ("rtf", "classe", "tours", "locuteurs",
                          "interventions", "couverts", "segments"):
                    if r.get(k) not in (None, []):
                        detail += f" {k}={r[k]}"
                marque = "OK " if r["_ok"] else "ECHEC"
                _ecrire(rapport, f"  - {marque} **{nom}** ({r['_s']}s){detail}"
                                 + (f"  ERREUR: {r.get('erreur')}" if r.get("erreur") else ""))
                if not r["_ok"]:
                    echecs += 1

            # contrôles de cohérence sur le groupe
            v = gmod.verifier_membres(con, gid)
            _ecrire(rapport, f"  - {'OK ' if v.get('ok') else 'ECHEC'} "
                             f"**cohérence** : {v.get('n_membres')} membres, "
                             f"écart {v.get('ecart_sec')} s")
            if not v.get("ok"):
                echecs += 1

            if p >= 5:
                cov = diarize.couverture(con, gid)
                _ecrire(rapport, f"  - couverture de la parole : "
                                 f"{cov.get('couverture')} % "
                                 f"({cov.get('couverts')} / {cov.get('duree_parlee')} s)")
                _ecrire(rapport, "\n**TRANSCRIPT :**\n")
                _ecrire(rapport, _markdown(con, gid)[:1500] + "\n")

            # l'extraction LLM seulement si le groupe a une classe qui
            # produit des idées (PERSONNEL ou BUSINESS)
            if avec_extraction and p >= 5:
                from extraction import extract
                cl = con.execute("SELECT classe FROM classification"
                                 " WHERE group_id=?", (gid,)).fetchone()
                if cl and cl["classe"] in ("PERSONNEL", "BUSINESS"):
                    r = extract.extraire_groupe(con, gid)
                    _ecrire(rapport, f"  - {'OK ' if 'erreur' not in r else 'ECHEC'} "
                                     f"**extraction LLM** : {r.get('statut')} "
                                     f"{r.get('ecrits', 0)} item(s), "
                                     f"{r.get('refuses_invention', 0)} refusés")
                else:
                    _ecrire(rapport, f"  - extraction LLM **sautée** : classe "
                                     f"{cl['classe'] if cl else '?'} "
                                     f"(seules PERSONNEL et BUSINESS produisent des idées)")

    _ecrire(rapport, f"\n---\n\n**Résultat : {echecs} échec(s).**")
    con.close()
    return echecs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--palier", type=int, choices=sorted(PALIERS),
                    help="un seul palier")
    ap.add_argument("--groupes", type=int, default=1,
                    help="groupes neufs par palier")
    ap.add_argument("--sans-extraction", action="store_true")
    a = ap.parse_args()

    paliers = [a.palier] if a.palier else sorted(PALIERS)
    return lancer(paliers, a.groupes, not a.sans_extraction)


if __name__ == "__main__":
    raise SystemExit(main())