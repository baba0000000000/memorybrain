"""Reconnaissance des noms de fichiers audio — le point le plus délicat.

L'app a produit TROIS générations de noms selon la période, et Syncthing
a créé des variantes « sync-conflict ». On doit toutes les lire sans
inventer, et sans bloquer (règle 3) : un nom illisible ne fait pas
perdre le fichier, il est signalé.

Générations de noms existantes (durées variables selon la version de
l'application) :
  1. AAAA-MM-JJ_HH-MM-SS_0000.wav              10 min, 4 champs
  2. AAAA-MM-JJ_HH-MM-SS_0000.wav               5 min, 4 champs
  3. AAAA-MM-JJ_HH-MM-SS-753_0000.wav            1 min, 6 champs (ms)
  + variante sync-conflict : <nom>.sync-conflict-20240101-000000-AAAAAAA.wav

Le nom encode l'heure de DÉBUT, donc la position temporelle absolue est
directement lisible (règle 2) — aucune reconstruction à faire.
"""
import re
from datetime import datetime

# 6 champs : AAAA-MM-JJ_HH-MM-SS-mmm_IIII
RE_V6 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ _](?P<time>\d{2}-\d{2}-\d{2})-(?P<ms>\d{3})"
    r"_(?P<idx>\d+)$")

# 4 champs : AAAA-MM-JJ_HH-MM-SS_IIII
RE_V4 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ _](?P<time>\d{2}-\d{2}-\d{2})"
    r"_(?P<idx>\d+)$")

RE_SUFFIX = re.compile(
    r"(?P<stem>.*?)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+$")

# Syncthing peut aussi créer des copies « --1 », « --2 »… quand la cible
# existe déjà. Le nom d'origine reste le même, l'audio est identique.
RE_DUPLICATE = re.compile(r"^(?P<stem>.+?)--(?P<n>\d+)$")

AUDIO_EXT = (".wav", ".ogg", ".mp3", ".m4a", ".flac", ".opus", ".aac")


def strip_sync_conflict(name: str) -> tuple[str, bool]:
    """Renvoie (nom_propre, etait_conflit_syncthing).

    Le conflit Syncthing a RENOMMÉ le fichier local : c'est cette copie-là
    qui a survécu, son nom d'origine n'existe plus. On garde donc le nom
    propre, et on signale le conflit pour que rien ne soit confondu.

    Format produit par Syncthing :
      <nom>.sync-conflict-20240101-000000-AAAAAAA.wav
    (date compacte AAAAMMJJ-HHMMSS, puis le code court du pair)
    """
    base = name.rsplit(".", 1)[0] if "." in name else name
    m = RE_SUFFIX.match(base) or RE_SUFFIX.match(name)
    if m:
        return m.group("stem"), True
    return name, False


def parse(name: str) -> dict:
    """Analyse un nom de fichier audio.

    Renvoie toujours un dict, jamais d'exception (règle 3) :
      ok           : bool   — le nom a-t-il été compris
      recorded_at  : str    — ISO 8601 local, '' si inconnu
      chunk_index  : int    — index dans le nom, None si inconnu
      duration_guess: float — durée probable selon la génération, None sinon
      generation   : str    — 'v6_ms' | 'v4' | 'inconnu'
      sync_conflict: bool
      duplicate    : int    — n° de la copie Syncthing « --N », None sinon
      reason       : str    — pourquoi illisible, si illisible
    """
    out = {"ok": False, "recorded_at": "", "chunk_index": None,
           "duration_guess": None, "generation": "inconnu",
           "sync_conflict": False, "duplicate": None, "reason": ""}

    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem, conflict = strip_sync_conflict(stem)
    out["sync_conflict"] = conflict
    if conflict:
        stem = stem.rsplit(".", 1)[0] if "." in stem else stem

    # copie « --1 », « --2 » créée par Syncthing (nom d'origine identique)
    m_dup = RE_DUPLICATE.match(stem)
    if m_dup:
        out["duplicate"] = int(m_dup.group("n"))
        stem = m_dup.group("stem")
        out["reason"] = f"copie Syncthing --{out['duplicate']}"

    # suffixe de debug posé à la main (« -red », « -fixed »), pas d'origine
    for suf in ("-red", "-fixed", "-dup"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            out["reason"] = (out["reason"] + " | " if out["reason"] else "") \
                + f"suffixe debug {suf!r} retiré"
            break

    m = RE_V6.match(stem)
    if m:
        out["generation"] = "v6_ms"
        out["duration_guess"] = 60.0          # CHECKPOINT_MS = 60 000 (app)
    else:
        m = RE_V4.match(stem)
        if m:
            out["generation"] = "v4"
            out["duration_guess"] = None      # 5 min ou 10 min : mesuré plus tard

    if not m:
        out["reason"] = out["reason"] or "format de nom non reconnu"
        return out

    try:
        ms = m.groupdict().get("ms") or "000"
        dt = datetime.strptime(
            f"{m.group('date')} {m.group('time')}.{ms}", "%Y-%m-%d %H-%M-%S.%f")
        out["recorded_at"] = dt.isoformat()
        out["chunk_index"] = int(m.group("idx"))
        out["ok"] = True
    except ValueError as e:
        out["reason"] = f"date invalide ({e})"
        return out
    return out


def is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXT)
