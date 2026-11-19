"""Construction de la base de production V2.

Pas un outil jetable : la commande qui prépare la base de travail
(`database/memorybrain.sqlite`) depuis les fichiers ingérés. Elle est
idempotente (règle 19).

    python3 build.py --ingest
    python3 build.py --sessions
    python3 build.py --transcribe 20
    python3 build.py --status
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import db
from ingestion import ingest
from sessions import sessions as sess_mod
from transcription import transcribe

DOSSIERS_AUDIO = (config.V1_DONE, config.V1_AUDIO, config.SYNC_INBOX)


def statut(con) -> None:
    q = con.execute
    print("=== V2 ===")
    print(f"  base            : {config.DB_PATH}")
    for table in ("source", "session", "chunk", "segment",
                  "speaker", "artifact", "pipeline_step"):
        try:
            n = q(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception as e:
            n = f"erreur : {e}"
        print(f"  {table:14} : {n}")
    try:
        v = q("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        print(f"  schema_version  : {v[0] if v else '?'}")
    except Exception:
        pass

    steps = q("SELECT step, status, COUNT(*) c FROM pipeline_step"
              " GROUP BY step, status").fetchall()
    if steps:
        print("\n  étapes :")
        for r in steps:
            print(f"    {r['step']:16} {r['status']:10} {r['c']:5}")

    n_tr = q("SELECT COUNT(*) FROM chunk WHERE status='transcribed'").fetchone()[0]
    n_tot = q("SELECT COUNT(*) FROM chunk").fetchone()[0]
    n_sil = q("SELECT COUNT(*) FROM chunk WHERE status='transcribed'"
              " AND chunk_id NOT IN (SELECT DISTINCT chunk_id FROM segment)").fetchone()[0]
    print(f"\n  transcrits      : {n_tr}/{n_tot}"
          + (f"  (dont {n_sil} silencieux)" if n_sil else ""))

    d = sess_mod.session_summary(con, 5)
    if d:
        print("\n  sessions récentes / longues :")
        for s in d:
            print(f"    s{s['session_id']:<4} {s['started_at'][:16]} → "
                  f"{s['ended_at'][:16]}  {s['chunks']:4} chunks  "
                  f"{s['minutes_reelles']:7.1f} min")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true", help="scanner les audios")
    ap.add_argument("--sessions", action="store_true", help="reconstruire les sessions")
    ap.add_argument("--transcribe", type=int, metavar="N", help="transcrire N chunks")
    ap.add_argument("--model", default=config.MODELE_ASR,
                    help="modèle ASR (défaut : config.MODELE_ASR = large-v3-turbo)")
    ap.add_argument("--status", action="store_true", help="état de la base")
    args = ap.parse_args()

    config.ensure_dirs()
    con = db.connect(config.DB_PATH)
    db.set_schema_version(con)
    src = ingest.register_source(con)

    if args.ingest:
        t0 = time.time()
        total = {"new": 0, "known": 0, "error": 0, "total": 0}
        for dossier in DOSSIERS_AUDIO:
            r = ingest.scan(con, dossier, src)
            print(f"  {dossier.name:16} {r}", flush=True)
            for k in total:
                total[k] += r.get(k, 0)
        print(f"  ingestion terminée en {time.time()-t0:.0f}s : {total}")

    if args.sessions:
        r = sess_mod.build_sessions(con)
        print(f"  sessions : {r}")

    if args.transcribe:
        t0 = time.time()
        c = transcribe.transcribe_pending(
            con, limit=args.transcribe, model=args.model,
            progress=lambda i, n, r: print(
                f"    {i}/{n} {r.get('statut')} seg={r.get('segments','-')} "
                f"{r.get('elapsed','-')}s", flush=True))
        print(f"  transcription : {c} en {time.time()-t0:.0f}s")

    if args.status or not (args.ingest or args.sessions or args.transcribe):
        statut(con)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())