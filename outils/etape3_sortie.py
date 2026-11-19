"""ÉTAPE 3 — LA SORTIE : ce que tu dis devient des idées, ce que tu écoutes
devient une note.

Principe : on n'extrait que la voix du propriétaire ; tout le reste part en
note d'écoute.

Deux portes, et deux seulement :

  1. **ta voix** (`_DIAR.md`, lignes rouges) -> idées / TODO / notes, via le
     LLM local. Prompt fermé, JSON strict, aucune invention : le modèle n'a
     pas le droit d'écrire un fait qui n'est pas dans le texte ;
  2. **le contenu** (lignes noires) -> une note d'écoute par case : de quoi
     ça parlait, en quelques lignes, avec l'heure exacte.

Le LLM est local (ollama ; modèle défini par `--modele`, sans coût d'API).
S'il est absent, l'étape est SAUTÉE et notée comme telle : on n'invente jamais
une idée à la place du modèle (règle 3).

Usage :
    python3 outils/etape3_sortie.py
    python3 outils/etape3_sortie.py --modele qwen3:4b
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

OLLAMA = "http://localhost:11434"
ROUGE = "color:#c0392b"

PROMPT_IDEES = """Tu analyses des passages de la vie quotidienne du propriétaire.

On te donne des phrases qu'il a dites, avec leur heure. Produis UNIQUEMENT un
JSON, sans texte autour :

{"idees": ["..."], "todos": ["..."], "notes": ["..."]}

Règles :
- "idees" : ses pensées, décisions ou projets (une phrase max chacune)
- "todos" : ce qu'il doit faire, avec le verbe d'action
- "notes" : les faits à retenir (noms, dates, lieux, montants)
- Si une catégorie n'a rien, mets une liste vide. N'invente RIEN.
- N'écris aucun fait qui ne soit pas dans les phrases ci-dessous.
- Si les phrases ne contiennent aucune idée (une simple dictée technique,
  une histoire racontée), renvoie trois listes vides : c'est une réponse
  correcte.

PHRASES :
"""

PROMPT_ECOUTE = """Voici la transcription d'un contenu audio que le propriétaire a écouté
(émission, vidéo, podcast). Résume-le en 3 à 5 lignes maximum, puis donne 3 à 6
sujets/thèmes en mots-clés.

Réponds UNIQUEMENT en JSON :
{"resume": "...", "sujets": ["...", "..."]}

N'invente rien qui ne soit pas dans la transcription.

TRANSCRIPTION :
"""


def llm(prompt: str, modele: str, format_json: bool = True) -> dict | None:
    corps = {"model": modele, "prompt": prompt, "stream": False,
             "options": {"temperature": 0.0, "seed": 42, "top_k": 1}}
    if format_json:
        corps["format"] = "json"
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=json.dumps(corps).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            out = json.load(r).get("response", "")
        return json.loads(out) if format_json else {"texte": out}
    except Exception as e:
        return {"erreur": f"{type(e).__name__}: {str(e)[:120]}"}


def _lire_case(fichier: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(lignes du propriétaire, lignes de contenu) d'un transcript diarisé."""
    proprietaire, contenu = [], []
    if not fichier.is_file():
        return proprietaire, contenu
    for ligne in fichier.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(?:<span[^>]*>)?(\d{2}:\d{2}:\d{2}) — (.+?)(?:</span>)?$", ligne.strip())
        if not m:
            continue
        heure, texte = m.group(1), m.group(2)
        texte = re.sub(r"<[^>]+>", "", texte)
        if ROUGE in ligne:
            proprietaire.append((heure, texte))
        elif ligne.startswith("<span"):
            continue          # gris = bruit, écarté
        else:
            contenu.append((heure, texte))
    return proprietaire, contenu


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modele", default="qwen2.5:7b-instruct")
    a = ap.parse_args()

    fichiers = sorted(config.WORK_TRACE.glob("*_DIAR.md"))
    if not fichiers:
        print("aucun transcript diarisé : lancer d'abord outils/transcript_diarise.py")
        return 1

    jour = datetime.date.today().isoformat()
    idees_tot = {"idees": [], "todos": [], "notes": []}
    ecoutes = []
    appels_ok = appels_ko = 0

    for f in fichiers:
        nom = f.stem.replace("_DIAR", "")
        proprietaire, contenu = _lire_case(f)
        print(f"  {nom} : {len(proprietaire)} phrase(s) de toi, {len(contenu)} de contenu")

        if proprietaire:
            texte = "\n".join(f"[{h}] {t}" for h, t in proprietaire)
            r = llm(PROMPT_IDEES + texte, a.modele)
            if r and "erreur" not in r:
                appels_ok += 1
                for k in idees_tot:
                    idees_tot[k] += [{"case": nom, **x} if isinstance(x, dict) else
                                     {"case": nom, "texte": x}
                                     for x in (r.get(k) or [])]
            else:
                appels_ko += 1
                print(f"      LLM : {r.get('erreur') if r else 'réponse vide'}")

        if len(contenu) >= 5:
            texte = "\n".join(f"[{h}] {t}" for h, t in contenu)[:12000]
            r = llm(PROMPT_ECOUTE + texte, a.modele)
            if r and "erreur" not in r:
                appels_ok += 1
                ecoutes.append({"case": nom, "resume": r.get("resume", ""),
                                "sujets": r.get("sujets") or []})
            else:
                appels_ko += 1

    lignes = [f"# Ce que tu as dit — {jour}", "",
              f"Modèle : `{a.modele}` (local, ollama). Extraction sur les phrases de la voix principale uniquement.",
              "", "---", ""]
    titres = {"idees": "## Idées", "todos": "## Tâches (TODO)", "notes": "## Notes"}
    for k in ("idees", "todos", "notes"):
        lignes.append(titres[k])
        lignes.append("")
        if not idees_tot[k]:
            lignes.append("*(rien dans les passages du jour)*")
        for x in idees_tot[k]:
            lignes.append(f"- {x.get('texte', x)}  <sub>{x.get('case')}</sub>")
        lignes.append("")
    (config.WORK_TRACE / f"IDEES-{jour}.md").write_text(
        "\n".join(lignes) + "\n", encoding="utf-8")

    lignes2 = [f"# Ce que tu as écouté — {jour}", "",
               f"Modèle : `{a.modele}` (local). Résumé du contenu, pas de tes paroles.",
               "", "---", ""]
    for e in ecoutes:
        lignes2 += [f"## {e['case']}", "", e["resume"], ""]
        if e["sujets"]:
            lignes2 += ["**Sujets** : " + ", ".join(e["sujets"]), ""]
    if not ecoutes:
        lignes2.append("*(pas assez de contenu pour résumer)*")
    (config.WORK_TRACE / f"ECOUTE-{jour}.md").write_text(
        "\n".join(lignes2) + "\n", encoding="utf-8")

    print(f"\nappels LLM : {appels_ok} ok, {appels_ko} en échec")
    print(f"  idées {len(idees_tot['idees'])} · TODO {len(idees_tot['todos'])} · "
          f"notes {len(idees_tot['notes'])} · écoutes résumées {len(ecoutes)}")
    print(f"  -> work/traces/IDEES-{jour}.md")
    print(f"  -> work/traces/ECOUTE-{jour}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
