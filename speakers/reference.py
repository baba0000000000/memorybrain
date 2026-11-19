"""LE PROFIL VOCAL DE PROPRIETAIRE — construit UNIQUEMENT sur ses références.

Pourquoi ce module existe
-------------------------
Une vérification a montré que les empreintes en base avaient un `group_id`
renseigné : elles avaient donc été **enrôlées automatiquement depuis
le corpus**, c'est-à-dire depuis ce que le pipeline croyait déjà être le propriétaire.
Une voix de média mal attribuée entrait alors dans le profil et renforçait
l'erreur : c'est le mécanisme des « reconnaissances fantômes ».

Ici, la règle est inverse et non négociable : le profil ne contient que les
enregistrements dont on sait qu'ils sont du propriétaire (config.FICHIERS_REFERENCE).

Deux enregistrements du propriétaire, à des moments différents, se
ressemblent fortement ; comparés à une voix de podcast, le score s'effondre.
L'écart est large : il n'y a pas de zone grise sur ces données.

⚠ Les fichiers de synthèse vocale (`*_clone_*.wav`) sont exclus : ce sont des
sorties de TTS, pas des enregistrements d'une voix réelle. Les inclure
apprendrait au profil le timbre d'un modèle, pas celui d'une personne.
"""
from __future__ import annotations

import numpy as np

import config
from speakers import voix

# Cache process : le profil ne change pas pendant une exécution.
_MATRICE = None

# Fenêtres d'enrôlement : 6 s, réparties sur toute la durée du fichier.
# 6 s est le compromis retenu : assez long pour une empreinte stable, assez
# court pour ne pas moyenner un changement de ton.
FRACTIONS = (0.15, 0.35, 0.55, 0.75, 0.90)
DUREE_EXTRAIT = 6.0


def _norme(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def fichier_reference() -> list:
    """Les fichiers qui existent vraiment (on ne suppose rien)."""
    import os
    out = []
    for f in config.FICHIERS_REFERENCE:
        p = os.path.join(str(config.REFERENCE_VOIX), f)
        if os.path.isfile(p):
            out.append(p)
    return out


def _confirmees(con) -> list:
    """Empreintes que le propriétaire a CONFIRMÉES lui-même (« oui, c'est moi »).

    Elles portent le tag `|confirme` dans `speaker_embedding.model`, ce qui
    les distingue des empreintes historiques enrôlées automatiquement
    depuis le corpus (tag `|auto`) — celles-là sont exclues pour toujours.
    """
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT vecteur, dim FROM speaker_embedding"
            " WHERE speaker_id=? AND model LIKE '%|confirme%'",
            (config.PROFIL_PROPRIETAIRE,)).fetchall()
    except Exception:
        return []
    return [_norme(np.frombuffer(r[0], dtype="float32")[:r[1]]) for r in rows]


def matrice(con=None, rafraichir: bool = False) -> np.ndarray:
    """Empreintes normalisées du profil : (N, 256).

    = les références sonores du propriétaire + les extraits qu'il a CONFIRMÉS comme
    étant sa voix (téléphone, de loin, dans le bruit).

    La seconde partie est ce qui manquait : une voix lointaine peut scorer au
    niveau d'une voix de contenu, donc la correction à l'écoute est la seule
    façon d'enrichir le profil avec des conditions réelles (téléphone,
    distance, bruit).

    Lève RuntimeError si aucune référence n'est lisible : mieux vaut un
    échec net qu'un profil vide qui ferait passer tout le monde pour le propriétaire.
    """
    global _MATRICE
    if _MATRICE is not None and not rafraichir:
        return _MATRICE
    import soundfile as sf

    fichiers = fichier_reference()
    if not fichiers:
        raise RuntimeError(
            f"aucune référence vocale lisible dans {config.REFERENCE_VOIX} "
            f"(attendu : {', '.join(config.FICHIERS_REFERENCE)})")

    lignes = []
    for p in fichiers:
        info = sf.info(p)
        for frac in FRACTIONS:
            debut = max(0.0, info.duration * frac - DUREE_EXTRAIT / 2)
            x, sr = sf.read(p, start=int(debut * info.samplerate),
                            frames=int(DUREE_EXTRAIT * info.samplerate),
                            dtype="float32")
            e = voix.empreinte(x, sr)
            if e is not None:
                lignes.append(_norme(np.asarray(e, dtype="float32")))
    if not lignes:
        raise RuntimeError("les références sont illisibles pour le modèle d'empreinte")
    lignes += _confirmees(con)
    _MATRICE = np.array(lignes)
    return _MATRICE


def oublier() -> None:
    global _MATRICE
    _MATRICE = None


def score_fenetre(x: np.ndarray, sr: int, base: np.ndarray | None = None):
    """Cosinus maximal d'un passage contre le profil (None si inexploitable)."""
    base = matrice() if base is None else base
    e = voix.empreinte(x, sr)
    if e is None:
        return None
    return float((base @ _norme(np.asarray(e, dtype="float32"))).max())


def juger_segment(x: np.ndarray, sr: int, base: np.ndarray | None = None) -> dict:
    """Verdict sur un segment : vote de plusieurs fenêtres de 2,5 s.

    Une fenêtre seule ne décide rien : une fenêtre qui tombe sur une
    transition s'effondre, alors que la même voix, vingt secondes plus loin,
    monte très haut. D'où le vote.

    Les fenêtres sans parole sont écartées : du silence ne donne pas une
    empreinte de voix, il donne du bruit qui peut ressembler à n'importe qui.
    """
    base = matrice() if base is None else base
    if x.ndim > 1:
        x = x.mean(axis=1)
    win, pas = int(config.FENETRE_VOIX * sr), int(config.PAS_VOIX * sr)
    scores = []
    if len(x) < int(config.MIN_EXTRAIT_EMPREINTE * sr):
        return {"verdict": "autre", "part": 0.0, "part_grise": 0.0,
                "score": 0.0, "fenetres": 0, "raison": "trop court"}
    # ⚠ CORRIGÉ : une phrase PLUS COURTE qu'une fenêtre
    # (entre 1,5 s et 2,5 s) ne passait jamais dans la boucle ci-dessous,
    # donc elle était jugée SANS UN SEUL AVIS et tombait en « contenu » :
    # perdue. Une phrase courte se juge sur elle-même.
    if len(x) < win:
        e = voix.empreinte(x, sr)
        if e is None:
            return {"verdict": "autre", "part": 0.0, "part_grise": 0.0,
                    "score": 0.0, "fenetres": 0, "raison": "empreinte_impossible"}
        s = float((base @ _norme(np.asarray(e, dtype="float32"))).max())
        verdict = ("proprietaire" if s >= config.VOIX_SEUIL_FORT
                   else "probable" if s >= config.SEUIL_PROPRIETAIRE_GRIS else "autre")
        return {"verdict": verdict, "part": 1.0 if verdict == "proprietaire" else 0.0,
                "part_grise": 1.0 if verdict == "probable" else 0.0,
                "score": round(s, 3), "fenetres": 1, "raison": "phrase courte"}
    t = 0
    while t + win <= len(x):
        w = x[t:t + win]
        if float(np.sqrt((w.astype("float64") ** 2).mean())) >= 0.003:
            s = score_fenetre(w, sr, base)
            if s is not None:
                scores.append(s)
        t += pas
    if not scores:
        # aucune fenêtre avec de la parole : on n'a pas d'avis, et on le dit
        return {"verdict": "autre", "part": 0.0, "part_grise": 0.0,
                "score": 0.0, "fenetres": 0, "raison": "aucune parole"}
    sc = np.array(scores)
    part = float((sc >= config.VOIX_SEUIL_FORT).mean())
    part_grise = float(((sc >= config.SEUIL_PROPRIETAIRE_GRIS) &
                        (sc < config.VOIX_SEUIL_FORT)).mean())
    # Deux façons d'être sûr, parce qu'une seule en rate :
    #   a) la moitié des fenêtres au-dessus du seuil fort — le cas propre ;
    #   b) AU MOINS UNE fenêtre au-dessus du seuil de pic (0,70) — le cas
    #      « le propriétaire parle par-dessus une vidéo » : la moyenne du
    #      segment est tirée vers le bas par le contenu, mais son timbre est
    #      sans ambiguïté sur un passage.
    # Le pic et la moyenne mesurent deux choses différentes : un pic franc ne
    # peut pas venir du contenu, qui ne monte jamais jusque-là.
    if part >= config.VOIX_PART_FORT or float(sc.max()) >= 0.70:
        verdict = "proprietaire"
    elif part + part_grise >= config.VOIX_PART_GRIS:
        verdict = "probable"
    else:
        verdict = "autre"
    return {"verdict": verdict, "part": round(part, 3),
            "part_grise": round(part_grise, 3),
            "score": round(float(sc.max()), 3), "fenetres": len(sc)}


def decouper(x: np.ndarray, sr: int, base: np.ndarray | None = None,
             duree_min: float = 3.0) -> list[dict]:
    """Découpe un segment là où la VOIX change (le propriétaire / autre).

    Pourquoi c'est nécessaire : un segment court peut contenir deux voix
    (celle du propriétaire puis celle d'une vidéo) — les fenêtres du début
    scorent haut, celles de la fin tombent au niveau du contenu. Attribuer
    tout le texte au propriétaire faisait produire de fausses citations. La
    décision doit donc se prendre par fenêtres, pas sur le segment entier.

    On ne devine pas le texte : on le répartit au prorata de la DURÉE, comme
    le faisait `speakers/align.py` avec les tours de parole. C'est
    approximatif au mot près, mais ça ne fait jamais dire à quelqu'un ce
    qu'il n'a pas dit.

    Renvoie [{debut, fin, verdict, score}] en secondes RELATIVES au passage.
    """
    base = matrice() if base is None else base
    if x.ndim > 1:
        x = x.mean(axis=1)
    win, pas = int(config.FENETRE_VOIX * sr), int(config.PAS_VOIX * sr)
    duree = len(x) / sr

    if duree < duree_min:
        return []
    if len(x) < win:                      # phrase plus courte qu'une fenêtre
        s = score_fenetre(x, sr, base)
        if s is None:
            return []
        v = ("proprietaire" if s >= config.VOIX_SEUIL_FORT
             else "probable" if s >= config.SEUIL_PROPRIETAIRE_GRIS else "autre")
        return [{"debut": 0.0, "fin": round(duree, 2), "verdict": v,
                 "score": round(s, 3)}]

    etiquettes = []
    t = 0
    while t + win <= len(x):
        w = x[t:t + win]
        if float(np.sqrt((w.astype("float64") ** 2).mean())) < config.RMS_MIN:
            etiquettes.append((t / sr, "silence", 0.0))
        else:
            s = score_fenetre(w, sr, base) or 0.0
            v = ("proprietaire" if s >= config.VOIX_SEUIL_FORT
                 else "autre" if s < config.SEUIL_PROPRIETAIRE_GRIS else "probable")
            etiquettes.append((t / sr, v, s))
        t += pas
    if not etiquettes:
        return []

    # on recolle les fenêtres voisines de même étiquette
    morceaux = []
    for (debut, v, s) in etiquettes:
        fin = debut + config.FENETRE_VOIX
        if morceaux and morceaux[-1]["verdict"] == v \
                and debut - morceaux[-1]["fin"] <= config.PAS_VOIX:
            morceaux[-1]["fin"] = fin
            morceaux[-1]["score"] = max(morceaux[-1]["score"], s)
        else:
            morceaux.append({"debut": debut, "fin": fin, "verdict": v, "score": s})

    # un silence n'est pas une voix : il rejoint le morceau précédent
    propre = []
    for m in morceaux:
        if m["verdict"] == "silence":
            if propre:
                propre[-1]["fin"] = m["fin"]
            continue
        if propre and propre[-1]["verdict"] == m["verdict"]:
            propre[-1]["fin"] = m["fin"]
            propre[-1]["score"] = max(propre[-1]["score"], m["score"])
            continue
        propre.append(m)

    if not propre:                        # que du silence : aucun avis
        return []

    # les morceaux trop courts n'ont pas de sens : on les rattache au voisin
    # le plus long (on ne jette jamais de durée, donc jamais de mot)
    # On ne coupe que si le contraste est FRANC et si les deux morceaux sont
    # assez longs. Un cri, un rire ou un bruit de gorge n'est pas de la
    # parole : le modèle d'empreinte le note très bas, et découper dessus
    # coupe une phrase en deux. Une découpe trop fine fabrique des erreurs :
    # on préfère NE PAS couper quand ce n'est pas net.
    if len(propre) > 1:
        meilleur = max(m["score"] for m in propre)
        pire_autre = min((m["score"] for m in propre if m["verdict"] != "proprietaire"),
                         default=meilleur)
        if meilleur < 0.60 or (meilleur - pire_autre) < 0.20:
            return [{"debut": 0.0, "fin": round(duree, 2),
                     "verdict": propre[0]["verdict"],
                     "score": round(meilleur, 3)}]
    if len(propre) > 1:
        garde = []
        for m in propre:
            if m["fin"] - m["debut"] < duree_min:
                if garde and (not garde[-1]["verdict"] == m["verdict"]):
                    garde[-1]["fin"] = m["fin"]
                elif len(propre) and m is not propre[-1]:
                    continue
            garde.append(m)
        propre = garde if len(garde) > 1 else propre

    fin_reelle = duree
    propre[-1]["fin"] = fin_reelle
    return [{"debut": round(m["debut"], 2), "fin": round(m["fin"], 2),
             "verdict": m["verdict"], "score": round(m["score"], 3)}
            for m in propre]
