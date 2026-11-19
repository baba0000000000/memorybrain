"""Écriture des fiches dans le wiki — la sortie finale du pipeline.

Règle 17 : le livrable est lisible par un humain. Une fiche sans sa
provenance ne vaut rien : on ne sait plus d'où elle sort, donc on ne peut
plus la vérifier ni la corriger. Chaque fiche porte donc TOUJOURS :
  - le fichier audio exact qui l'a produite ;
  - le timestamp du passage ;
  - le groupe qui l'a traitée.

On n'écrit JAMAIS par-dessus une fiche existante. Si une fiche existe
déjà pour cette idée, on ne la remplace pas : on propose. le propriétaire garde la
main (règle 20 : les corrections sont décidées par lui, pas par le
pipeline).
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

# Les catégories du graphe de mémoire. Le dossier de destination est
# choisi ici, en un seul endroit, pour être modifiable en une fois.
CATEGORIES = {
    "IDEA": "concepts/idees",
    "NOTE": "concepts/notes",
    "TODO": "actions/todos",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(texte: str, max_len: int = 60) -> str:
    """Un nom de fichier lisible, sans accents ni caractères bizarres."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFD", texte.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:max_len] or "sans-titre").rstrip("-")


def chemin_fiche(kind: str, slug: str, quand: str) -> Path:
    """Le chemin complet d'une future fiche."""
    dossier = config.WIKI / CATEGORIES.get(kind, "concepts/notes")
    prefixe = {"IDEA": "Idee", "TODO": "Todo", "NOTE": "Note"}[kind]
    return dossier / f"{prefixe}-{quand}_{slug}.md"


def ecrire_fiche(con: sqlite3.Connection, extract_id: int,
                 dry_run: bool = False) -> dict:
    """Écrit la fiche correspondant à une extraction.

    `dry_run=True` écrit le contenu dans le retour sans toucher au wiki :
    c'est le mode par défaut du pipeline, parce qu'écrire dans le wiki de
    le propriétaire avec des idées de LLM non relues serait le pire des scénarios.
    """
    e = con.execute(
        "SELECT * FROM extraction WHERE extract_id=?", (extract_id,)).fetchone()
    if e is None:
        return {"erreur": f"extraction {extract_id} inexistante"}

    g = con.execute("SELECT * FROM chunk_group WHERE group_id=?",
                    (e["group_id"],)).fetchone()

    # le fichier audio exact : le PREMIER chunk du groupe, et l'instant
    # du passage. Sans ça, impossible de vérifier la fiche.
    provenance = "inconnue"
    if g is not None:
        membres = con.execute(
            "SELECT c.original_file, c.start_time FROM group_member m"
            " JOIN chunk c ON c.chunk_id = m.chunk_id"
            " WHERE m.group_id=? ORDER BY m.rang LIMIT 1",
            (e["group_id"],)).fetchone()
        if membres:
            nom = Path(membres["original_file"]).name
            quand = datetime.fromtimestamp(membres["start_time"]).strftime(
                "%Y-%m-%d %H:%M")
            provenance = f"{nom} @ {quand}"

    slug = _slug(e["texte"])
    quand = datetime.now().strftime("%Y-%m-%d")
    chemin = chemin_fiche(e["kind"], slug, quand)

    corps = [
        "---",
        f"type: {e['kind']}",
        f"source: {provenance}",
        f"groupe: {e['group_id']}",
        f"extraction: {extract_id}",
        f"llm: {e['llm'] or 'inconnu'}",
        f"cree: {now_iso()}",
        "statut: a_revoir",
        "---",
        "",
        f"# {e['texte']}",
        "",
        "> Fiche générée par MemoryBrain V2, **non relue**.",
        "> Le champ `statut: a_revoir` signale qu'elle attend la validation",
        "> du propriétaire. Ne pas la traiter comme une vérité.",
        "",
        f"Audio d'origine : `{provenance}`",
        "",
    ]
    contenu = "\n".join(corps)

    if dry_run:
        return {"statut": "dry_run", "chemin": str(chemin),
                "contenu": contenu, "kind": e["kind"]}

    chemin.parent.mkdir(parents=True, exist_ok=True)
    if chemin.exists():
        return {"statut": "existe_déjà", "chemin": str(chemin)}
    chemin.write_text(contenu, encoding="utf-8")

    con.execute("UPDATE extraction SET chemin=? WHERE extract_id=?",
                (str(chemin), extract_id))
    con.commit()
    return {"statut": "écrit", "chemin": str(chemin), "kind": e["kind"]}


def ecrire_toutes(con: sqlite3.Connection, limit: int | None = None,
                  dry_run: bool = True) -> dict:
    """Écrit les fiches de toutes les extractions. Dry run par défaut."""
    q = ("SELECT extract_id FROM extraction WHERE chemin IS NULL"
         " ORDER BY extract_id")
    if limit:
        q += f" LIMIT {int(limit)}"
    ids = [r["extract_id"] for r in con.execute(q)]
    out = {"écrit": 0, "existe": 0, "dry_run": 0, "exemples": []}
    for i in ids:
        r = ecrire_fiche(con, i, dry_run=dry_run)
        if r.get("statut") == "dry_run":
            out["dry_run"] += 1
            if len(out["exemples"]) < 3:
                out["exemples"].append({"chemin": r["chemin"],
                                        "contenu": r["contenu"]})
        elif r.get("statut") == "écrit":
            out["écrit"] += 1
        else:
            out["existe"] += 1
    return out