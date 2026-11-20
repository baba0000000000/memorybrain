#!/usr/bin/env python3
"""CONTRÔLE DE CONFIDENTIALITÉ — refuser ce qui n'a rien à faire en public.

Gitleaks cherche des **secrets** (jetons, clés). Il ne voit pas la seconde
famille : les chemins d'un compte, les adresses IP privées, les noms de
machines, les identifiants d'appareil ou de locuteur, les dates de mesure et
les chiffres tirés d'un corpus personnel. Ce script cherche cette famille-là.

Trois usages :

    python3 outils/verifier_confidentialite.py              # tout le dépôt
    python3 outils/verifier_confidentialite.py --staged      # avant un commit
    python3 outils/verifier_confidentialite.py chemin/       # un dossier
    python3 outils/verifier_confidentialite.py --auto-test   # les motifs

Les noms de personnes et de machines ne sont **pas** écrits ici : un
garde-fou qui liste ce qu'il protège le publie. Ils vivent dans un fichier
local, jamais versionné : `<dossier git>/pre-commit-local.txt`, une ligne par
motif, au format « nom<TABULATION>expression régulière ». Absent, ce contrôle
est simplement inactif.

Ce que les motifs décrivent, ce sont des **formes** — la forme d'un identifiant
d'appareil, d'un hôte passé à une commande, d'une métrique de corpus — jamais
des valeurs. Un témoin manifestement faux (`AAAAAAA`, `speaker_0001`,
`*.invalid`, `localhost`) est donc reconnu comme tel et laissé passer : c'est
ce qui permet aux tests d'exercer les mêmes formes sans crier au loup.

Sortie : code 0 si rien, code 1 si une ligne est refusée, code 2 si le
contrôle n'a rien pu regarder (ce n'est pas une validation).
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

# ── Motifs propres à toute machine (« on publie le code, pas la topologie »)
#
# Cinq familles, dans l'ordre où elles ont mordu :
#   1. le compte et son disque   — chemins /home, ~/…, Path.home()/…
#   2. les périphériques         — identifiant Syncthing, profil vocal
#   3. la topologie              — hôte imposé à un worker, libellé hub
#   4. les mesures et volumes    — dates de relevé, métriques de corpus
#   5. le matériel               — modèle de carte ou de processeur
#
# Toute famille ajoutée ici doit l'être aussi dans outils/hooks/pre-commit :
# le hook s'appuie sur ce script en second regard, mais il doit rester utile
# quand le script est absent (archive, copie partielle). Un test le vérifie.
MOTIFS: list[tuple[str, str]] = [
    # — 1. compte et disque -------------------------------------------------
    ("chemin de compte",        r"/home/[a-z][a-z0-9_-]*/"),
    ("chemin de compte",        r"/Users/[a-z][a-z0-9_-]*/"),
    ("chemin Windows",          r"[A-Za-z]:[\\/]Users"),
    # Tout chemin sous ~/ sauf les dossiers XDG (~/.config, ~/.local/share…) :
    # ce sont ceux de toute application Linux, les refuser rendrait le
    # garde-fou inutilisable et pousserait à le désarmer.
    ("dossier personnel",       r"~/(?!\.)[\w.-]+/"),
    ("chemin personnel codé",   r"Path\.home\(\)\s*/\s*[\"'](?!\.)"),
    # Dossiers numérotés à la main : la nomenclature d'un disque personnel.
    ("dossier numéroté",        r"[\"'](?:0\d|1\d)-[A-Z][\w-]*[\"']"),

    # — 2. périphériques ----------------------------------------------------
    # Suffixe de conflit Syncthing : AAAAAMMJJ-HHMMSS puis le code de l'appareil.
    # Les témoins AAAAAAA / FAKE001 sont des formes de test, pas des appareils.
    ("identifiant d'appareil",  r"(?i)sync-conflict-\d{8}-\d{6}-(?!AAAAAAA|FAKE001)[A-Z0-9]{7}"),
    # Profil vocal numéroté : 0000 (le propriétaire) et 0001 (témoin de test)
    # sont les seuls numéros qui ne désignent personne.
    ("identifiant de locuteur", r"(?i)\bspeaker_(?!0{3}[01]\b)\d{4}\b"),

    # — 3. topologie --------------------------------------------------------
    # Nom d'hôte écrit en dur là où une variable d'environnement devrait
    # passer : option --worker, libellé hub:<hôte>, forme hôte@file.
    ("hôte imposé à un worker", r"--worker[\"']?\s*[,:]\s*[\"'](?!localhost[\"']|[\w.-]*\.invalid[\"'])[\w.@-]+[\"']"),
    ("hôte dans un libellé",    r"[\"']hub:(?!localhost[\"']|[\w.@-]*\.invalid[\"'])[a-z][\w.@-]*"),
    ("hôte@file",               r"[\"'][a-z][\w-]{1,20}@[a-z][\w-]{0,20}[\"']"),
    ("nom de machine local",    r"(?i)\b[a-z][a-z0-9-]*\.local\b(?![.-])"),
    ("IP privée",               r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    ("IP privée",               r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("IP privée",               r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),

    # — 4. mesures et volumes ----------------------------------------------
    ("date de mesure",          r"(?i)\b(mesur\w*|relev\w*|constat\w*)\s+(?:ici\s+)?(?:le|au)\s+\d{4}-\d{2}-\d{2}"),
    # Un relevé sans unité ni séparateur de milliers échappe à un motif naïf :
    # d'où la famille « dispersion », qui regarde une ligne de commentaire.
    ("dispersion de mesures",   r"(?i)^\s*[#*].*\b(min|max|moyenne|m[ée]diane)\b.*\d[.,]\d{2,}"),
    ("relevé réel",             r"(?i)^\s*[#*].*(mesures?\s+r[ée]elles?|relev[ée]s?\s+r[ée]els?|r[ée]sultats?\s+mesur[ée]s?)"),
    # Effectif d'un corpus, avec séparateur de milliers : « 1 234 transcripts ».
    ("métrique de corpus",      r"(?i)\b\d{1,3}(?:[ .]\d{3})+\s*(chunks?|fichiers?|groupes?|segments?"
                                r"|profils?|tours?|conflits?|transcripts?|timestamps?|paires?|copies?"
                                r"|mots|enregistrements?|dict[ée]es?)"),
    # Effectif sans séparateur mais à trois chiffres ou plus : « 123 conflits ».
    # En dessous de trois chiffres, c'est une fixture, pas un relevé.
    ("métrique de corpus",      r"(?i)\b\d{3,7}\s+(conflits?|transcripts?|timestamps?|paires?|copies?"
                                r"|enregistrements?|dict[ée]es?|chunks?|fichiers?|segments?|profils?|tours?)\b"),
    # Volume mesuré en heures d'audio ou en temps de calcul.
    ("volume de calcul",        r"(?i)\b\d+(?:[.,]\d+)?\s*h(?:eures?)?\s*(?:d['’]?\s*audio|de\s+calcul|de\s+CPU)"),
    ("volume de calcul",        r"(?i)\b\d+\s*min(?:utes)?\s+(?:de|par)\s+(?:calcul|heure\s+d['’]?\s*audio)"),

    # Seuil d'assertion qui encode la taille d'un corpus : un test qui exige
    # « plus de 5 000 noms » échoue chez tout contributeur et publie l'effectif.
    ("seuil de corpus",         r"(?i)assertGreater(?:Equal)?\(\s*len\([^)]*\),\s*\d{3,}"),

    # — 5. divers ------------------------------------------------------------
    # Un point d'entrée de service local écrit en dur (« localhost:11434 »)
    # fige une topologie dans le code publié : l'adresse utile doit venir
    # d'une variable d'environnement. Le nom du service, lui, n'est pas
    # personnel — c'est le fait de le câbler qui l'est.
    ("service local en dur",    r"(?i)\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d{2,5}\b"),
    ("modèle de matériel",      r"(?i)\b(?:rtx|gtx|arc\s?a|radeon|ryzen|gfx)\s?-?\d{3,4}\b"),
    ("adresse e-mail",          r"[\w.+-]+@[\w-]+\.[a-z]{2,}"),
]

# Fichiers exclus : le garde-fou se déclencherait sur ses propres motifs.
EXCLUS = {
    "outils/verifier_confidentialite.py",
    "outils/hooks/pre-commit",
}
EXTENSIONS_TEXTE = {
    ".py", ".md", ".sh", ".txt", ".yml", ".yaml", ".toml", ".json",
    ".java", ".xml", ".gradle", ".properties", ".cfg", ".ini", ".html", ".css",
}
ECHAPPATOIRE = "no-fuite"

# ── Auto-test : chaque motif doit avoir mordu au moins un leurre ───────────
#
# Un garde-fou qui n'a jamais refusé n'est pas un garde-fou. Ces leurres sont
# fabriqués pour l'exercice : ils n'ont ni valeur ni réalité, et ils ne
# contiennent aucun nom de personne ou de machine.
TEMOINS: list[tuple[str, str]] = [
    ("chemin de compte",        "ouvrir /home/quelquun/donnees/x.md"),
    ("chemin de compte",        "ouvrir /Users/quelquun/Desktop/x.md"),
    ("chemin Windows",          "ouvrir C:\\Users\\quelquun\\x.md"),
    ("dossier personnel",       "lire ~/mes-documents/x.md"),
    ("chemin personnel codé",   "d = Path.home() / \"mon-projet\""),
    ("dossier numéroté",        "d = base / \"07-Projet\""),
    ("identifiant d'appareil",  "a.sync-conflict-20240101-000000-ZZZ9999.wav"),
    ("identifiant de locuteur", "VALUES('speaker_0042','UNKNOWN','n')"),
    ("hôte imposé à un worker", "cmd = [\"x\", \"--worker\", \"hote-interne\", cible]"),
    ("hôte dans un libellé",    "r = {\"backend\": \"hub:hote-interne\"}"),
    ("hôte@file",               "\"worker\": \"user@queue\""),
    ("nom de machine local",    "ssh poste-de-travail.local"),
    ("IP privée",               "http://192.168.1.20:8080"),
    ("IP privée",               "http://10.0.0.5/x"),
    ("IP privée",               "http://172.20.3.4/x"),
    ("date de mesure",          "mesuré le 2023-05-04 sur le corpus"),
    ("dispersion de mesures",   "# scores : min 0.71, moyenne 0.84"),
    ("relevé réel",             "# relevé réel sur les enregistrements"),
    ("métrique de corpus",      "il reste 1 234 transcripts sans horaires"),
    ("métrique de corpus",      "123 conflits Syncthing ont été résolus"),
    ("volume de calcul",        "42 h d'audio oubliées par la V1"),
    ("volume de calcul",        "2 h de calcul par heure d'audio"),
    ("modèle de matériel",      "RTX 4070 Ti"),
    ("adresse e-mail",          "écrire à prenom.nom@exemple.fr"),
    ("seuil de corpus",         "self.assertGreater(len(noms), 4200, \"le corpus a rétréci\")"),
    ("service local en dur",    "url = \"http://localhost:11434/api/chat\""),
]

# Lignes qui doivent PASSER : faux positifs interdits, sous peine de voir le
# garde-fou désarmé à la première alerte inutile.
PROPRES: list[str] = [
    "wiki = ~/.local/share/memorybrain/wiki",
    "config = ~/.config/memorybrain/config.toml",
    "cmd = [\"x\", \"--worker\", \"localhost\", cible]",
    "cmd = [\"x\", \"--worker\", \"worker.exemple.invalid\", cible]",
    "a.sync-conflict-20240101-000000-AAAAAAA.wav",
    "VALUES('speaker_0001','UNKNOWN','n')",
    "self.assertEqual(len(noms), 5)",
    "self.assertGreater(len(noms), 1)",
    "arrêt après 30 s de silence, tranches de 60 s",
    "3 fichiers suffisent pour ce test",
    "d = Path.home() / \".local\" / \"share\" / \"memorybrain\"",
]


def noms_du_hook() -> set[str] | None:
    """Noms des motifs de données personnelles listés par le hook.

    Le hook duplique volontairement ces motifs : il doit rester utile quand ce
    script est absent (archive, copie partielle). Deux listes qui divergent,
    c'est un trou qui revient par la porte du hook. On lit donc la sienne.
    """
    f = Path(__file__).resolve().parent / "hooks" / "pre-commit"
    if not f.exists():
        return None
    arbre = ast.parse(f.read_text(encoding="utf-8"))
    for n in arbre.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "MOTIFS_PERSO" for t in n.targets):
            return {e.elts[0].value for e in n.value.elts
                    if isinstance(e, ast.Tuple) and isinstance(e.elts[0], ast.Constant)}
    return None


def auto_test() -> int:
    """Vérifie que chaque motif mord son leurre et épargne les lignes propres."""
    noms = {nom for nom, _ in MOTIFS}
    echecs = []
    hook = noms_du_hook()
    if hook is not None and hook != noms:
        echecs.append("  le hook et ce script ne protègent pas les mêmes familles : "
                      f"manquant côté hook {sorted(noms - hook)}, "
                      f"en trop côté hook {sorted(hook - noms)}")
    for nom, leurre in TEMOINS:
        if nom not in noms:
            echecs.append(f"  motif absent de la liste : {nom}")
        elif not any(re.search(m, leurre) for n, m in MOTIFS if n == nom):
            echecs.append(f"  [{nom}] ne reconnaît pas son leurre : {leurre}")
    for propre in PROPRES:
        touche = [nom for nom, m in MOTIFS if re.search(m, propre)]
        if touche:
            echecs.append(f"  faux positif {touche} sur une ligne propre : {propre}")
    if echecs:
        print("❌ AUTO-TEST DU GARDE-FOU EN ÉCHEC :\n", file=sys.stderr)
        print("\n".join(echecs), file=sys.stderr)
        return 1
    print(f"✅ auto-test : {len(TEMOINS)} leurre(s) refusé(s), "
          f"{len(PROPRES)} ligne(s) propre(s) épargnée(s), {len(MOTIFS)} motif(s).")
    return 0


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout


def motifs_locaux() -> list[tuple[str, str]]:
    d = git("rev-parse", "--git-dir").strip()
    if not d:
        return []
    f = Path(d) / "pre-commit-local.txt"
    if not f.exists():
        return []
    out = []
    for ligne in f.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        nom, _, motif = ligne.partition("\t")
        if motif.strip():
            out.append((nom.strip() or "motif local", motif.strip()))
    return out


def fichiers(mode: str, cibles: list[str]) -> list[Path]:
    if mode == "staged":
        noms = git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines()
        return [Path(n) for n in noms if n.strip()]
    if cibles:
        res = []
        for c in cibles:
            p = Path(c)
            res.extend([p] if p.is_file() else sorted(p.rglob("*")))
        return [p for p in res if p.is_file()]
    # Sans dépôt git (archive téléchargée, copie de travail exportée), git
    # ls-files ne rend RIEN : un garde-fou qui ne voit aucun fichier et annonce
    # « aucun motif trouvé » serait un faux vert. On parcourt l'arborescence.
    if not git("rev-parse", "--git-dir").strip():
        return [p for p in sorted(Path(".").rglob("*")) if p.is_file()]
    return [Path(n) for n in git("ls-files").splitlines() if n.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cibles", nargs="*", help="fichiers ou dossiers (défaut : tout le dépôt suivi)")
    ap.add_argument("--staged", action="store_true", help="contrôler seulement ce qui est mis en scène")
    ap.add_argument("--auto-test", action="store_true",
                    help="vérifier que chaque motif refuse bien un leurre")
    a = ap.parse_args()

    if a.auto_test:
        return auto_test()

    motifs = MOTIFS + motifs_locaux()
    problemes: list[str] = []
    vus = 0
    liste = fichiers("staged" if a.staged else "dir", a.cibles)

    # En mode « tout le dépôt », une liste vide n'est pas un succès : c'est
    # l'outil qui n'a rien vu (mauvais dossier, archive sans dépôt git).
    if not liste and not a.staged:
        print(
            "⚠ AUCUN FICHIER CONTRÔLÉ — ce n'est pas une validation.\n"
            "   Vérifier le dossier courant.",
            file=sys.stderr,
        )
        return 2

    for p in liste:
        rel = str(p).replace("\\", "/").lstrip("./")
        # L'exclusion doit tenir quel que soit le préfixe passé en argument
        # (« outils/… » ou « memorybrain-main/outils/… »).
        exclu = any(rel == e or rel.endswith("/" + e) for e in EXCLUS)
        if exclu or p.suffix.lower() not in EXTENSIONS_TEXTE:
            continue
        try:
            texte = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        vus += 1
        for i, ligne in enumerate(texte.splitlines(), 1):
            if ECHAPPATOIRE in ligne:
                continue
            for nom, motif in motifs:
                if re.search(motif, ligne):
                    apercu = ligne.strip()
                    problemes.append(f"  {rel}:{i}  [{nom}]\n    {apercu[:100]}")
                    break

    if problemes:
        print("❌ DONNÉE PERSONNELLE DÉTECTÉE — publication refusée :\n", file=sys.stderr)
        print("\n".join(problemes), file=sys.stderr)
        print(
            f"\n{len(problemes)} ligne(s) à traiter. Trois issues :\n"
            "  1. retirer la valeur (variable d'environnement, exemple générique) ;\n"
            "  2. si c'est un faux positif assumé, ajouter « no-fuite » en commentaire ;\n"
            "  3. si c'est un secret : le RÉVOQUER avant tout le reste.",
            file=sys.stderr,
        )
        return 1

    if vus == 0:
        print(f"✅ rien à contrôler ({len(liste)} fichier(s) listé(s), aucun texte publiable).")
        return 0

    # Un vert dit ce qui a été cherché, jamais ce qui existe : « aucune donnée
    # personnelle détectée » était un mensonge par construction.
    print(f"✅ {vus} fichier(s) contrôlé(s) — {len(motifs)} motif(s) appliqué(s), "
          "aucun n'a été trouvé. Liste non exhaustive : ce vert ne prouve pas "
          "l'absence de fuite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
