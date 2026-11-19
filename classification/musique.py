"""Musique ou parole ? (la musique n'est pas transcrite)

Choix du modèle : AudioSet (AST, `MIT/ast-finetuned-audioset-10-10-0.4593`).
Il est multi-étiquettes et connaît « Music », « Speech », « Singing »,
« Musical instrument ». Quelques centaines de millisecondes par fenêtre de
10 s sur CPU, 87 M paramètres — pas de GPU requis.

Pourquoi pas une heuristique spectrale : les seuils changent avec le micro,
la pièce et le niveau d'enregistrement. Un seuil réglé sur un micro donné ne
tient pas sur un autre. Un modèle entraîné sur AudioSet ne bouge pas quand on
change de micro.

Cas typique : un extrait classé très « musique » est aussi celui où Whisper
produit « Sous-titrage ST' 501 » — la signature classique d'un modèle de
parole livré à de la musique. Le détecteur attrape donc la cause, pas un
symptôme.

Règle : la musique n'est JAMAIS prioritaire sur le propriétaire. S'il parle par-dessus,
c'est de la parole — sa voix est ce qu'on veut garder.
"""
from __future__ import annotations

import numpy as np

import config

_MODEL = None
_FE = None
_IDX: dict[str, int] = {}


def _charger():
    global _MODEL, _FE, _IDX
    if _MODEL is not None:
        return _MODEL, _FE, _IDX
    import torch
    from transformers import (AutoFeatureExtractor,
                             AutoModelForAudioClassification)
    _FE = AutoFeatureExtractor.from_pretrained(config.MODELE_MUSIQUE)
    _MODEL = AutoModelForAudioClassification.from_pretrained(
        config.MODELE_MUSIQUE).eval()
    etiquettes = [_MODEL.config.id2label[i]
                  for i in range(_MODEL.config.num_labels)]
    for cle in ("Music", "Speech", "Singing"):
        if cle in etiquettes:
            _IDX[cle] = etiquettes.index(cle)
    if "Music" not in _IDX:
        raise RuntimeError("le modèle musique n'expose pas la classe Music")
    return _MODEL, _FE, _IDX


def scores(x: np.ndarray, sr: int = 16000) -> dict:
    """p(Music), p(Speech), p(Singing) d'un extrait (10 s conseillées)."""
    import torch
    modele, fe, idx = _charger()
    a = np.asarray(x, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if a.size < sr:                       # < 1 s : rien de décidable
        return {"music": 0.0, "speech": 0.0, "singing": 0.0}
    ent = fe(a, sampling_rate=sr, return_tensors="pt")
    with torch.no_grad():
        logits = modele(**ent).logits[0]
    p = torch.sigmoid(logits).numpy()
    return {"music": float(p[idx["Music"]]),
            "speech": float(p[idx["Speech"]]),
            "singing": float(p[idx.get("Singing", idx["Music"])])}


def est_musique(x: np.ndarray, sr: int = 16000,
                proprietaire_present: bool = False) -> tuple[bool, dict]:
    """Musique ? Jamais si le propriétaire parle par-dessus (sa voix prime)."""
    p = scores(x, sr)
    if proprietaire_present:
        return False, p
    musical = (p["music"] >= config.SEUIL_MUSIQUE
               and p["music"] > p["speech"])
    return musical, p
