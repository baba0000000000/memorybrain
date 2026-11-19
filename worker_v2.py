"""Un worker du pipeline V2 : prend 1 groupe sur N, le traite entièrement.

Pourquoi un fichier séparé pour le worker plutôt qu'une option de
`pipeline_v2.py` :

  - **Chaque worker a sa propre base SQLite.** SQLite n'accepte qu'un seul
    écrivain à la fois ; deux workers sur la même base donnent
    « database is locked » après quelques secondes. C'est constaté, pas
    supposé.
  - La répartition se fait par **modulo**, sans aucune coordination : le
    worker i ne prend que les groupes où `group_id % total == i`. Deux
    workers ne peuvent donc pas tomber sur le même groupe, même si on en
    relance 5 par accident.
  - Les modèles (Whisper, pyannote) sont chargés UNE fois par worker et
    réutilisés pour tous ses groupes : 4 s + 4 s économisés par groupe.

Variables d'environnement lues :
    MB_WORKER         index de ce worker, à partir de 1
    MB_TOTAL_WORKERS  nombre total de workers
    MB_GROUPES        nombre max de groupes à traiter
    MB_BASE           chemin de la base (défaut : database/worker_N.sqlite)
    MB_LLM            0 pour ne pas appeler le LLM (utile pour mesurer
                      l'audio seul)
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import db

from diarization import diarize
from grouping import group as gmod
from speakers import align, identify
from transcription import transcribe_group as tg

WORKER = int(os.environ.get("MB_WORKER", "1"))
TOTAL = int(os.environ.get("MB_TOTAL_WORKERS", "1"))
MAX_GROUPES = int(os.environ.get("MB_GROUPES", "9999"))
BASE = Path(os.environ.get("MB_BASE") or
            str(config.DB_PATH.parent / f"worker_{WORKER}.sqlite"))
UTILISER_LLM = os.environ.get("MB_LLM", "1") != "0"


def mes_groupes(con) -> list[int]:
    """Les groupes de CE worker, dans l'ordre."""
    r = con.execute(
        "SELECT group_id FROM chunk_group ORDER BY group_id").fetchall()
    return [x["group_id"] for x in r
            if x["group_id"] % TOTAL == WORKER % TOTAL][:MAX_GROUPES]


def traiter(con, gid: int) -> dict:
    """Toutes les étapes sur un groupe. Ne lève pas (règle 3)."""
    t0 = time.time()
    etapes: dict = {}
    try:
        g = con.execute("SELECT statut FROM chunk_group WHERE group_id=?",
                        (gid,)).fetchone()
        etapes["statut_initial"] = g["statut"] if g else "?"

        r = tg.transcrire_groupe(con, gid)
        etapes["transcription"] = r.get("statut", "échec")
        etapes["rtf_asr"] = r.get("rtf")
        if r.get("statut") not in ("ok", "déjà_transcrit"):
            return {"group_id": gid, **etapes, "erreur": r.get("erreur")}

        # Depuis la refonte de la diarisation : plus de pyannote, plus de
        # création de profils. Une seule étape locale décide « le propriétaire » /
        # « contenu » et écarte la musique. `identify.attribuer_profils`
        # fabriquait un profil par voix inconnue — c'est ce qui a produit
        # des dizaines de profils et des heures de calcul pour rien.
        from pipeline_v2 import etape_voix
        r = etape_voix(con, [gid])
        e = (r.get("groupes") or [{}])[0]
        etapes["voix"] = e.get("statut", "ok")
        etapes["proprietaire"] = e.get("proprietaire")
        etapes["musique"] = e.get("musique")
        if e.get("statut") == "echec":
            return {"group_id": gid, **etapes, "erreur": "étape voix en échec"}

        a = align.aligner_groupe(con, gid)
        etapes["interventions"] = len(a["interventions"])
        etapes["couverts"] = a["couverts"]

    except Exception as e:
        etapes["exception"] = str(e)[:200]

    etapes["elapsed"] = round(time.time() - t0, 1)
    etapes["verification"] = gmod.verifier_membres(con, gid).get("ok")
    return {"group_id": gid, **etapes}


def main() -> int:
    print(f"=== worker {WORKER}/{TOTAL} — base {BASE} ===", flush=True)
    config.ensure_dirs()
    con = db.connect(BASE)

    groupes = mes_groupes(con)
    print(f"  {len(groupes)} groupe(s) à traiter : {groupes[:12]}"
          + ("…" if len(groupes) > 12 else ""), flush=True)
    if not groupes:
        print("  rien à faire", flush=True)
        con.close()
        return 0

    t0 = time.time()
    bilan = []
    for n, gid in enumerate(groupes, 1):
        r = traiter(con, gid)
        bilan.append(r)
        print(f"  [{n}/{len(groupes)}] groupe {gid} : "
              f"asr={r.get('transcription')} "
              f"proprietaire={r.get('proprietaire')} musique={r.get('musique')} "
              f"interv={r.get('interventions')} "
              f"couverts={r.get('couverts')} "
              f"{r.get('elapsed', '?')}s"
              + (f"  ERREUR {r['erreur']}" if r.get("erreur") else ""),
              flush=True)

    total = time.time() - t0
    ok = sum(1 for b in bilan if not b.get("erreur"))
    print(f"\n  worker {WORKER} : {ok}/{len(groupes)} groupes en "
          f"{round(total, 1)}s", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())