"""Ingestion : le son entre dans la mémoire.

Ce module fait UNE chose : transformer un fichier audio en ligne `chunk`
dans la base, avec le minimum d'interprétation. C'est le début de la
chaîne, donc il ne doit jamais rien inventer ni rien bloquer (règle 3).

Ce qu'il ne fait PAS, volontairement :
  - il ne devine pas la session   (règle 8, §8 : on ne devine pas)
  - il ne devine pas le locuteur  (règle 3)
  - il ne décide pas si c'est une tâche, une idée ou une note
    (ça vient en Phase 7-8, après transcription et diarisation)

Ce qu'il garantit :
  - RÈGLE 19 : rejouer l'ingestion ne crée aucun doublon (SHA-256 unique)
  - RÈGLE 2  : start_time est ABSOLU dans la source, jamais relatif
  - RÈGLE 3  : un fichier au nom illisible est enregistré quand même,
                avec recorded_at=NULL et le motif dans `error`
  - RÈGLE 17 : l'audio d'origine ne bouge JAMAIS. On ne lit que.

L'audio reste où il est (`pipeline/done` ou `syncthing-inbox`). La V2
enregistre des POINTERS, elle ne copie pas des dizaines de Go.
"""
import hashlib
import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path

import config
import db
from ingestion.naming import is_audio, parse

# Un fichier audio plus long que ça n'est pas un chunk du téléphone.
MAX_CHUNK_SEC = 900.0

# Lecture par blocs pour le SHA-256 : un WAV de 60 s fait ~2 Mo, un film
# ferait des Gio. On ne lit jamais plus de MAX_HASH_BYTES.
MAX_HASH_BYTES = 200 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path, max_bytes: int = MAX_HASH_BYTES) -> str:
    """SHA-256 du fichier. Volontairement calculé sur les OCTETS DU FICHIER,
    pas sur le contenu décodé : deux WAV identiques ré-encodés doivent être
    considérés comme deux fichiers distincts, on ne devine pas (règle 3)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    # si le fichier dépasse la limite, on le signale dans le hash pour
    # qu'aucun chunk différent ne puisse partager le même identifiant
    if path.stat().st_size > max_bytes:
        h.update(f"|TRUNCATED:{path.stat().st_size}".encode())
    return h.hexdigest()


def audio_duration(path: Path) -> float | None:
    """Durée en secondes, tous formats. None si illisible — on ne devine pas.

    Deux lecteurs, parce que le corpus est mixte :
      - WAV non compressé : le module wave de la stdlib, zéro dépendance
      - OGG/Opus, MP3, FLAC : soundfile (libsndfile), ou ffprobe en secours
    """
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:
        pass

    try:
        import soundfile
        info = soundfile.info(str(path))
        if info.samplerate:
            return round(info.frames / float(info.samplerate), 3)
    except Exception:
        pass

    # ffprobe : slower mais couvre tout ce que soundfile refuse
    try:
        import subprocess
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        val = r.stdout.strip()
        if r.returncode == 0 and val:
            return round(float(val), 3)
    except Exception:
        pass
    return None


def absolute_start(recorded_at: str, idx: int | None) -> float | None:
    """Position du chunk dans la source, en secondes (règle 2).

    On utilise le timestamp DU NOM DE FICHIER comme position absolue.

    Pourquoi pas `idx * 60`, qui semble évident : le compteur chunk_index
    redémarre à 0 à chaque nouvelle session, plusieurs fois par jour. Deux
    chunks proches en index peuvent donc être séparés de plusieurs heures,
    et idx*60 dérive de façon arbitraire. C'est une INVENTION, et la
    règle 3 l'interdit.

    Le timestamp du nom, lui, est solide : les chunks consécutifs d'un
    même enregistrement sont espacés d'exactement 60 s — c'est la
    signature d'un enregistrement continu. Et les coupures sont nettes (5 min,
    10 min : les anciennes générations de chunks).

    On note donc `recorded_at` comme position absolue du chunk, et
    l'index reste un simple numéro d'ordre, jamais une durée.
    """
    if not recorded_at:
        return None
    try:
        t = datetime.fromisoformat(recorded_at)
    except ValueError:
        return None
    return round(t.timestamp(), 3)


def register_source(con: sqlite3.Connection, kind: str = "microphone",
                    label: str = "téléphone") -> int:
    """La source 'micro téléphone'. Idempotent (règle 19)."""
    con.execute(
        "INSERT OR IGNORE INTO source(kind, label, uri, first_seen)"
        " VALUES(?,?,?,?)", (kind, label, "syncthing:memorybrain-audio", now_iso()))
    row = con.execute(
        "SELECT source_id FROM source WHERE kind=? AND label=?",
        (kind, label)).fetchone()
    return row["source_id"]


def ingest_one(con: sqlite3.Connection, path: Path,
               source_id: int | None = None) -> tuple[int, str]:
    """Enregistre un chunk. Renvoie (chunk_id, statut).

    statut = 'new'     inséré à l'instant
            'known'  déjà présent, rien fait (règle 19)
            'skipped' pas un fichier audio
            'error'   illisible — le motif est dans chunk.error

    Ne lève jamais (règle 3) : un fichier foireux est signalé, pas fatal.
    """
    if not is_audio(path.name):
        return 0, "skipped"

    try:
        file_hash = sha256(path)
    except OSError as e:
        return 0, f"error"

    # déjà connu ? (règle 19)
    row = con.execute("SELECT chunk_id FROM chunk WHERE hash=?", (file_hash,)).fetchone()
    if row:
        return row["chunk_id"], "known"

    info = parse(path.name)
    recorded_at = info["recorded_at"] or ""
    duration = audio_duration(path)
    start = absolute_start(recorded_at if info["ok"] else "",
                           info["chunk_index"] if info["ok"] else None)
    end = round(start + duration, 3) if (start is not None and duration) else None

    # on garde trace de TOUT ce qui a été appris sur le nom, même si
    # le fichier est valide : un nom de conflit Syncthing doit rester
    # visible bien plus tard (règle 3)
    bits = []
    if info["sync_conflict"]:
        bits.append("sync-conflict Syncthing")
    if info["duplicate"]:
        bits.append(f"copie Syncthing --{info['duplicate']}")
    if info["reason"]:
        bits.append(info["reason"])

    if not info["ok"] and not recorded_at:
        recorded_at = "1970-01-01T00:00:00"  # placeholder : jamais inventé
        bits.append("date inconnue")

    if duration is None:
        bits.append("durée illisible")

    note = " | ".join(bits) if bits else None

    try:
        cur = con.execute(
            "INSERT INTO chunk(session_id, source_id, recorded_at, chunk_index,"
            " start_time, end_time, duration_sec, original_file, hash, status, note, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (None, source_id, recorded_at, info["chunk_index"], start, end,
             duration, str(path), file_hash, "pending", note, now_iso()))
        con.commit()
        return (cur.lastrowid or 0), "new"
    except sqlite3.IntegrityError as e:
        # Règle 19 : course possible, le même hash est arrivé entre-temps.
        # On relit — mais si la ligne n'existe toujours pas, c'est une
        # VRAIE erreur d'insertion : il ne faut surtout pas la déguiser en
        # "déjà connu", sinon le fichier disparaît sans bruit (règle 3).
        row = con.execute("SELECT chunk_id FROM chunk WHERE hash=?", (file_hash,)).fetchone()
        if row:
            return row["chunk_id"], "known"
        con.rollback()
        print(f"[ingest] REFUSÉ : {path.name} -> {e}")
        return 0, f"erreur d'insertion : {e}"


def scan(con: sqlite3.Connection, folder: Path, source_id: int | None = None,
         limit: int | None = None, progress=None) -> dict:
    """Scanne un dossier et enregistre tous les chunks.

    Ne déplace, ne supprime, ne modifie AUCUN fichier.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return {"erreur": f"dossier absent : {folder}", "new": 0,
                "known": 0, "error": 0, "total": 0}

    noms = sorted(p for p in folder.iterdir() if is_audio(p.name))
    if limit:
        noms = noms[:limit]

    compte = {"new": 0, "known": 0, "skipped": 0, "error": 0, "total": len(noms)}
    for i, p in enumerate(noms, 1):
        _, statut = ingest_one(con, p, source_id)
        if statut == "error":
            compte["error"] += 1
        elif statut in compte:
            compte[statut] += 1
        if progress and i % 100 == 0:
            progress(i, len(noms), compte)
    return compte