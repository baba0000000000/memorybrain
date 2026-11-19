"""Sessions : regrouper les chunks en périodes de vie continues.

Ce module ne DÉVINE PAS les sessions. Il applique une règle empirique, et
il laisse `session_id = NULL` quand il ne peut pas trancher (règle 3).

LA RÈGLE (§2, règle 2)
----------------------
Les chunks du téléphone arrivent à cadence régulière — une minute — et les
générations plus anciennes à 5 puis 10 minutes. Les écarts entre deux
chunks consécutifs se répartissent donc en populations nettes (60 s,
300 s, 600 s) ; les écarts irréguliers viennent du VAD, qui fait écrire
deux fois dans la même minute.

Le code ne dépend d'aucune valeur mesurée : il classe les écarts par ordre
de grandeur. Il n'y a pas de continuum entre 60 et 300 s, donc un écart de
60 s et un écart de 300 s ne peuvent pas venir du même comportement, et on
PEUT trancher sans deviner.

SEUIL DE COUPURE = 600 s (10 min)
    en dessous  → même session (le téléphone écrivait sans interruption)
    au dessus   → nouvelle session (le téléphone s'est arrêté)

Ce seuil n'est pas un chiffre sorti du plafond : il tombe dans le trou qui
sépare les populations. Entre 300 et 600 s il n'y a que des transitions de
génération de chunks, pas du silence.

Ce que ce module ne fait PAS :
  - il ne regarde pas le contenu parlé (trop tôt, pas encore transcrit)
  - il ne devine pas le thème de la session
  - il ne fusionne pas deux jours différents
"""
import sqlite3
from datetime import datetime, timedelta

# Les chunks sont produits par blocs de 5 ou 10 min : au-delà de 600 s de
# silence entre deux chunks, c'est une coupure et non une continuation.
SEUIL_COUPURE_S = 600.0

# Une session ne peut pas s'étaler sur plus de 24 h : au-delà c'est
# qu'on a manqué des données, pas qu'une session a duré un jour.
DUREE_MAX_S = 24 * 3600.0


def _dt(row) -> datetime | None:
    try:
        return datetime.fromisoformat(row["recorded_at"])
    except (ValueError, TypeError):
        return None


def _nouvelle_session(con: sqlite3.Connection, debut: str, fin: str,
                      note: str | None) -> int:
    cur = con.execute(
        "INSERT INTO session(source_id, started_at, ended_at, note, created_at)"
        " VALUES((SELECT source_id FROM source LIMIT 1),?,?,?,datetime('now'))",
        (debut, fin, note))
    return cur.lastrowid or 0


def build_sessions(con: sqlite3.Connection, assign: bool = True) -> dict:
    """Reconstruit les sessions et (si assign) rattache les chunks.

    Idempotent : les sessions existantes sont supprimées puis reconstruites,
    donc relancer ne crée pas de doublon (règle 19).

    Renvoie un compte rendu.
    """
    chunks = con.execute(
        "SELECT chunk_id, recorded_at, start_time FROM chunk"
        " WHERE recorded_at != '1970-01-01T00:00:00'"
        " ORDER BY start_time").fetchall()

    if not chunks:
        return {"sessions": 0, "chunks": 0, "orphelins": 0}

    # 1. couper en séries sur l'écart entre positions absolues
    series: list[list] = []
    courante = [chunks[0]]
    for prec, act in zip(chunks, chunks[1:]):
        if act["start_time"] is None or prec["start_time"] is None:
            # un chunk sans position ne peut pas être placé : nouvelle série
            series.append(courante)
            courante = [act]
            continue
        ecart = act["start_time"] - prec["start_time"]
        if ecart <= SEUIL_COUPURE_S and ecart >= -SEUIL_COUPURE_S:
            courante.append(act)
        else:
            series.append(courante)
            courante = [act]
    series.append(courante)

    # 2. une série qui dépasse DUREE_MAX est suspecte (données manquantes)
    for s in series:
        if len(s) >= 2:
            duree = s[-1]["start_time"] - s[0]["start_time"]
            if duree > DUREE_MAX_S:
                # on la coupe en deux : mieux vaut 2 sessions fausses
                # qu'une session de 3 jours qui n'existe pas (règle 4)
                moitie = len(s) // 2
                idx = series.index(s)
                series[idx:idx + 1] = [s[:moitie], s[moitie:]]

    compte = {"sessions": 0, "chunks": 0, "orphelins": 0}
    con.execute("UPDATE chunk SET session_id = NULL")

    for s in series:
        if not s:
            continue
        note = None
        if len(s) == 1:
            note = "session d'un seul chunk — continuité non établie"
        session_id = _nouvelle_session(con, s[0]["recorded_at"],
                                       s[-1]["recorded_at"], note)
        compte["sessions"] += 1
        compte["chunks"] += len(s)
        if assign:
            con.executemany("UPDATE chunk SET session_id=? WHERE chunk_id=?",
                            [(session_id, r["chunk_id"]) for r in s])
    con.commit()

    # les chunks sans position ne sont pas oubliés, ils sont comptés
    compte["orphelins"] = con.execute(
        "SELECT COUNT(*) FROM chunk WHERE session_id IS NULL").fetchone()[0]
    return compte


def session_summary(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Les sessions les plus longues — un premier regard sur la vie.

    ATTENTION à la lecture de `minutes` : c'est la somme des durées des
    chunks, donc elle COMPTE les encodages multiples. Un même
    enregistrement présent en .ogg ET en .wav (même audio, deux encodages,
    hashes différents donc deux chunks) est compté deux fois. Le premier
    `minutes_reelles` est la durée de la session dans le temps.
    """
    return [dict(r) for r in con.execute(
        "SELECT s.session_id, s.started_at, s.ended_at,"
        "       COUNT(c.chunk_id) AS chunks,"
        "       ROUND(SUM(c.duration_sec)/60.0, 1) AS minutes,"
        "       ROUND((julianday(s.ended_at) - julianday(s.started_at))*1440.0, 1)"
        "         AS minutes_reelles,"
        "       s.note"
        " FROM session s LEFT JOIN chunk c ON c.session_id = s.session_id"
        " GROUP BY s.session_id"
        " ORDER BY minutes_reelles DESC LIMIT ?", (limit,))]


def session_timeline(con: sqlite3.Connection, session_id: int) -> dict:
    """Reconstitue une session : les chunks dans l'ordre, avec le texte."""
    s = con.execute("SELECT * FROM session WHERE session_id=?",
                    (session_id,)).fetchone()
    if s is None:
        return {}
    chunks = [dict(r) for r in con.execute(
        "SELECT chunk_id, recorded_at, start_time, end_time, duration_sec,"
        "       original_file, status FROM chunk WHERE session_id=?"
        " ORDER BY start_time", (session_id,))]
    for c in chunks:
        c["segments"] = [dict(r) for r in con.execute(
            "SELECT start_time, end_time, text, speaker_id FROM segment"
            " WHERE chunk_id=? ORDER BY idx", (c["chunk_id"],))]
    return {"session": dict(s), "chunks": chunks}