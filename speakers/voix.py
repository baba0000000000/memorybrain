"""Empreinte vocale via le code de PYANNOTE (pas le paquet wespeaker).

Pourquoi ne pas utiliser le paquet `wespeaker` de PyPI/GitHub :
  - il tire `s3prl` → exige torchaudio.set_audio_backend, supprimé
    en torchaudio 2.9 et au-delà ;
  - downgrader torchaudio casserait pyannote ET Whisper, qui marchent ;
  - et le modele qu'on veut n'utilise pas sox (sox sert aux
    augmentations d'entrainement).

Pyannote embarque la MEME architecture et le meme poids sur HF
(`pyannote/wespeaker-voxceleb-resnet34-LM`) :
    pyannote/audio/models/embedding/wespeaker/resnet.py
    classe WeSpeakerResNet34

On utilise donc le loader de pyannote, qui fonctionne déjà,
et le projet n'a plus besoin du paquet casse.
"""
import numpy as np

MODELE = "pyannote/wespeaker-voxceleb-resnet34-LM"
MODELE_LOCAL = "pyannote/wespeaker-voxceleb-resnet34-LM"
DIM = 256

_MODEL = None


def _charger():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from pyannote.audio.models.embedding.wespeaker import WeSpeakerResNet34
    _MODEL = WeSpeakerResNet34.from_pretrained(MODELE_LOCAL)
    _MODEL.eval()
    return _MODEL


def disponible() -> bool:
    try:
        _charger()
        return True
    except Exception:
        return False


def _mono_16k(morceau, sr: int):
    """Mono + rééchantillonné à 16 kHz, sans dépendance externe.

    ATTENTION : `resampy` n'est pas une dépendance du projet ; inutile de
    l'ajouter, il ne servirait qu'ici. soundfile sait lire du 44,1 kHz et
    numpy suffit à interpoler. Les audios du pipeline sont déjà en
    16 kHz ; ce chemin ne sert donc que pour les fichiers de référence
    du propriétaire (44,1 kHz stéréo) — d'où l'importance de ne pas planter.
    """
    import numpy as np
    x = np.asarray(morceau, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr == 16000:
        return np.ascontiguousarray(x, dtype="float32")
    # interpolation linéaire : suffisant pour un mel-cepstre, et
    # suffisant pour 44,1 -> 16 kHz sur un extrait de parole.
    n_cible = max(1, int(round(x.shape[0] * 16000.0 / sr)))
    idx = np.linspace(0, x.shape[0] - 1, n_cible, dtype="float64")
    y = np.interp(idx, np.arange(x.shape[0], dtype="float64"), x)
    return np.ascontiguousarray(y.astype("float32"))


def empreinte(morceau, sr: int = 16000):
    """Empreinte 256-dim d'un passage. None si impossible (regle 3).

    L'appel est `model(tensor)` et NON `model({...})` : le forward de
    WeSpeakerResNet34 fait `waveforms * (1 << 15)` et un dict casse
    (TypeError: dict * int). Vérifié sur pyannote 4.0.7.
    """
    try:
        import torch
        m = _charger()
        x = _mono_16k(morceau, sr)
        if x.size < 8000:          # < 0,1 s : trop court pour une voix
            return None
        with torch.no_grad():
            out = m(torch.from_numpy(x)[None, None])
        if isinstance(out, dict):
            out = next((v for v in out.values() if hasattr(v, "detach")), None)
        v = np.asarray(out.detach().cpu().numpy(), dtype="float32").reshape(-1)
        return v if v.size == DIM else None
    except Exception:
        return None


def cosine(a, b) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))