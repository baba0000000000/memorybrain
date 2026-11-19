"""Accès au Compute Hub (Celery + Redis) : répartition facultative du
travail sur plusieurs workers.

Le Hub répartit le travail sur tous les workers connectés. Il ACCÉLÈRE la
chaîne, il ne la décide pas : un worker peut recevoir un job dont il ne
peut pas lire le fichier, et le job reste alors « active » indéfiniment
pendant que tout paraît normal, la chaîne ne produisant plus rien.

D'où la règle unique de ce module :

    ON SONDE AVANT D'ENVOYER, ET ON RENVOIE None SI RIEN NE VIENT.

Aucun appelant ne doit rester bloqué à cause du Hub : il ACCÉLÈRE, il ne
décide pas. Chaque appelant garde son chemin local en repli.

Deuxième règle : on n'envoie JAMAIS un chemin absolu. Un chemin absolu
n'est lisible que par la machine qui l'a émis ; le job qui tombe ailleurs
échoue. On envoie un NOM DE PARTAGE, que chaque worker résout chez lui via
AIINFRA_SHARED_ROOTS (voir la documentation de déploiement des workers).
"""
import os
import time
from pathlib import Path

# Dossier du paquet qui expose l'application Celery (hors dépôt).
HUB_DIR = Path(os.environ.get(
    "MEMORYBRAIN_HUB_DIR", Path.home() / ".local" / "share" / "memorybrain-hub"))

# Nom de partage des WAV de groupe, déclaré par les DEUX workers. Un seul
# nom pour tout le monde : c'est le worker qui le traduit en chemin local.
PARTAGE = "memorybrain-groupes"

# Attente max d'un résultat. Large : un gros groupe peut prendre 10 min.
DELAI = int(os.environ.get("MB_HUB_DELAI", "1800"))

# La sonde coûte ~3 s (aller-retour Redis). Une sonde par appel coûterait
# plus qu'elle ne rapporte : on garde le résultat 60 s. Assez court pour
# voir un worker apparaître en cours de route, assez long pour ne pas
# ralentir.
_DUREE_SONDE = 60
_SONDE = {"quand": 0.0, "workers": []}


def workers() -> list[str]:
    """Noms des workers vivants. Vide si le Hub est muet ou absent."""
    if time.time() - _SONDE["quand"] < _DUREE_SONDE:
        return _SONDE["workers"]
    noms: list[str] = []
    try:
        import sys
        if str(HUB_DIR) not in sys.path:
            sys.path.insert(0, str(HUB_DIR))
        from aiinfra_app import app
        noms = sorted((app.control.inspect(timeout=3).ping() or {}).keys())
    except Exception:
        noms = []
    _SONDE.update(quand=time.time(), workers=noms)
    return noms


def actif() -> bool:
    """Y a-t-il au moins un worker ? Si non : ne rien soumettre du tout."""
    return bool(workers())


def oublier() -> None:
    """Invalide la sonde gardée (après un échec, on veut revérifier)."""
    _SONDE["quand"] = 0.0
    _SONDE["workers"] = []


def soumettre(tache: str, args: list, kwargs: dict | None = None,
              delai: int | None = None) -> dict | None:
    """Envoie une tâche au Hub et attend le résultat. None si rien.

    None couvre TOUS les cas d'échec — pas de worker, délai dépassé,
    exception, tâche en erreur, réponse illisible. L'appelant n'a donc
    qu'un seul cas à traiter : « le Hub n'a rien donné, je fais en local ».
    """
    try:
        import sys
        if str(HUB_DIR) not in sys.path:
            sys.path.insert(0, str(HUB_DIR))
        from aiinfra_app import app
        res = app.send_task(tache, args=args, kwargs=kwargs or {})
        out = res.get(timeout=delai or DELAI)
    except Exception:
        return None
    if not isinstance(out, dict) or out.get("error"):
        return None
    return out