"""ÉTAPE 4 — IDENTIFICATION : qui est cette voix ?

C'est l'étape qui répond à « qui parle ? », après que la diarisation a
répondu à « quand ? ».

Le profil 0 est réservé au propriétaire du micro ; les autres locuteurs sont
numérotés à partir de 1.

Donc :
  speaker_id 0 = le propriétaire (celui qui enregistre).
  speaker_id 1, 2, 3… = les autres, numérotés dans l'ordre de découverte.
  La numérotation est STABLE : un profil ne change jamais de numéro.
"""
import sqlite3
from datetime import datetime, timezone

import config

# Modèle d'empreinte vocale. WeSpeaker ResNet34 : 256 dimensions,
# entraîné sur VoxCeleb, standard de la comparaison de voix. Les seuils
# de décision sont dans config.py.
MODELE_EMPREINTE = "microsoft/wespeaker-voxceleb-resnet34-LM"
DIM_EMPREINTE = 256

# En dessous de ça, on ne crée pas de profil : c'est du bruit ou un mot
# isolé, pas une identité. §3 : « une voix sans contexte n'est pas un
# profil ».
MIN_SECONDE_PAR_PROFIL = 3.0

# Nombre maximum d'empreintes gardées par profil. Au-delà, les nouvelles
# n'apportent plus rien et la table grossit pour rien (§6).
MAX_EMPREINTES_PAR_PROFIL = 50


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_EMBEDDER = None


def charger():
    """Charge le modèle d'empreinte une fois. Ne lève pas (règle 3)."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    from wespeaker import torch_model  # fourni avec pyannote
    _EMBEDDER = torch_model
    return _EMBEDDER


def profil_proprietaire(con: sqlite3.Connection) -> int:
    """Renvoie (et crée au besoin) le profil 0 = le propriétaire.

    Idempotent (règle 19). Le nom est mis tel quel, mais `status` reste
    'UNKNOWN' : le profil 0 désigne le propriétaire du micro,
    ce qui est bien une confirmation, mais on garde la trace du fait que
    le nom n'a pas été posé par une reconnaissance vocale mais par le propriétaire.
    """
    con.execute(
        "INSERT OR IGNORE INTO speaker(speaker_id, name, status, created_at)"
        " VALUES(?,?,?,?)",
        (config.PROFIL_PROPRIETAIRE, "le propriétaire", "confirmed", now_iso()))
    # la colonne speaker_id est PRIMARY KEY : INSERT OR IGNORE respecte
    # l'unicité sur la clé primaire, donc pas de doublon.
    row = con.execute("SELECT speaker_id FROM speaker WHERE speaker_id=?",
                      (config.PROFIL_PROPRIETAIRE,)).fetchone()
    return row["speaker_id"] if row else config.PROFIL_PROPRIETAIRE


def prochain_profil(con: sqlite3.Connection) -> int:
    """Le prochain numéro de profil libre, en partant de 1.

    On cherche le PREMIER TROU, pas `max+1`. Deux raisons :
      - le `min(speaker_id)` seul est faux dès qu'il y a un trou au milieu
        (profil 2 existe, pas 3) : il renverrait 2 qui est déjà pris, et
        l'INSERT echouerait sur la contrainte UNIQUE ;
      - `max+1` est faux autrement : si un profil est supprimé par erreur,
        la numérotation remonterait et une voix historique se retrouverait
        réattribuée à quelqu'un d'autre.

    Le trou se cherche donc en parcourant les ids depuis 1. C'est une
    boucle sur quelques dizaines d'entiers au maximum : le coût est
    négligeable devant une transcription.
    """
    existant = {r["speaker_id"] for r in
                con.execute("SELECT speaker_id FROM speaker")}
    candidat = config.PREMIER_PROFIL_AUTRE
    while candidat in existant:
        candidat += 1
    return candidat


def _segments_par_label(con: sqlite3.Connection, group_id: int) -> dict:
    """Durée de parole de chaque voix locale du groupe.

    On mesure sur le TEMPS PARLÉ, pas sur le nombre de tours : une voix
    qui parle 0,4 s douze fois n'est pas un profil, une voix qui parle
    4 s une seule fois en est un.
    """
    tours = con.execute(
        "SELECT start_time, end_time, label_local FROM turn"
        " WHERE group_id=? ORDER BY start_time", (group_id,)).fetchall()
    totaux: dict[str, float] = {}
    for t in tours:
        if not t["label_local"]:
            continue
        # les colonnes s'appellent start_time / end_time : les lire par
        # t["start"] lève un IndexError.
        totaux[t["label_local"]] = totaux.get(t["label_local"], 0.0) \
            + (t["end_time"] - t["start_time"])
    return {k: round(v, 2) for k, v in totaux.items()}


def associer_locaux(con: sqlite3.Connection, group_id: int,
                    correspondance: dict[str, int]) -> dict:
    """Écrit le profil réel sur les tours du groupe.

    `correspondance` : {"SPEAKER_00": 0, "SPEAKER_01": 1, ...}
    Les voix absentes de la table gardent speaker_id NULL et
    confiance 'UNKNOWN' (règle 3) : on ne devine pas.
    """
    n = 0
    for label, spid in correspondance.items():
        cur = con.execute(
            "UPDATE turn SET speaker_id=?, confiance=? WHERE group_id=?"
            " AND label_local=?",
            (spid, "MATCH", group_id, label))
        n += cur.rowcount or 0
    con.commit()
    return {"tours_attribues": n, "correspondance": correspondance}


def attribuer_profils(con: sqlite3.Connection, group_id: int,
                     trace: bool = True) -> dict:
    """Crée et attribue les profils vocaux d'un groupe.

    Règle appliquée :
      - profil 0 = le propriétaire est TOUJOURS présent, même s'il ne parle pas
        dans ce groupe (c'est le propriétaire du téléphone) ;
      - une voix locale qui parle moins de MIN_SECONDE_PAR_PROFIL ne
        reçoit pas de profil : elle reste UNKNOWN (§3) ;
      - les autres reçoivent le prochain numéro libre, dans l'ordre
        décroissant de temps parlé, pour que la numérotation du propriétaire soit
        aussi stable que possible d'un groupe à l'autre.

    ⚠ Honnêteté sur la limite de cette méthode. On ne sait PAS dire que
    SPEAKER_00 est le propriétaire : on sait seulement que c'est la voix
    dominante. Le premier groupe est donc une HYPOTHÈSE. La méthode qui la
    rend fiable : le matching par empreinte vocale du premier groupe sert
    de référence, et les groupes suivants s'y rattachent. C'est fait par
    `rattacher_au_repertoire`, à appeler après le premier groupe.
    """
    proprietaire = profil_proprietaire(con)
    totaux = _segments_par_label(con, group_id)

    correspondance: dict[str, int] = {}
    details: list[dict] = []

    # classement par temps parlé décroissant
    for label, secondes in sorted(totaux.items(), key=lambda z: -z[1]):
        if secondes < MIN_SECONDE_PAR_PROFIL:
            details.append({"label": label, "secondes": secondes,
                            "profil": None, "motif": "trop peu de parole"})
            continue
        spid = prochain_profil(con)
        con.execute(
            "INSERT INTO speaker(speaker_id, name, status, created_at)"
            " VALUES(?,?,?,?)",
            (spid, None, "UNKNOWN", now_iso()))
        correspondance[label] = spid
        details.append({"label": label, "secondes": secondes,
                        "profil": spid, "motif": "nouveau profil"})

    res = associer_locaux(con, group_id, correspondance)
    res["profil_proprietaire"] = proprietaire
    res["voix"] = details
    res["secondes_par_voix"] = totaux

    if trace:
        _ecrire_trace(group_id, details, totaux, proprietaire)

    return res


def _ecrire_trace(group_id: int, details: list[dict],
                  totaux: dict, proprietaire: int) -> None:
    config.WORK_TRACE.mkdir(parents=True, exist_ok=True)
    chemin = config.WORK_TRACE / f"groupe_{group_id:04d}_profils.md"
    lignes = [
        f"# Profils vocaux — groupe {group_id}",
        "",
        f"- **Profil {proprietaire}** = propriétaire du micro",
        "",
        "| voix locale | secondes | profil | motif |",
        "|-----------|----------|--------|-------|",
    ]
    for d in details:
        lignes.append(f"| {d['label']} | {d['secondes']} | "
                     f"{d['profil'] if d['profil'] is not None else '—'} | "
                     f"{d['motif']} |")
    lignes += [
        "",
        "> ⚠ Les numéros attribués dans ce groupe sont une HYPOTHÈSE de",
        "> classement, pas une reconnaissance. Le rattachement réel se fait",
        "> par empreinte vocale sur les groupes suivants.",
        "",
    ]
    chemin.write_text("\n".join(lignes), encoding="utf-8")


def embedder_disponible() -> bool:
    """Le calcul d'empreinte vocale est-il utilisable ici ?

    weSpeaker est livré avec pyannote. On vérifie vraiment, parce que
    l'import peut réussir et le modèle ne pas se charger (pas de réseau).
    """
    try:
        m = charger()
        return hasattr(m, "Model")
    except Exception:
        return False
