"""ÉTAPE 5 — ALIGNEMENT : à qui appartient ce texte ?

On a deux choses dans le groupe :
  - des SEGMENTS de texte (Whisper), avec des temps ;
  - des TOURS de parole (pyannote), avec des temps et une étiquette.

Elles viennent de deux modèles différents, indépendants. On doit les
rapprocher. C'est l'étape qui transforme un transcript technique en
transcript lisible : « qui a dit quoi ».

Méthode : recouvrement temporel, avec partage au prorata.

    Pour chaque segment de texte, on regarde quels tours de parole il
    chevauche. Le segment est ensuite DÉCOUPÉ en autant de morceaux que
    de tours qui le couvrent, chaque morceau portant l'étiquette du tour
    qui le recouvre.

Pourquoi découper plutôt qu'attribuer tout le segment au tour majoritaire :
il arrive qu'un segment de texte chevauche deux voix différentes (une
phrase commence sur une réponse et finit sur une autre). Attribuer tout au
tour majoritaire ferait dire à quelqu'un des mots qu'il n'a pas prononcés —
c'est exactement le défaut de la V1.

Deux cas particuliers traités :

1. **Segment sans aucun tour** (pyannote n'a rien couvert) → on ne devine
   pas. Le texte reste sans profil, avec un marqueur explicite
   `[?]` dans la sortie. Règle 3 : mieux vaut une lacune visible qu'une
   attribution inventée.

2. **Chevauchement partiel et faible** (moins de 40 % du segment, ou
   moins de 0,6 s) → on préfère aussi laisser inconnu plutôt que
   d'attribuer sur une base fragile. Le seuil est dans config.

Sortie : une liste d'interventions `{start, end, texte, label, ratio}`
dans l'ordre chronologique, prête à devenir des phrases.
"""
import re
import sqlite3

import config

# Un tour qui ne couvre qu'une petite fraction du segment ne « possède »
# pas ce segment. En dessous de ça, le texte reste sans profil.
SEUIL_PART = 0.40
DUREE_MIN_TOUR = 0.6

# Deux tours de la MÊME voix distants de moins de `FUSION_GAP` secondes
# de SILENCE REEL sont une seule intervention coupée en deux.
#
# ⚠ SEUIL EMPIRIQUE, PAS CHOISI AU HASARD. Trop grand, il recolle deux
# répliques distinctes ; trop petit, il laisse le transcript en miettes
# (« c'est », « la », « je » sur trois lignes pour une seule phrase).
#
# Le gap doit mesurer le SILENCE, pas la distance entre les fins : deux tours
# peuvent être à 25 s l'un de l'autre tout en étant coupés par moins de 2 s
# de vrai silence — des segments Whisper séparés par des dizaines de secondes
# font parfois partie de la même réplique. On cumule donc les silences entre deux tours de la
# même voix tant qu'ils restent sous ce seuil.
FUSION_GAP = 2.5

# Un tour plus court que ça n'est pas une réplique, c'est un fragment.
# Sur ces fragments, pyannote alterne les étiquettes — une seule personne
# se voit attribuer SPEAKER_01 puis SPEAKER_02 puis SPEAKER_01 en quelques
# secondes. Garder seulement les tours d'au moins 1,5 s élimine ces
# faux changements de locuteur.
TOUR_SIGNIFICATIF = 1.5


def _chevauchement(a0: float, a1: float, b0: float, b1: float) -> float:
    """Secondes communes à deux intervalles. Jamais négatif."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def aligner_segment(tours: list[tuple[float, float, str]],
                    seg: tuple[float, float, str]) -> list[dict]:
    """Découpe un segment sur les tours qui le recouvrent.

    `tours`  : [(début, fin, label), ...] triés, sans recouvrement
    `seg`    : (début, fin, texte)
    Renvoie  : [{start, end, texte, label, ratio}]

    ⚠ BUG CORRIGÉ ICI. La version précédente mettait le TEXTE ENTIER dans
    chaque morceau qui recouvrait. Sur un segment traversé par plusieurs
    tours alternés, elle produisait le même texte autant de fois, avec un
    profil différent à chaque fois — le lecteur croyait que plusieurs
    personnes avaient répété la même phrase.

    Le texte doit être RÉPARTI au prorata temporel. Si le tour A couvre
    0-2 s, le tour B 2-4 s et le tour C 4-6 s, chacun reçoit le tiers de
    la phrase qui correspond à sa fenêtre. C'est approximatif — on ne
    sait pas quel mot tombe à quel millième — mais c'est la seule chose
    qui ne fasse pas dire n'importe quoi à quelqu'un.
    """
    s0, s1, texte = seg
    duree = s1 - s0
    if duree <= 0:
        return []

    # d'abord, on ne garde que les tours qui justifient une attribution
    retenus = []
    for (t0, t1, label) in tours:
        d = _chevauchement(s0, s1, t0, t1)
        if d <= 0:
            continue
        ratio = d / duree
        if ratio < SEUIL_PART and d < DUREE_MIN_TOUR:
            # trop faible pour être une attribution fiable : ignoré
            continue
        retenus.append((max(s0, t0), min(s1, t1), label, ratio))

    if not retenus:
        return []

    # un seul tour couvre tout : on rend le texte intact, sans découpe
    if len(retenus) == 1 and retenus[0][0] <= s0 + 0.001 \
            and retenus[0][1] >= s1 - 0.001:
        return [{"start": s0, "end": s1, "texte": texte,
                 "label": retenus[0][2], "ratio": 1.0}]

    # ⚠ RÉPARTITION EN PARTITION COMPLÈTE, ET NON EN PARTIES DE TOURS.
    # La version précédente découpait le texte proportionnellement à
    # CHAQUE tour, ce qui laissait vides les trous entre deux tours —
    # et ces trous contiennent du texte. Un segment de texte peut durer
    # 85 s quand les tours, eux, durent 1 à 5 s : un tiers des mots
    # disparaissait, presque tous des mots de liaison (« tu », « c'est »,
    # « le ») qui tombaient précisément dans les intervalles non couverts.
    #
    # La méthode correcte : on découpe le segment en portions continues
    # qui, elles, se touchent. Chaque portion est attribuée au tour qui la
    # recouvre, ou au tour le plus proche si elle tombe dans un trou. Le
    # texte est ainsi réparti SUR 100 % du segment, sans trou.
    bornes = sorted((m0, m1, label) for (m0, m1, label, _) in retenus)

    # points de coupe : toutes les extrémités de tours, plus les bords
    points = {s0, s1}
    for (m0, m1, _) in bornes:
        if s0 < m0 < s1:
            points.add(m0)
        if s0 < m1 < s1:
            points.add(m1)
    coupes = sorted(points)

    # ⚠ BUG CORRIGÉ ICI (trouvé par le contrôle « 0 mot perdu »). La version précédente calculait l'intervalle de mots de
    # CHAQUE portion indépendamment, avec `_part_texte`, en forçant un mot
    # minimum quand la fenêtre était trop étroite. Deux portions voisines
    # pouvaient donc se PARTAGER le même mot : il apparaissait deux fois, et
    # la rustine qui dédupliquait à la fusion mangeait alors aussi les
    # répétitions LÉGITIMES (« … ça » / « ça sera… » ⇒ un « ça » réel
    # disparaissait). Bilan : un mot perdu, deux ajoutés.
    #
    # La bonne méthode : calculer UNE fois l'indice de mot à chaque borne de
    # temps, puis découper la liste de mots sur ces bornes PARTAGÉES. Les
    # portions forment alors une partition STRICTE : chaque mot est rendu
    # exactement une fois, ni perdu ni dupliqué. Plus rien à dédupliquer.
    mots = texte.split()
    n = len(mots)

    def indice(t: float) -> int:
        return max(0, min(n, int(round((t - s0) / duree * n))))

    morceaux = []
    for i in range(len(coupes) - 1):
        c0, c1 = coupes[i], coupes[i + 1]
        if c1 - c0 <= 0.001:
            continue
        milieu = (c0 + c1) / 2
        # le tour qui recouvre le milieu de la portion
        label = None
        for (m0, m1, lbl) in bornes:
            if m0 <= milieu <= m1:
                label = lbl
                break
        if label is None:
            # portion dans un trou entre deux tours : on prend le tour le
            # plus proche. Le texte reste, l'attribution est approximative.
            label = min(bornes, key=lambda t: min(abs(t[0] - milieu),
                                                   abs(t[1] - milieu)))[2]
        i0, i1 = indice(c0), indice(c1)
        part = " ".join(mots[i0:i1])
        if not part:
            # la borne est trop serrée pour contenir un mot entier : cette
            # portion n'existe pas dans le texte, on la saute. Aucun mot
            # perdu : le mot limite appartient à la portion voisine.
            continue
        if morceaux and morceaux[-1]["label"] == label:
            morceaux[-1]["end"] = c1
            morceaux[-1]["texte"] = (morceaux[-1]["texte"] + " " + part).strip()
            continue
        morceaux.append({"start": c0, "end": c1, "texte": part,
                         "label": label,
                         "ratio": round((c1 - c0) / duree, 3)})
    return morceaux


def _sans_profil(texte: str) -> str:
    """Retire les marqueurs de locuteur du texte.

    Nos transcripts contiennent `**profil_3** :` et le LLM les recopie
    (« Profil_3 parle de son travail »). C'est un défaut de notre mise en
    forme, pas une idée du locuteur. On retire l'étiquette ET les
    astérisques qui l'entourent.

    ⚠ Le motif doit tolérer les `**` : `**profil_3**` ne matche pas
    `profil_\\d+\\s+` parce qu'il n'y a pas d'espace juste après le
    nombre — sans ça, le nettoyage ne fait rien.
    """
    texte = re.sub(r"\*\*\s*profil_\d+\s*\*\*\s*:?\s*", "", texte, flags=re.I)
    texte = re.sub(r"\bprofil_\d+\s*:?\s*", "", texte, flags=re.I)
    return texte


def _fusionner_interventions(items: list[dict]) -> list[dict]:
    """Fusionne les morceaux consécutifs d'une même voix.

    Après le découpage, une même intervention peut être coupée en trois
    morceaux par un micro-silence au milieu. On les recolle : « Bonjour »
    coupé en trois est illisible, et une phrase par ligne est lisible.

    ⚠ La fusion doit CONCATENER les textes. La version précédente ne
    gardait que le texte du PREMIER morceau et jetait les deux autres : un
    test le prouve (« je » + « vais » + « partir » → « je »). Perte de
    mots silencieuse, exactement le genre d'erreur qu'on ne voit qu'en
    relisant le transcript.
    """
    fusionnes: list[dict] = []
    for it in sorted(items, key=lambda z: z["start"]):
        if not fusionnes:
            fusionnes.append(dict(it))
            fusionnes[0]["_silence"] = 0.0
            continue
        precedent = fusionnes[-1]
        meme_voix = precedent["label"] == it["label"]
        silence = max(0.0, it["start"] - precedent["end"])
        # le silence se CUMULE : plusieurs trous de 1 s avec de la parole
        # de 3 s entre les deux ne font pas une nouvelle intervention.
        cumule = precedent.get("_silence", 0.0) + silence
        if meme_voix and cumule <= FUSION_GAP:
            precedent["end"] = max(precedent["end"], it["end"])
            precedent["ratio"] = round(
                min(1.0, precedent["ratio"] + it["ratio"]), 3)
            # ⚠ On CONCATÈNE sans jamais retirer de mot. La rustine qui
            # supprimait un mot répété à la frontière (l'ancien texte finit
            # par le même mot que le nouveau commence) mangeait de vraies
            # répétitions : « … ça » / « ça sera… » perdait un « ça » réel,
            # ce que le contrôle « 0 mot perdu » a attrapé. Le découpage
            # étant désormais une partition STRICTE
            # (voir aligner_segment), aucun mot ne peut être dupliqué à une
            # frontière : il n'y a plus rien à dédupliquer.
            avant = precedent["texte"].rstrip()
            apres = it["texte"].strip()
            if apres:
                precedent["texte"] = f"{avant} {apres}" if avant else apres
            precedent["_silence"] = cumule
        else:
            fusionnes.append(dict(it))
            fusionnes[-1]["_silence"] = 0.0
    for f in fusionnes:
        f["texte"] = f["texte"].strip()
        f.pop("_silence", None)      # clé interne, pas une donnée

    # On écarte les interventions trop courtes pour être une réplique :
    # elles viennent d'un micro-tour pyannote et produisent du texte en
    # miettes. On ne les jette que si la VOIX voisine est la même —
    # sinon on perdrait une vraie prise de parole courte (« ouais. »).
    if len(fusionnes) > 1:
        resultat_filtre: list[dict] = []
        for i, f in enumerate(fusionnes):
            mots = len(f["texte"].split())
            if mots >= 3:
                resultat_filtre.append(f)
                continue
            precedent = resultat_filtre[-1] if resultat_filtre else None
            suivant = fusionnes[i + 1] if i + 1 < len(fusionnes) else None
            # on rattache à l'intervention longue de la MÊME voix la plus
            # proche : le texte reste, l'attribution reste cohérente.
            if precedent and precedent["label"] == f["label"]:
                precedent["texte"] = (precedent["texte"] + " " + f["texte"]).strip()
                precedent["end"] = max(precedent["end"], f["end"])
                continue
            if suivant and suivant["label"] == f["label"] \
                    and len(suivant["texte"].split()) >= 3:
                suivant["texte"] = (f["texte"] + " " + suivant["texte"]).strip()
                suivant["start"] = min(suivant["start"], f["start"])
                continue
            # voix différente et seule : on garde quand même, mais c'est
            # rare et le lecteur verra une intervention très courte.
            resultat_filtre.append(f)
        fusionnes = resultat_filtre

    return fusionnes


def _coller_tours(bruts: list) -> list[tuple[float, float, str]]:
    """Recolle les tours en interventions lisibles.

    Deux opérations, dans cet ordre, parce que le problème n'est pas le
    silence mais la FRAGMENTATION.

    ⚠ CE QUE MONTRE LE DIAGNOSTIC. Sur quelques secondes, pyannote produit :
          0,9 s  SPEAKER_01
          5,0 s  SPEAKER_02   ← chevauche
          0,7 s  SPEAKER_01
          0,5 s  SPEAKER_01
          1,3 s  SPEAKER_02
    Une seule personne parle, l'étiquette change plusieurs fois. C'est le
    comportement normal de pyannote : il n'a aucune mémoire d'un tour à
    l'autre. Sur des tours très courts séparés par de longs silences, il
    « change de locuteur » à chaque phrase.

    Conséquence : sans correction, le transcript sort en miettes — « c'est »
    / « la » / « je sais » / « quoi. » sur quatre lignes avec des profils
    différents, pour une seule réplique.

    Le correctif, en deux temps :
      1. les tours de moins de TOUR_SIGNIFICATIF secondes sont trop courts
         pour être une réplique à eux seuls : on les rattache au tour
         significatif qui les contient ou les jouxte ;
      2. ensuite seulement, on cumule les silences entre tours de la même
         voix pour recoller les vraies phrases coupées.
    """
    if not bruts:
        return []

    # 1. On ne garde que les tours significatifs.
    significatifs = [t for t in bruts
                     if (t["end_time"] - t["start_time"]) >= TOUR_SIGNIFICATIF]
    if not significatifs:
        # groupe entièrement composé de micro-tours : on garde le plus
        # long plutôt que de ne rien rendre (règle 3)
        significatifs = [max(bruts, key=lambda t: t["end_time"] - t["start_time"])]

    # 2. On recolle les silences entre tours de la même voix.
    resultat: list[tuple[float, float, str]] = [
        (significatifs[0]["start_time"], significatifs[0]["end_time"],
         significatifs[0]["label_local"])]
    silence_cumule = 0.0
    for t in significatifs[1:]:
        precedent = resultat[-1]
        if t["label_local"] == precedent[2]:
            silence = max(0.0, t["start_time"] - precedent[1])
            if silence_cumule + silence <= FUSION_GAP:
                resultat[-1] = (precedent[0], t["end_time"], t["label_local"])
                silence_cumule += silence
                continue
        resultat.append((t["start_time"], t["end_time"], t["label_local"]))
        silence_cumule = 0.0
    return resultat


def aligner_groupe(con: sqlite3.Connection, group_id: int) -> dict:
    """Aligne texte et tours pour un groupe entier.

    Renvoie les interventions dans l'ordre chronologique, avec le
    `speaker_id` résolu quand il existe (étape 4), sinon None.
    """
    segments = con.execute(
        "SELECT s.start_time, s.end_time, s.text FROM segment s"
        " JOIN group_member m ON m.chunk_id = s.chunk_id"
        " WHERE m.group_id=? AND s.text <> ''"
        " AND COALESCE(s.audio_kind,'parole') NOT IN ('musique','hallucination')"
        " ORDER BY s.start_time",
        (group_id,)).fetchall()

    tours_bruts = con.execute(
        "SELECT start_time, end_time, label_local, speaker_id FROM turn"
        " WHERE group_id=? ORDER BY start_time", (group_id,)).fetchall()

    if not tours_bruts:
        return {"interventions": [], "segments": len(segments),
                "tours": 0, "couverts": 0, "inconnus": len(segments)}

    # On recolle les tours contigus de la MÊME voix AVANT d'aligner, sinon un
    # même locuteur produit des dizaines de micro-tours et le découpage des
    # segments devient illisible. Le cumul des silences est fait par
    # `_coller_tours` — voir le commentaire sur FUSION_GAP pour pourquoi on
    # ne peut pas se contenter de l'écart brut entre deux fins de tours.
    tours = _coller_tours(tours_bruts)

    tous_items = []
    couverts = 0
    orphelins: list[tuple] = []
    for s in segments:
        morceaux = aligner_segment(tours, (s["start_time"], s["end_time"], s["text"]))
        if morceaux:
            couverts += 1
            tous_items.extend(morceaux)
        else:
            # Aucun tour significatif ne recouvre ce segment. Ça arrive
            # quand le texte tombe entièrement dans un micro-tour écarté
            # par TOUR_SIGNIFICATIF. Constaté : quelques segments, dont le
            # PREMIER du groupe (~120 mots), qui disparaissait entièrement
            # du transcript.
            #
            # On ne perd pas le texte. On le rattache au tour significatif
            # le plus proche en temps, même si la voix est incertaine :
            # aucun profil vaut mieux que des mots disparus.
            orphelins.append((s["start_time"], s["end_time"], s["text"]))

    interventions = _fusionner_interventions(tous_items)

    if orphelins and tours:
        # insertion à la bonne place chronologique
        for (o0, o1, texte) in orphelins:
            proche = min(tours, key=lambda t: min(abs(t[1] - o0), abs(o0 - t[0])))
            item = {"start": o0, "end": o1, "texte": texte,
                    "label": proche[2], "ratio": 0.0, "orphelin": True}
            interventions.append(item)
        interventions = _fusionner_interventions(interventions)

    # résolution des profils : label_local -> speaker_id
    resolution = {t["label_local"]: t["speaker_id"] for t in tours_bruts}
    for it in interventions:
        it["speaker_id"] = resolution.get(it["label"])
        it["texte"] = it["texte"].strip()

    return {
        "interventions": interventions,
        "segments": len(segments),
        "tours": len(tours),
        "couverts": couverts,
        "inconnus": len(segments) - couverts,
        "voix": sorted({it["label"] for it in interventions}),
    }


def en_markdown(interventions: list[dict],
                noms: dict[int, str] | None = None) -> str:
    """Le transcript lisible : une ligne par intervention, avec le nom.

    Règle 17 : le Markdown est la sortie HUMAINE. C'est ce que le propriétaire
    lit et ce qu'il lui reste à corriger.
    """
    noms = noms or {}
    lignes = []
    for it in interventions:
        spid = it.get("speaker_id")
        if spid is not None and noms.get(spid):
            qui = noms[spid]
        elif spid is not None:
            qui = f"profil_{spid}"
        else:
            # Une parole non attribuée
            # n'est pas une personne inconnue : c'est du CONTENU (émission,
            # vidéo, interlocuteur non identifié). Il n'existe qu'un seul
            # profil, celui du propriétaire — on ne fabrique plus d'identité.
            qui = "contenu"
        lignes.append(f"**{qui}** : {it['texte']}")
    return "\n\n".join(lignes)
