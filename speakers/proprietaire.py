"""Une seule question : est-ce la voix du propriétaire ?

Pourquoi ce module existe
-------------------------
La V2 demandait à pyannote de segmenter puis à `rattacher` de nommer chaque
locuteur, en CRÉANT un profil neuf quand aucune empreinte ne correspondait.
Résultat : des dizaines de profils pour un seul locuteur nommé, des
milliers de tours tous étiquetés « MATCH » (le compteur validait sa propre
invention), et un coût CPU plusieurs fois supérieur à la durée de l'audio.

Or une seule chose compte : savoir quand PROPRIETAIRE parle. Le reste est du
contenu — émissions, vidéos, musique — que rien n'oblige à identifier.

La décision se prend donc ici, sans diarisation, par fenêtres glissantes :

  * chaque fenêtre de 2,5 s donne une empreinte (ResNet34, 26 ms CPU) ;
  * le score d'une fenêtre est le MEILLEUR cosinus contre le noyau du
    profil (plusieurs empreintes : micro du téléphone, distance, bruit) ;
  * un segment est « le propriétaire » si une part suffisante de ses fenêtres passe
    le seuil fort.

Pourquoi par fenêtres et pas sur le tour entier : quand il parle par-dessus
une vidéo, l'empreinte du tour entier est la MOYENNE de deux voix — elle ne
ressemble plus à personne. À niveau sonore égal avec le contenu, une part
des fenêtres reste au-dessus du seuil alors que la moyenne du tour tombe.
Le vote de présence récupère la voix qu'un tour moyenné perd.

Seuils : voir config.py §8.
"""
from __future__ import annotations

import sqlite3

import numpy as np

import config
from speakers import voix

# Cache du noyau par connexion : le recalculer à chaque segment coûterait
# un aller-retour SQLite et une normalisation par segment.
_NOYAU: dict[int, np.ndarray] = {}


def _norme(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def noyau(con: sqlite3.Connection, rafraichir: bool = False) -> np.ndarray:
    """Empreintes du profil le propriétaire, sans les contaminées.

    Le profil 0 peut contenir des voix de média (des paires sous le seuil
    de match, dont certaines à cosinus négatif) : ces intrus faisaient
    reconnaître « le propriétaire » dans des émissions. On élague par
    similarité au centre (0,55), six passes au plus, ce qui suffit à isoler
    un noyau cohérent sans superviseur.
    """
    cle = id(con)
    if not rafraichir and cle in _NOYAU:
        return _NOYAU[cle]
    lignes = con.execute(
        "SELECT vecteur, dim FROM speaker_embedding WHERE speaker_id=?",
        (config.PROFIL_PROPRIETAIRE,)).fetchall()
    if not lignes:
        raise RuntimeError(
            "profil le propriétaire vide : lancer outils/amorcer_profil_proprietaire.py")
    V = np.array([_norme(np.frombuffer(r[0], dtype="float32")[:r[1]])
                  for r in lignes])
    garde = np.ones(len(V), dtype=bool)
    for _ in range(6):
        centre = _norme(V[garde].mean(axis=0))
        nouveau = (V @ centre) >= 0.55
        if nouveau.sum() == garde.sum() or nouveau.sum() < 3:
            break
        garde = nouveau
    _NOYAU[cle] = V[garde]
    return _NOYAU[cle]


def oublier_cache() -> None:
    _NOYAU.clear()


def analyser(x: np.ndarray, sr: int, con: sqlite3.Connection) -> dict:
    """Verdict sur un segment (ou un tour) complet."""
    base = noyau(con)
    if x.ndim > 1:
        x = x.mean(axis=1)
    win = int(config.FENETRE_VOIX * sr)
    pas = int(config.PAS_VOIX * sr)
    if len(x) < win:
        bornes = [0] if len(x) >= config.MIN_EXTRAIT_EMPREINTE * sr else []
    else:
        bornes = list(range(0, len(x) - win + 1, pas))
    sc = []
    for i in bornes:
        e = voix.empreinte(x[i:i + win], sr)
        if e is not None:
            sc.append(float((base @ _norme(np.asarray(e))).max()))
    if not sc:
        return {"verdict": "autre", "part": 0.0, "part_grise": 0.0,
                "score": 0.0, "score_med": 0.0, "fenetres": 0}
    sc = np.array(sc)
    part = float((sc >= config.SEUIL_PROPRIETAIRE).mean())
    part_grise = float(((sc >= config.SEUIL_PROPRIETAIRE_GRIS) &
                        (sc < config.SEUIL_PROPRIETAIRE)).mean())
    if part >= config.PART_PROPRIETAIRE:
        verdict = "proprietaire"
    elif part + part_grise >= max(0.40, config.PART_PROPRIETAIRE):
        verdict = "probable"
    else:
        verdict = "autre"
    return {"verdict": verdict, "part": round(part, 3),
            "part_grise": round(part_grise, 3),
            "score": round(float(sc.max()), 3),
            "score_med": round(float(np.median(sc)), 3),
            "fenetres": len(sc)}


def attribut(analyse: dict) -> tuple[int | None, str]:
    """(speaker_id, confiance) prêts pour `turn` ou `segment`."""
    v = analyse["verdict"]
    if v == "proprietaire":
        return config.PROFIL_PROPRIETAIRE, "MATCH"
    if v == "probable":
        return config.PROFIL_PROPRIETAIRE, "PROBABLE"
    return None, "UNKNOWN"


def enroler(con: sqlite3.Connection, x: np.ndarray, sr: int,
            group_id: int | None = None, condition: str = "auto") -> bool:
    """Ajoute une empreinte au profil, sous garde-fou.

    Deux règles, apprises du désastre précédent :

    1. on n'ajoute QUE si l'extrait ressemble déjà fortement au noyau
       (>= SEUIL_ENROLEMENT, 0,75) : c'est lui, pas une voix voisine ;
    2. on n'ajoute pas de doublon (cosinus >= 0,90 avec une empreinte
       existante) : 50 empreintes du même passage n'apportent rien.

    Ce que ça apporte vraiment : les conditions techniques. Près du micro,
    à distance, au casque — chaque condition a sa signature acoustique et la
    moyenne des unes ne vaut pas l'ensemble.
    """
    a = analyser(x, sr, con)
    if a["score"] < config.SEUIL_ENROLEMENT:
        return False
    if x.ndim > 1:
        x = x.mean(axis=1)
    win = min(len(x), int(config.FENETRE_VOIX * sr))
    e = voix.empreinte(x[:win], sr)
    if e is None:
        return False
    e = _norme(np.asarray(e, dtype="float32"))
    if len(noyau(con)) >= config.MAX_EMPREINTES_PROPRIETAIRE:
        return False
    existantes = con.execute(
        "SELECT vecteur, dim FROM speaker_embedding WHERE speaker_id=?",
        (config.PROFIL_PROPRIETAIRE,)).fetchall()
    for vec, dim in existantes:
        if float(np.dot(e, _norme(np.frombuffer(vec, dtype="float32")[:dim]))) >= 0.90:
            return False
    con.execute(
        "INSERT INTO speaker_embedding(speaker_id, group_id, vecteur, dim,"
        " model, created_at) VALUES(?,?,?,?,?,datetime('now'))",
        (config.PROFIL_PROPRIETAIRE, group_id, e.astype("float32").tobytes(),
         len(e), f"wespeaker-resnet34|{condition}"))
    con.commit()
    oublier_cache()
    return True


def etat(con: sqlite3.Connection) -> dict:
    """Diagnostic du profil, pour `outils/auditer_voix.py`."""
    V = noyau(con, rafraichir=True)
    C = V @ V.T
    hors = C[~np.eye(len(V), dtype=bool)]
    return {"empreintes_noyau": int(len(V)),
            "cos_moyen": round(float(hors.mean()), 3),
            "cos_min": round(float(hors.min()), 3),
            "profils_en_base": con.execute(
                "SELECT COUNT(*) FROM speaker").fetchone()[0]}
