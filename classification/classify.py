"""ÉTAPE 7 — CLASSIFICATION : PERSONNEL, MEDIA ou BUSINESS ?

Trois classes (§12) :

  PERSONNEL — propos tenus par le porteur du micro à la première personne,
              hors contenu écouté. C'est ce qui remplit le graphe de mémoire.
  MEDIA     — une émission, un podcast, une vidéo YouTube, de la musique.
              Ce n'est pas la voix du propriétaire : c'est du contenu qu'il écoute.
              Important : ici on n'extrait PAS d'idées, sinon la mémoire se
              remplit de ce qu'un YouTubeur a dit.
  BUSINESS  — clients, devis, matériel, travail, argent.
  UNKNOWN   — quand on ne sait pas (règle 3). Jamais forcé.

Comment on décide, du plus fiable au moins fiable :

  1. **La source.** Un chunk qui vient de YouTube est MEDIA, c'est
     certain. C'est le seul signal sur lequel on peut s'appuyer sans
     deviner.
  2. **Le contenu.** Des mots comme « client », « devis », « facture »
     pointent vers BUSINESS. C'est un indice, pas une preuve.
  3. **Le ton.** Un texte de plus de 120 mots avec des tournures de
     narration ressemble à une émission.

Ce qui n'est PAS fait ici, volontairement : aucun appel au LLM. La
classification est déterministe, donc explicable et rejouable. Un LLM
qui propose « BUSINESS » pour une phrase ambiguë n'apporte rien et
coûte du temps. Le LLM intervient en étape 8, sur l'extraction.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone

import config

# Mots qui orientent vers BUSINESS. Liste courte et explicite : on préfère
# « UNKNOWN » à une classification fondée sur une liste de 200 mots.
RE_BUSINESS = re.compile(
    r"\b(client|clients|devis|facture|facturer|contrat|commande|"
    r"livraison|fournisseur|tarif|prestation|facturation|"
    r"chiffre d'affaires|budget|investissement|assurance|comptabilité)\b", re.I)

# Mots qui orientent vers PERSONNEL.
RE_PERSONNEL = re.compile(
    r"\b(famille|maman|papa|ma mère|mon père|"  # no-fuite : vocabulaire de détection
    r"médicament|medicament|docteur|hôpital|hopital|"
    r"vacances|anniversaire|école|ecole|courses|voiture|"
    r"appartement|maison)\b", re.I)

# Signes de narration / podcast : tournures typiques d'une émission.
RE_NARRATION = re.compile(
    r"\b(selon nous|podcast|abonnés|abonnez|vous êtes|"
    r"dans cette vidéo|on va voir|il faut savoir que)\b", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mots_cles(textes: list[str]) -> dict:
    texte = " ".join(textes).lower()
    return {
        "business": len(RE_BUSINESS.findall(texte)),
        "personnel": len(RE_PERSONNEL.findall(texte)),
        "narration": len(RE_NARRATION.findall(texte)),
    }


def classer_groupe(con: sqlite3.Connection, group_id: int) -> dict:
    """Classe UN groupe. Déterministe : mêmes entrées, même sortie."""
    segments = con.execute(
        "SELECT s.text FROM segment s JOIN group_member m"
        " ON m.chunk_id = s.chunk_id WHERE m.group_id=? AND s.text <> ''",
        (group_id,)).fetchall()
    textes = [s["text"] for s in segments]
    n_mots = sum(len(t.split()) for t in textes)

    # 1. LA SOURCE — le seul signal sûr
    sources = con.execute(
        "SELECT DISTINCT c.source_id, src.kind, src.label FROM group_member m"
        " JOIN chunk c ON c.chunk_id = m.chunk_id"
        " LEFT JOIN source src ON src.source_id = c.source_id"
        " WHERE m.group_id=?", (group_id,)).fetchall()
    kinds = {s["kind"] for s in sources if s["kind"]}

    signaux: dict = {
        "mots": n_mots,
        "sources": sorted(kinds),
        "indices": _mots_cles(textes),
    }

    classe = "UNKNOWN"
    confiance = 0.0
    motif = "aucun signal"

    # Règle 1 : source média. On est sûrs que ce n'est PAS le propriétaire.
    if kinds & {"youtube", "film"}:
        classe, confiance, motif = "MEDIA", 0.95, "source média"
    # Règle 2 : beaucoup de mots business, et la voix du propriétaire est là
    elif signaux["indices"]["business"] >= 2:
        classe, confiance, motif = "BUSINESS", 0.6, \
            f"{signaux['indices']['business']} indices business"
    # Règle 3 : indices business OU personnel
    elif signaux["indices"]["personnel"] >= 1:
        classe, confiance, motif = "PERSONNEL", 0.5, \
            f"{signaux['indices']['personnel']} indices personnel"
    # Règle 4 : beaucoup de texte mais aucun mot du propriétaire -> probablement média
    elif n_mots > 120 and signaux["indices"]["narration"] > 0:
        classe, confiance, motif = "MEDIA", 0.4, "ton de narration"
    # Règle 5 : groupe presque vide -> on ne classe pas
    elif n_mots < 15:
        classe, confiance, motif = "UNKNOWN", 0.0, "trop peu de texte"

    signaux["motif"] = motif
    _ecrire(con, group_id, classe, signaux, confiance)
    return {"group_id": group_id, "classe": classe, "confiance": confiance,
            "motif": motif, "mots": n_mots, "sources": sorted(kinds)}


def _ecrire(con, group_id: int, classe: str, signaux: dict,
            confiance: float) -> None:
    con.execute(
        "INSERT OR REPLACE INTO classification(group_id, classe, signaux,"
        " confiance, llm, created_at) VALUES(?,?,?,?,?,?)",
        (group_id, classe, json.dumps(signaux, ensure_ascii=False),
         confiance, None, now_iso()))
    con.commit()


def classer_tout(con: sqlite3.Connection) -> dict:
    """Classe tous les groupes qui ont un transcript."""
    r = con.execute(
        "SELECT DISTINCT g.group_id FROM chunk_group g"
        " JOIN group_member m ON m.group_id = g.group_id"
        " JOIN segment s ON s.chunk_id = m.chunk_id"
        " WHERE s.text <> '' ORDER BY g.group_id").fetchall()
    compte: dict = {}
    for row in r:
        c = classer_groupe(con, row["group_id"])["classe"]
        compte[c] = compte.get(c, 0) + 1
    return compte
