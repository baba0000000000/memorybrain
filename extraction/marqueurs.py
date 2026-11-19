"""ÉTAGE 1 — MARQUEURS ORAUX : le locuteur dit lui-même ce qui compte.

Pourquoi ce module existe
-------------------------

La classification par mots-clés devine. Et elle devine mal : un groupe sans
aucun mot-clé connu tombe en `UNKNOWN` et **rien n'en est extrait** — même
quand le locuteur y dicte une vraie tâche.

D'où l'idée : un marqueur oral — « je commence mon idée » / « fin de l'idée »,
ou « rajoute ça à ma to-do » — est un signal **explicite**. C'est infiniment
plus fiable qu'un mot-clé deviné.

Règle tenue ici : **le marqueur PRIME sur la classification.** Un marqueur
trouvé fait extraire le passage même si le groupe est `UNKNOWN`. Sans ça, la
mémoire resterait vide sur tout ce qui ne ressemble pas à une catégorie prévue.
"""
import re

# Formes explicites uniquement : « je dois » seul est trop fréquent pour
# ouvrir un bloc, et le mot « idée » isolé ne suffit pas.
DEBUT_IDEE = [
    "début de l'idée", "debut de l'idee",
    "début d'idée", "debut d'idee",
    "je commence mon idée", "je commence l'idée",
    "je démarre mon idée", "je démarre l'idée",
    "début idée", "debut idee",
]
FIN_IDEE = [
    "fin de l'idée", "fin de l'idee",
    "fin d'idée", "fin d'idee",
    "je finis mon idée", "je finis l'idée",
    "fin de mon idée",
    "fin idée", "fin idee",
]
TODO = [
    "dans la to-do", "dans la todo", "à la to-do", "à la todo",
    "rajoute comme tâche", "rajoutes comme tâche",
    "rajoute ça comme tâche", "rajoutes ça comme tâche",
    "ajoute comme tâche", "ajoutes comme tâche",
    "mets ça comme tâche", "mets ça comme une tâche",
    "mets ça dans la to-do", "mets ça dans la todo",
    "mets-le dans la to-do", "mets le dans la to-do",
    "rajoute dans la to-do", "ajoute dans la to-do",
    "rajoute dans la todo", "ajoute dans la todo",
    "ajoute une tâche", "rajoute une tâche",
]
# ⚠ « nouvelle tâche » a été RETIRÉ : faux positif quand le locuteur décrit
# le système au lieu de parler à l'assistant.

# Au-delà de cette longueur, un « bloc » n'est plus une idée : c'est le
# micro resté ouvert. On coupe, plutôt que d'envoyer 5 minutes au LLM.
MAX_BLOC = 1200


def _motif(phrases: list[str]) -> re.Pattern:
    """Regex tolérant : espaces multiples et apostrophes droite/courbe.

    Les dictées donnent « début de l'idée », « début de l’idée »,
    « début  de l'idée » : les trois doivent matcher.
    """
    parts = []
    for p in phrases:
        p = re.escape(p)
        p = p.replace("\\ ", r"\s+")
        p = p.replace("'", "['’]?")
        parts.append(p)
    return re.compile("(" + "|".join(parts) + ")", re.IGNORECASE)


_RE_DEBUT = _motif(DEBUT_IDEE)
_RE_FIN = _motif(FIN_IDEE)
_RE_TODO = _motif(TODO)


def _normalise(texte: str) -> str:
    return texte.replace("’", "'")


def blocs_idees(texte: str) -> list[dict]:
    """Passages entre « début de l'idée » et « fin de l'idée ».

    Renvoie [{"debut": i, "fin": j, "texte": "..."}]. Un « début » sans
    (marqueur de fermeture oublié)
    préfère un bloc trop long qu'un bloc perdu.
    """
    t = _normalise(texte)
    out = []
    pos = 0
    while True:
        d = _RE_DEBUT.search(t, pos)
        if not d:
            break
        f = _RE_FIN.search(t, d.end())
        fin = f.start() if f else len(t)
        contenu = t[d.end():fin].strip()
        if contenu:
            out.append({"debut": d.end(), "fin": fin,
                        "texte": contenu[:MAX_BLOC]})
        pos = f.end() if f else len(t)
    return out


def blocs_todo(texte: str) -> list[dict]:
    """Passages ouverts par un marqueur de tâche.

    Un marqueur de tâche n'a pas (encore) de marqueur de fin : le propriétaire ne
    dit pas « fin de la tâche ». On prend donc du marqueur jusqu'à la fin
    du paragraphe/du tour, ou jusqu'au prochain marqueur, plafonné.
    """
    t = _normalise(texte)
    out = []
    for m in _RE_TODO.finditer(t):
        suite = t[m.end():]
        # on s'arrête au prochain marqueur quel qu'il soit
        bornes = [len(suite)]
        for rx in (_RE_DEBUT, _RE_FIN, _RE_TODO):
            n = rx.search(suite)
            if n:
                bornes.append(n.start())
        # un double saut de ligne = fin du tour dans le markdown
        par = suite.find("\n\n")
        if par > 0:
            bornes.append(par)
        fin = min(bornes)
        contenu = suite[:fin].strip()
        if contenu:
            out.append({"debut": m.end(), "fin": m.end() + fin,
                        "texte": contenu[:MAX_BLOC]})
    return out


def extraire(texte: str) -> dict:
    """{"idees": [...], "todos": [...]} — les passages marqués par le propriétaire."""
    return {"idees": blocs_idees(texte), "todos": blocs_todo(texte)}


def a_des_marqueurs(texte: str) -> bool:
    r = extraire(texte)
    return bool(r["idees"] or r["todos"])
