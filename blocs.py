"""ÉTAPE 0 — LES CASES DE 5 MINUTES : le son est rangé à l'heure.

LE PROBLÈME QUE CE MODULE SUPPRIME
----------------------------------
La V2 collait 5 fichiers bout à bout, puis convertissait une position dans
le WAV en heure réelle en ajoutant une base unique. C'était vrai tant que
les fichiers étaient contigus et de durée fixe. Depuis, le téléphone écrit
des fichiers de durées variables, séparés par des blancs irréguliers.
Résultat : l'heure des phrases dérivait de plusieurs minutes, et
le contrôle trouvait des segments hors de leur chunk.

LA RÈGLE UNIQUE, À LA PLACE DE SEPT CONVERSIONS
-----------------------------------------------
Le son est rangé dans des cases de 5 minutes qui PORTENT l'heure :

    AAAA-MM-JJ_13h45.wav  ->  contient exactement 13:45:00 -> 13:50:00
    AAAA-MM-JJ_13h50.wav  ->  contient exactement 13:50:00 -> 13:55:00

À l'ingestion, chaque fichier du téléphone est POSÉ à sa place dans la
bonne case : s'il commence à 13:47:21, son son va à 2 min 21 s dans la
case 13h45. Les blancs restent des blancs.

Donc, partout dans le programme :

    heure réelle = début de la case + position dans le fichier

Une seule addition, et elle est exacte par construction : le début de la
case est une heure ronde (multiple de 5 minutes), pas un offset accumulé.
Il n'y a plus de table d'offsets, plus de recoupement aux frontières, plus
de chemin inverse.

CE QUE CE MODULE NE FAIT PAS
----------------------------
  - il ne modifie AUCUN fichier du téléphone (règle 17) ;
  - il ne devine pas les sujets ni les conversations (plus le traitement est
    complexe, plus il se trompe) ; si une phrase tombe à
    cheval sur deux cases, le LLM ira voir la case d'avant et celle
    d'après quand il en aura besoin ;
  - il ne supprime aucun blanc : le blanc fait partie de la ligne du temps.

"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

import config

# Durée d'une case, en secondes : 5 minutes.
DUREE_CASE = 300.0

SR = 16000          # Whisper et l'empreinte vocale attendent 16 kHz
EXT = ".wav"


def debut_case(t: float) -> float:
    """Heure ronde (multiple de 5 min) de la case qui contient `t`."""
    return float(int(t // DUREE_CASE) * DUREE_CASE)


def nom_case(t_debut: float) -> str:
    """Nom lisible et triable : la case PORTE l'heure qu'elle contient.

    Exemple : AAAA-MM-JJ_13h45.wav
    Le nom est la seule vérité : relire un fichier suffit à savoir ce qu'il
    contient, sans base de données. C'est ce qui survit à un déménagement
    du projet (leçon des chemins périmés).
    """
    return datetime.fromtimestamp(t_debut).strftime("%Y-%m-%d_%Hh%M") + EXT


def chemin_case(t_debut: float) -> Path:
    return Path(config.WORK_CASES) / nom_case(t_debut)


def cases_dun_chunk(start: float, duree: float) -> list[float]:
    """Les cases qu'un enregistrement traverse (une seule, sauf débordement).

    Un fichier de 70 s qui commence à 13:49:50 déborde sur la case 13h50 :
    on le range dans les deux, sinon 20 s de son disparaîtraient.
    """
    if duree <= 0:
        return [debut_case(start)]
    premiere = debut_case(start)
    derniere = debut_case(start + duree - 0.001)
    out = []
    t = premiere
    while t <= derniere + 0.001:
        out.append(t)
        t += DUREE_CASE
    return out


def _buffer_vide() -> np.ndarray:
    """Une case de 5 minutes de silence, prête à recevoir le son."""
    return np.zeros(int(DUREE_CASE * SR), dtype="float32")


def construire_case(con: sqlite3.Connection, t_debut: float,
                    avec_audio: bool = True) -> dict:
    """Construit la case qui commence à `t_debut` à partir des chunks en base.

    On écrit dans le VIDE uniquement : un silence déjà là ne recouvre jamais
    du son. Deux enregistrements qui se chevauchent sont rares (du silence
    des deux côtés) ; le son gagne toujours.
    """
    import soundfile as sf

    t_fin = t_debut + DUREE_CASE
    chunks = con.execute(
        "SELECT chunk_id, start_time, duration_sec, original_file FROM chunk"
        " WHERE start_time IS NOT NULL AND duration_sec IS NOT NULL"
        "   AND start_time < ? AND start_time + duration_sec > ?"
        " ORDER BY start_time, chunk_id", (t_fin, t_debut)).fetchall()

    if not chunks:
        return {"case": t_debut, "nom": nom_case(t_debut), "chunks": 0,
                "statut": "vide"}

    if not avec_audio:
        return {"case": t_debut, "nom": nom_case(t_debut),
                "chunks": len(chunks), "statut": "sans_audio"}

    buf = _buffer_vide()
    places = 0
    debordements = 0
    manquants = 0
    for c in chunks:
        src = Path(c["original_file"] or "")
        if not src.is_file():
            manquants += 1
            continue
        try:
            x, sr = sf.read(str(src), dtype="float32")
        except Exception:
            manquants += 1
            continue
        if x.ndim > 1:
            x = x.mean(axis=1)
        if sr != SR:                      # corpus déjà en 16 kHz ; on signale
            manquants += 1
            continue

        # ⚠ On ne prend QUE la partie du son qui tombe dans cette case.
        # Un enregistrement de 70 s commencé à 13:49:50 a 10 s dans la case
        # 13h45 et 60 s dans la case 13h50. Écrire « le début du fichier »
        # à la position 0 de la case suivante dupliquait ces 10 s.
        debut_abs = float(c["start_time"])
        duree = float(c["duration_sec"])
        debut_dans_case = max(0.0, t_debut - debut_abs)      # s à sauter
        fin_dans_case = min(duree, t_fin - debut_abs)        # s à garder
        if fin_dans_case - debut_dans_case <= 0:
            continue
        pos = int(round((debut_abs + debut_dans_case - t_debut) * SR))
        if pos < 0 or pos >= len(buf):
            continue
        i0 = int(round(debut_dans_case * SR))
        i1 = min(len(x), int(round(fin_dans_case * SR)))
        morceau = x[i0:i1]
        if len(morceau) == 0:
            continue
        if debut_dans_case > 0 or fin_dans_case < duree:
            debordements += 1             # le reste vit dans la case voisine
        dispo = len(buf) - pos
        morceau = morceau[:dispo]
        cible = buf[pos:pos + len(morceau)]
        vide = (cible == 0.0)             # on n'écrase jamais du son
        cible[vide] = morceau[vide]
        places += 1

    chemin = chemin_case(t_debut)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(chemin), buf, SR, subtype="PCM_16")

    con.execute(
        "INSERT INTO bloc(start_time, path, duree, n_chunks, statut, created_at)"
        " VALUES(?,?,?,?,?,datetime('now'))"
        " ON CONFLICT(start_time) DO UPDATE SET path=excluded.path,"
        " duree=excluded.duree, n_chunks=excluded.n_chunks,"
        " statut=excluded.statut",
        (t_debut, str(chemin), DUREE_CASE, places, "assemblee"))
    con.commit()

    return {"case": t_debut, "nom": nom_case(t_debut), "chunks": places,
            "debordements": debordements, "manquants": manquants,
            "statut": "assemblee", "octets": chemin.stat().st_size}


def assembler(con: sqlite3.Connection, session_id: int | None = None,
              avec_audio: bool = True, depuis: float | None = None,
              jusqu_a: float | None = None) -> dict:
    """Range tous les chunks dans leurs cases. Idempotent (on reconstruit).

    `session_id` limite aux chunks d'une session : c'est ce qu'on utilise
    pour tester sur le lot du jour sans toucher au corpus déjà traité.
    """
    q = ("SELECT chunk_id, start_time, duration_sec FROM chunk"
         " WHERE start_time IS NOT NULL AND duration_sec IS NOT NULL")
    params: list = []
    if session_id is not None:
        q += " AND session_id=?"
        params.append(session_id)
    if depuis is not None:
        q += " AND start_time >= ?"
        params.append(depuis)
    if jusqu_a is not None:
        q += " AND start_time < ?"
        params.append(jusqu_a)
    q += " ORDER BY start_time"

    debuts: set[float] = set()
    for c in con.execute(q, params):
        for d in cases_dun_chunk(float(c["start_time"]),
                                 float(c["duration_sec"])):
            debuts.add(d)

    faits = []
    for d in sorted(debuts):
        faits.append(construire_case(con, d, avec_audio=avec_audio))
    compte: dict = {}
    for r in faits:
        compte[r["statut"]] = compte.get(r["statut"], 0) + 1
    return {"cases": len(faits), "par_statut": compte, "detail": faits}


def heure_absolue(t_debut_case: float, position_wav: float) -> float:
    """LA conversion unique du programme : position dans la case -> heure.

    C'est elle, et rien d'autre, qui remplace les sept conversions de la V2.
    """
    return round(float(t_debut_case) + float(position_wav), 3)


def position_dans_case(t_debut_case: float, heure: float) -> float:
    """Le chemin inverse, aussi simple : heure -> position dans la case."""
    return round(float(heure) - float(t_debut_case), 3)


def verifier_placement(con: sqlite3.Connection, t_debut: float,
                       chunks_temoins: int = 3) -> dict:
    """Contrôle : chaque chunk est-il VRAIMENT à sa place dans la case ?

    On ne se contente pas de vérifier des durées : on compare le SON. Pour
    quelques chunks témoins, on relit un extrait du fichier d'origine et on
    le cherche dans la case, autour de la position attendue. Si la
    corrélation est proche de 1, le placement est juste — c'est la seule
    preuve qui ne peut pas être un artefact de calcul.
    """
    import soundfile as sf

    chemin = chemin_case(t_debut)
    if not chemin.is_file():
        return {"ok": False, "detail": f"case absente : {chemin.name}"}
    case, sr = sf.read(str(chemin), dtype="float32")
    if case.ndim > 1:
        case = case.mean(axis=1)

    t_fin = t_debut + DUREE_CASE
    chunks = con.execute(
        "SELECT chunk_id, start_time, duration_sec, original_file FROM chunk"
        " WHERE start_time IS NOT NULL AND duration_sec IS NOT NULL"
        "   AND start_time < ? AND start_time + duration_sec > ?"
        " ORDER BY duration_sec DESC LIMIT ?",
        (t_fin, t_debut, chunks_temoins)).fetchall()

    resultats = []
    for c in chunks:
        src = Path(c["original_file"] or "")
        if not src.is_file():
            continue
        x, sr_src = sf.read(str(src), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        if sr_src != sr:
            continue

        debut_abs = float(c["start_time"])
        duree = float(c["duration_sec"])
        # ⚠ On ne compare QUE la partie du chunk qui tombe dans cette case :
        # prendre le milieu du fichier d'origine donnait une position
        # négative pour un chunk qui déborde de la case précédente (chunk
        # 1846 : « attendu à -10,985 s »), donc un contrôle faux.
        inter_debut = max(debut_abs, t_debut)
        inter_fin = min(debut_abs + duree, t_fin)
        if inter_fin - inter_debut < 6.0:
            continue
        n = int(5 * sr)
        milieu = (inter_debut + inter_fin) / 2.0
        local = int(round((milieu - debut_abs) * sr)) - n // 2   # dans le chunk
        local = max(0, min(local, len(x) - n))
        extrait = x[local:local + n]
        if len(extrait) < n or np.sqrt((extrait ** 2).mean()) < 0.001:
            continue

        heure_extrait = debut_abs + local / sr
        attendu = int(round((heure_extrait - t_debut) * sr))
        marge = int(0.5 * sr)
        a = max(0, attendu - marge)
        b = min(len(case), attendu + n + marge)
        fenetre = case[a:b]
        if len(fenetre) < n:
            continue

        # corrélation de Pearson glissante, exacte : on divise par l'énergie
        # RÉELLE de la fenêtre à chaque décalage (cumsum), sinon le score
        # sort de [0,1].
        e = extrait - extrait.mean()
        f = fenetre - fenetre.mean()
        norme_e = float(np.linalg.norm(e))
        if norme_e == 0:
            continue
        brut = np.correlate(f, e, "valid")
        carres = np.concatenate(([0.0], np.cumsum(f * f)))
        energie = carres[n:] - carres[:-n]
        denom = np.sqrt(np.maximum(energie, 0.0)) * norme_e
        corr = np.where(denom > 0, brut / np.where(denom > 0, denom, 1.0), 0.0)
        i = int(np.argmax(np.abs(corr)))
        resultats.append({
            "chunk_id": c["chunk_id"],
            # position ABSOLUE dans la case (c'est ça qu'on veut lire)
            "attendu_case_s": round(attendu / sr, 3),
            "trouve_case_s": round((a + i) / sr, 3),
            "attendu_heure": round(heure_extrait, 3),
            "ecart_ms": round((i - (attendu - a)) / sr * 1000, 1),
            "correlation": round(float(corr[i]), 4),
        })

    ok = all(abs(r["ecart_ms"]) <= 20 and r["correlation"] > 0.95
             for r in resultats) if resultats else False
    return {"ok": ok, "case": nom_case(t_debut), "temoirs": resultats}


def cases_a_traiter(con: sqlite3.Connection) -> list[dict]:
    """Les cases présentes dans `work/cases`, avec le nombre de chunks."""
    dossier = Path(config.WORK_CASES)
    if not dossier.is_dir():
        return []
    out = []
    for f in sorted(dossier.glob("*" + EXT)):
        out.append({"fichier": f.name, "octets": f.stat().st_size})
    return out
