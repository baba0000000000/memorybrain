"""Reconnaître ce que Whisper INVENTE (et ne jamais le garder).

Un modèle de parole livré à du silence, à du bruit ou à de la musique ne
rend pas la main : il produit une phrase plausible. Les mêmes reviennent
d'un corpus à l'autre, ce qui les rend reconnaissables.

Cas typique (bloc sans musique ni voix, du silence autour) : « Sous-titrage
ST' 501 » — la signature d'un modèle entraîné sur des sous-titres.

Ces sorties n'ont aucune valeur : elles polluent les idées extraites et
faussent les compteurs de mots des contrôles. On les écarte à la source.
"""
from __future__ import annotations

import re

# Phrases signatures (insensibles à la casse). On les garde COURTES et
# spécifiques : une regex trop large effacerait une vraie phrase du propriétaire
# (« merci » seul n'est pas une hallucination, « merci d'avoir regardé »
# suivie d'un générique l'est).
SIGNATURES = (
    r"sous[- ]?titrage.{0,20}(st|sous[- ]?titr)",
    r"sous[- ]?titres? (réalisés|realises|par|de)",
    r"amara\.org",
    r"merci d'avoir (regardé|regarde)",
    r"thanks? (you )?for watching",
    r"(abonnez[- ]vous|subscribe to (my|the) channel)",
    r"like and subscribe",
    r"www\.[a-z0-9.-]+\.(com|fr|org)",
    r"^[\s.♪♫\-–—]*$",
)
_RE = re.compile("|".join(SIGNATURES), re.IGNORECASE)


def est_hallucination(texte: str) -> bool:
    """Vrai si le segment n'est pas de la parole réelle."""
    t = (texte or "").strip()
    if not t:
        return True
    return bool(_RE.search(t))
