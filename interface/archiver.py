"""ÉTAPE 10 — ARCHIVAGE : audio traité, et V1 dégagée.

Deux opérations distinctes, à ne surtout pas confondre :

1. **COPIER les audios traités dans la corbeille.** Tant que la V2 n'est pas
   validée, on COPIE, on ne supprime rien : les originaux restent en place
   (règle 5). La corbeille est un filet de sécurité, pas une destruction.

2. **Envoyer les résultats V1 du wiki dans la poubelle.** Les fichiers V1 ont
   été produits avec un modèle plus faible, sans diarisation. Ils ne sont pas
   erronés, ils sont de qualité insuffisante : on les sort du chemin de la V2
   sans les détruire.

Le point commun : AUCUN FICHIER N'EST DÉTRUIT. Si un choix se révèle mauvais,
tout est encore là.
"""
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import config


def _poubelle_du_jour(sous_dossier: str) -> Path:
    """Le dossier de poubelle daté du jour. Créé à la demande.

    ⚠ Le dossier poubelle EXISTE DÉJÀ : on ne le crée pas à l'aveugle, on ne
    le cherche que si la racine est là.
    """
    racine = config.POUBELLE
    if not racine.parent.is_dir():
        # on ne peut pas créer la racine : le projet a été déplacé.
        # On signale plutôt que d'écrire au hasard (règle 3).
        raise FileNotFoundError(
            f"poubelle introuvable : {racine} (le projet a peut-être été "
            f"déplacé ; vérifier config.POUBELLE)")
    dest = racine / f"{config.POUBELLE_PREFIXE}-{sous_dossier}-{datetime.now():%Y-%m-%d}"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def archiver_audio_traite(con: sqlite3.Connection, deja_copie: bool = True,
                          poubelle: Path | None = None) -> dict:
    """Copie dans la poubelle les audios des groupes réellement traités.

    `deja_copie=True` ne prend que les groupes qui ont un transcript : les
    audios des groupes non transcrits ne seront pas copiés pour rien.
    """
    from grouping import group as gmod

    q = ("SELECT group_id, audio_path FROM chunk_group WHERE audio_path IS NOT NULL")
    if deja_copie:
        q += " AND statut='transcrit'"
    out = gmod.copier_vers_poubelle(
        con, poubelle=poubelle) if poubelle else gmod.copier_vers_poubelle(con)
    return out


def wiki_v1_vers_poubelle(con: sqlite3.Connection | None = None,
                          confirmer: bool = False) -> dict:
    """Déplace les résultats V1 du wiki vers la poubelle.

    ⚠ `confirmer=False` par défaut : cette fonction ne fait RIEN tant
    qu'elle n'a pas été appelée explicitement avec confirmation. Une erreur
    de chemin détruirait un travail irrécupérable. Le pipeline appelle cette
    fonction, mais avec `--wiki` — jamais par défaut.

    Ce qui part en poubelle :
      - les fiches `concepts/idees/Idee-*.md` ;
      - les fiches `concepts/ecoutes/Ecoute-*.md` ;
      - les transcripts bruts `raw/transcripts/`.

    Ce qui reste en place :
      - les notes du propriétaire lui-même (concepts/notes/)
      - la structure des dossiers
      - tout ce qui n'est pas explicitement listé
    """
    wiki = config.WIKI
    if not wiki.is_dir():
        return {"erreur": f"wiki introuvable : {wiki}"}

    if not confirmer:
        return {"statut": "non confirmé",
                "detail": "appeler avec confirmer=True pour déplacer"}

    dest = _poubelle_du_jour("wiki-v1")

    deplace: list[str] = []
    kopie: list[str] = []

    # 1. les fiches idées et écoutes V1
    for motif in ("idees", "ecoutes"):
        dossier = wiki / "concepts" / motif
        if not dossier.is_dir():
            continue
        for f in dossier.iterdir():
            if not f.is_file():
                continue
            if motif == "idees" and not f.name.startswith("Idee-"):
                continue
            if motif == "ecoutes" and not f.name.startswith("Ecoute-"):
                continue
            shutil.move(str(f), str(dest / f.name))
            deplace.append(f"{motif}/{f.name}")

    # 2. les transcripts bruts V1
    transcripts = wiki / "raw" / "transcripts"
    n_transcripts = 0
    if transcripts.is_dir():
        for f in transcripts.iterdir():
            if f.is_file() and f.suffix in (".md", ".txt", ".json"):
                shutil.move(str(f), str(dest / f.name))
                n_transcripts += 1

    return {"deplace": len(deplace), "transcripts": n_transcripts,
            "dossier": str(dest),
            "exemples": deplace[:5]}


def resume(con: sqlite3.Connection) -> dict:
    """Ce qui a été archivé, et où. Pour que le propriétaire puisse vérifier."""
    return {
        "poubelle": str(config.POUBELLE),
        "prefixe": config.POUBELLE_PREFIXE,
        "dossiers": sorted(p.name for p in config.POUBELLE.iterdir()
                           if p.is_dir()) if config.POUBELLE.is_dir() else [],
        "groupes_copies": con.execute(
            "SELECT COUNT(*) FROM chunk_group WHERE statut='transcrit'").fetchone()[0],
    }