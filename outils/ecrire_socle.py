"""L'ÉCRIVAIN — dépose dans le socle ce qui est fini, et rien d'autre.

Contrat : le socle est l'unique interface de consultation.
L'atelier travaille, puis DÉPOSE. Quatre destinations :

    la voix du propriétaire -> idée
        <dictees>/idees-AAAA/Idee-AAAA-MM-JJ-slug.md
    la voix du propriétaire -> note
        <dictees>/notes-AAAA/Note-AAAA-MM-JJ-slug.md
    le contenu écouté          <wiki>/raw/ecoutes/AAAA-MM-JJ-ecoutes.md
    le transcript brut         <wiki>/raw/transcripts/AAAA/AAAA-MM-JJ_HHhMM.txt

`<dictees>` et `<wiki>` viennent de `config` (variables d'environnement) : ce
script ne connaît aucun chemin absolu.

Règles tenues ici :
  - **on n'écrase jamais** : si la fiche existe, on ajoute `-2` et on le dit ;
  - **la provenance est obligatoire** : audio, heure, score d'empreinte ;
  - une case n'est marquée `ecrite` qu'une fois TOUT écrit pour elle ;
  - un « fin de l'idée » sans « début de l'idée » fait remonter 5 minutes en
    arrière (règle : on regarde 5 minutes avant).

Usage :
    python3 outils/ecrire_socle.py --jour AAAA-MM-JJ --dry-run
    python3 outils/ecrire_socle.py --jour AAAA-MM-JJ
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import blocs
import importlib
_e3 = importlib.import_module("outils.etape3_sortie")

ROUGE = "color:#c0392b"
MODÈLE = "qwen2.5:7b-instruct"


def _slug(texte: str, maxi: int = 60) -> str:
    t = unicodedata.normalize("NFD", texte.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:maxi] or "sans-titre").rstrip("-")


def _sans_ecraser(chemin: Path, contenu: str) -> Path:
    """Écrit sans jamais écraser : `-2`, `-3`… si la place est prise."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    if not chemin.exists():
        chemin.write_text(contenu, encoding="utf-8")
        return chemin
    i = 2
    while True:
        autre = chemin.with_name(f"{chemin.stem}-{i}{chemin.suffix}")
        if not autre.exists():
            autre.write_text(contenu, encoding="utf-8")
            return autre
        i += 1


def _proprietaire_et_contenu(fichier: Path):
    return _e3._lire_case(fichier)


def _score_proprietaire(fichier: Path) -> float:
    """Le score d'empreinte du locuteur identifié comme le propriétaire (provenance)."""
    meilleur = 0.0
    if not fichier.is_file():
        return meilleur
    for ligne in fichier.read_text(encoding="utf-8").splitlines():
        if not ligne.startswith("|") or "le propriétaire" not in ligne:
            continue
        # colonnes : | locuteur | tours | durée | score | verdict |
        cells = [c.strip() for c in ligne.strip("|").split("|")]
        if len(cells) >= 5 and cells[-1].startswith("le propriétaire"):
            try:
                meilleur = max(meilleur, float(cells[3]))
            except ValueError:
                pass
    return round(meilleur, 3)


def _fiche(kind: str, texte: str, case: str, heure: str, score, jour: str) -> str:
    titre = texte[0].upper() + texte[1:] if texte else "Sans titre"
    theme = "perso"
    tags = "idees, perso" if kind == "idee" else "notes, perso"
    corps = [
        "---",
        f"title: {titre[:80]}",
        f"created: {jour}",
        f"updated: {jour}",
        f"type: {kind}",
        "status: nouvelle",
        f"theme: {theme}",
        f"tags: [{tags}]",
        f"source: audio {case} à {heure} (téléphone, MemoryBrain V2)",
        f"voix: confirmée par empreinte (score {score})",
        "---",
        "",
        f"# {titre}",
        "",
        "**Statut** : 💡 nouvelle" if kind == "idee" else "**Fait à retenir**",
        "",
        texte,
        "",
        "**Propos original** (brut) :",
        "",
        f"> {texte}",
        "",
    ]
    return "\n".join(corps)


def ecrire(con: sqlite3.Connection, jour: str | None = None,
           dry_run: bool = False) -> dict:
    """Écrit dans le socle toutes les cases `diarisee` de la plage demandée."""
    q = "SELECT start_time, statut FROM bloc WHERE statut='diarisee' ORDER BY start_time"
    rows = [dict(r) for r in con.execute(q)]
    if jour:
        rows = [r for r in rows if datetime.datetime.fromtimestamp(
            r["start_time"]).strftime("%Y-%m-%d") == jour]

    out = {"cases": [], "idees": 0, "notes": 0, "todos": 0,
           "ecoutes": 0, "transcripts": 0, "ecrits": []}
    precedente = ""      # texte du propriétaire de la case d'avant (règle des 5 min)
    collecte = {"idees": [], "notes": [], "todos": [], "ecoutes": []}

    for r in rows:
        t = r["start_time"]
        dt = datetime.datetime.fromtimestamp(t)
        nom = blocs.nom_case(t).replace(".wav", "")
        fichier = config.WORK_TRACE / f"{nom}_DIAR.md"
        if not fichier.is_file():
            out["cases"].append({"case": nom, "statut": "diarisation absente"})
            continue
        proprietaire, contenu = _proprietaire_et_contenu(fichier)
        score = _score_proprietaire(fichier)

        # --- le transcript brut, pour vérifier plus tard -------------------
        p = config.dossier_transcripts(dt.year) / f"{nom}.txt"
        if not dry_run:
            _sans_ecraser(p, "\n".join(f"[{h}] {x}" for h, x in proprietaire + contenu))
        out["transcripts"] += 1

        # --- ta voix -> idées et notes -------------------------------------
        if proprietaire:
            texte = "\n".join(f"[{h}] {x}" for h, x in proprietaire)
            # « fin de l'idée » sans « début de l'idée » : on remonte 5 minutes
            if re.search(r"fin d[eu] l?'?id[ée]e", texte, re.I) and \
               not re.search(r"d[ée]but d[eu] l?'?id[ée]e", texte, re.I):
                texte = precedente + "\n" + texte
            res = _e3.llm(_e3.PROMPT_IDEES + texte, MODÈLE)
            if res and "erreur" not in res:
                for kind, cle in (("idee", "idees"), ("note", "notes")):
                    for item in (res.get(cle) or []):
                        txt = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                        collecte[cle].append({"texte": txt, "case": nom,
                                              "heure": proprietaire[0][0], "dt": dt,
                                              "score": score})
                        out["idees" if kind == "idee" else "notes"] += 1
                for item in (res.get("todos") or []):
                    txt = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                    collecte["todos"].append({"texte": txt, "case": nom,
                                              "heure": proprietaire[0][0]})
                    out["todos"] += 1
            else:
                out["cases"].append({"case": nom, "statut": "LLM indisponible"})
            precedente = "\n".join(f"[{h}] {x}" for h, x in proprietaire)[-1500:]

        # --- le contenu -> une note d'écoute --------------------------------
        if len(contenu) >= 5:
            texte = "\n".join(f"[{h}] {x}" for h, x in contenu)[:12000]
            res = _e3.llm(_e3.PROMPT_ECOUTE + texte, MODÈLE)
            if res and "erreur" not in res:
                collecte["ecoutes"].append({"heure": f"{dt:%H:%M}",
                                            "resume": res.get("resume", ""),
                                            "sujets": res.get("sujets") or []})
                out["ecoutes"] += 1

        if not dry_run:
            con.execute("UPDATE bloc SET statut='ecrite' WHERE start_time=?", (t,))
            con.commit()
        out["cases"].append({"case": nom, "statut": "ecrite" if not dry_run else "simulée"})

    # ---------------- écriture, une fois tout collecté --------------------
    for kind, cle, dossier_fn, prefixe in (
            ("idee", "idees", config.dossier_idees, "Idee"),
            ("note", "notes", config.dossier_notes, "Note")):
        for e in collecte[cle]:
            dt = e["dt"]
            chemin = dossier_fn(dt.year) / f"{prefixe}-{dt:%Y-%m-%d}-{_slug(e['texte'])}.md"
            if dry_run:
                out["ecrits"].append(str(chemin) + " (simulé)")
            else:
                out["ecrits"].append(str(_sans_ecraser(
                    chemin, _fiche(kind, e["texte"], e["case"], e["heure"],
                                   e.get("score", 0.0), f"{dt:%Y-%m-%d}"))))

    # le fichier d'écoute du jour est une PROJECTION (il se reconstruit),
    # donc on le réécrit en entier : pas de doublons si la passe est relancée.
    if collecte["ecoutes"]:
        jours = sorted({e["heure"] for e in collecte["ecoutes"]})
        d = datetime.datetime.fromtimestamp(
            [b["start_time"] for b in rows][0]) if rows else datetime.datetime.now()
        p = config.ECOUTES / f"{d:%Y-%m-%d}-ecoutes.md"
        corps = [f"# Écoutes du jour — {d:%Y-%m-%d}", "",
                 "> Source : capture audio → MemoryBrain. Texte non rédigé.",
                 "", "---", ""]
        for e in collecte["ecoutes"]:
            corps += [f"## {e['heure']}", "", e["resume"], ""]
            if e["sujets"]:
                corps += ["**Sujets** : " + ", ".join(e["sujets"]), ""]
        if dry_run:
            out["ecrits"].append(str(p) + " (simulé, reconstruit)")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(corps) + "\n", encoding="utf-8")
            out["ecrits"].append(str(p))

    # les TODO du jour, regroupés dans un seul fichier
    if collecte["todos"]:
        d = datetime.datetime.fromtimestamp([b["start_time"] for b in rows][0]) if rows else datetime.datetime.now()
        p = config.DICTEES / f"TODO-{d:%Y-%m-%d}.md"
        corps = [f"# À faire — {d:%Y-%m-%d}", "",
                 "> Extrait de ta voix par le LLM local. Cocher au fur et à mesure.", ""]
        for e in collecte["todos"]:
            corps.append(f"- [ ] {e['texte']}  <sub>{e['heure']} · {e['case']}</sub>")
        corps.append("")
        if dry_run:
            out["ecrits"].append(str(p) + " (simulé)")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(corps), encoding="utf-8")
            out["ecrits"].append(str(p))

    return out


def resume(r: dict) -> str:
    l = [f"cases traitées : {len(r['cases'])}",
         f"idées {r['idees']} · notes {r['notes']} · TODO {r['todos']} · "
         f"écoutes {r['ecoutes']} · transcripts {r['transcripts']}"]
    for c in r["cases"]:
        l.append(f"  {c['case']} : {c['statut']}")
    for e in r["ecrits"]:
        l.append(f"  -> {e}")
    return "\n".join(l)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jour")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    print(resume(ecrire(con, a.jour, a.dry_run)))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
