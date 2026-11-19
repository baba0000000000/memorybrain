"""Structure de vérification — UN CONTRÔLE PAR ÉTAPE.

Principe : chaque étape passe son contrôle avant la suivante — si l'étape 1
a laissé des données fausses, tout ce qui vient après travaille sur du faux.

Règle de lecture, importante : un contrôle distingue toujours trois états.

  * `non_fait`  → l'étape n'a pas encore tourné sur ce groupe. Ce n'est
                  PAS un défaut (règle 3 : le pipeline continue). Un
                  rapport qui crie au bug sur ce qui n'est pas encore
                  passé produit du bruit, et on apprend à l'ignorer ;
  * `ok`        → l'étape a tourné ET les données tiennent le contrôle ;
  * `probleme`  → l'étape a tourné ET la sortie est fausse. C'est le
                  seul cas qui bloque la suite.

La règle « le nombre de mots en base == le nombre de mots du transcript »
(étape 5) est le contrôle le plus important de la chaîne : c'est le seul
qui révèle une perte de texte, et il n'est pas devinable.
"""
import re
import sqlite3
from collections import Counter

import config
from grouping import group as gmod

# Ordre du graphe (identique à pipeline_v2.ETAPES).
ETAPES = ("groupement", "transcription", "voix", "alignement",
          "classification", "markdown", "extraction")

_MOTS = re.compile(r"[0-9A-Za-zÀ-ÿ']+")


def _mots(texte: str) -> Counter:
    return Counter(m.lower() for m in _MOTS.findall(texte or ""))


def _a_tourne(con: sqlite3.Connection, etape: str, group_id: int) -> bool:
    """L'étape a-t-elle été lancée sur ce groupe ? (journal `meta`).

    C'est ce qui sépare « pas encore fait » de « fait et faux ». Sur une
    base neuve, la table `meta` peut ne pas exister encore : cela veut
    dire qu'aucune étape n'a tourné, donc False (et pas une exception).
    """
    try:
        r = con.execute("SELECT value FROM meta WHERE key=?",
                        (f"etape:{etape}:{group_id}",)).fetchone()
    except sqlite3.OperationalError:
        return False
    return r is not None


def _tous_les_groupes(con: sqlite3.Connection) -> list[int]:
    return [r["group_id"] for r in
            con.execute("SELECT group_id FROM chunk_group ORDER BY group_id")]


def _res(etape: str) -> dict:
    return {"etape": etape, "ok": True, "faits": 0, "non_faits": 0,
            "problemes": [], "mesures": {}}


# ---------------------------------------------------------------- étape 1

def controler_groupement(con, groupes=None) -> dict:
    """1. Chaque groupe = exactement 5 chunks, contigus, somme = durée WAV."""
    groupes = groupes or _tous_les_groupes(con)
    r = _res("groupement")
    if not groupes:
        r["mesures"]["detail"] = "aucun groupe construit"
        return r
    import os
    for gid in groupes:
        v = gmod.verifier_membres(con, gid)
        if not v.get("ok"):
            r["ok"] = False
            r["problemes"].append(f"groupe {gid} : membres incorrects {v}")
        g = con.execute("SELECT n_chunks, taille, audio_path FROM chunk_group"
                        " WHERE group_id=?", (gid,)).fetchone()
        if g and g["n_chunks"] != config.GROUPE_TAILLE:
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : {g['n_chunks']} chunks au lieu de "
                f"{config.GROUPE_TAILLE}")
        if g and g["audio_path"] and not os.path.exists(g["audio_path"]):
            r["ok"] = False
            r["problemes"].append(f"groupe {gid} : WAV absent ({g['audio_path']})")
        r["faits"] += 1
    return r


# ---------------------------------------------------------------- étape 2

def controler_transcription(con, groupes=None) -> dict:
    """2. Segments présents, bornés dans leur chunk, horaires renseignés."""
    groupes = groupes or _tous_les_groupes(con)
    r = _res("transcription")
    for gid in groupes:
        if not _a_tourne(con, "transcription", gid):
            r["non_faits"] += 1
            continue
        r["faits"] += 1
        n = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=?", (gid,)).fetchone()[0]
        vides = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=?"
            " AND (s.start_time IS NULL OR s.end_time IS NULL)", (gid,)).fetchone()[0]
        hors = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN chunk c ON c.chunk_id=s.chunk_id"
            " JOIN group_member m ON m.chunk_id=s.chunk_id"
            " WHERE m.group_id=? AND (s.start_time < c.start_time-0.05"
            " OR s.end_time > c.end_time+0.05)", (gid,)).fetchone()[0]
        r["mesures"][gid] = {"segments": n, "sans_horaire": vides, "hors_chunk": hors}
        if vides:
            r["ok"] = False
            r["problemes"].append(f"groupe {gid} : {vides} segment(s) sans horaire")
        # Un débordement isolé (quelques cas sur des milliers de segments)
        # est toléré et signalé.
        if n and hors > max(3, n * 0.01):
            r["ok"] = False
            r["problemes"].append(f"groupe {gid} : {hors} segments hors de "
                                  f"leur chunk sur {n}")
        # 0 segment = silence légitime : noté, pas un défaut.
    return r


# ---------------------------------------------------------------- étape 3

def controler_voix(con, groupes=None) -> dict:
    """3. Une seule voix identifiée (le propriétaire) ; le reste est du contenu.

    Ce que ce contrôle refuse, et pourquoi :

      * un profil autre que 0 dans `speaker` — c'est la cause du désastre
        précédent (des dizaines de profils pour un nommé) ;
      * un tour PROPRIETAIRE sans speaker_id 0, ou un tour AUTRE avec un
        speaker_id — l'étiquette et l'identité doivent dire la même
        chose, sinon le markdown attribue au propriétaire des phrases de média ;
      * un groupe marqué « musique » qui contient pourtant du le propriétaire — sa
        voix primant toujours, ce serait une perte de contenu.
    """
    groupes = groupes or _tous_les_groupes(con)
    r = _res("voix")
    for gid in groupes:
        if not _a_tourne(con, "voix", gid):
            r["non_faits"] += 1
            continue
        r["faits"] += 1
        tours = con.execute(
            "SELECT label_local, speaker_id FROM turn WHERE group_id=?",
            (gid,)).fetchall()
        segs = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=?", (gid,)).fetchone()[0]
        proprietaire = con.execute(
            "SELECT COUNT(*) FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=? AND s.speaker_id=?",
            (gid, config.PROFIL_PROPRIETAIRE)).fetchone()[0]
        statut = con.execute("SELECT statut FROM chunk_group WHERE group_id=?",
                             (gid,)).fetchone()[0]
        r["mesures"][gid] = {"segments": segs, "proprietaire": proprietaire,
                             "tours": len(tours), "statut": statut}
        for t in tours:
            incoherent = ((t["label_local"] == "PROPRIETAIRE"
                           and t["speaker_id"] != config.PROFIL_PROPRIETAIRE)
                          or (t["label_local"] != "PROPRIETAIRE"
                              and t["speaker_id"] is not None))
            if incoherent:
                r["ok"] = False
                r["problemes"].append(
                    f"groupe {gid} : tour {t['label_local']} avec "
                    f"speaker_id={t['speaker_id']}")
        if segs and not tours:
            r["ok"] = False
            r["problemes"].append(f"groupe {gid} : {segs} segments, 0 tour")
        if statut == "musique" and proprietaire:
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : marqué musique mais {proprietaire} segment(s) le propriétaire")
    if r["faits"]:
        intrus = con.execute(
            "SELECT COUNT(*) FROM speaker WHERE speaker_id<>?",
            (config.PROFIL_PROPRIETAIRE,)).fetchone()[0]
        if intrus:
            r["ok"] = False
            r["problemes"].append(
                f"{intrus} profil(s) en base alors que seul le propriétaire doit exister")
        p0 = con.execute("SELECT name FROM speaker WHERE speaker_id=?",
                         (config.PROFIL_PROPRIETAIRE,)).fetchone()
        if p0 is None:
            r["ok"] = False
            r["problemes"].append("profil 0 (le propriétaire) absent de la table speaker")
    return r


# ---------------------------------------------------------------- étape 5

def controler_alignement(con, groupes=None) -> dict:
    """5. LE contrôle : aucun mot ne se perd entre la base et le transcript."""
    from speakers import align
    groupes = groupes or _tous_les_groupes(con)
    r = _res("alignement")
    for gid in groupes:
        if not _a_tourne(con, "alignement", gid):
            r["non_faits"] += 1
            continue
        r["faits"] += 1
        segs = con.execute(
            "SELECT s.text FROM segment s JOIN group_member m"
            " ON m.chunk_id=s.chunk_id WHERE m.group_id=? AND s.text <> ''"
            " AND COALESCE(s.audio_kind,'parole') <> 'musique'",
            (gid,)).fetchall()
        base = Counter()
        for s in segs:
            base += _mots(s["text"])
        a = align.aligner_groupe(con, gid)
        trans = Counter()
        for it in a["interventions"]:
            trans += _mots(it["texte"])
        perdus = base - trans
        ajoutes = trans - base
        n_perdus = sum(perdus.values())
        r["mesures"][gid] = {
            "mots_base": sum(base.values()),
            "mots_transcript": sum(trans.values()),
            "mots_perdus": n_perdus,
            "mots_ajoutes": sum(ajoutes.values()),
            "exemples_perdus": [m for m, _ in perdus.most_common(8)],
        }
        if n_perdus > 0:
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : {n_perdus} mot(s) perdu(s) "
                f"({[m for m, _ in perdus.most_common(6)]})")
    return r


# ---------------------------------------------------------------- étape 7

def controler_classification(con, groupes=None) -> dict:
    """7. Chaque groupe traité porte une classe."""
    groupes = groupes or _tous_les_groupes(con)
    r = _res("classification")
    for gid in groupes:
        if not _a_tourne(con, "classification", gid):
            r["non_faits"] += 1
            continue
        r["faits"] += 1
        c = con.execute("SELECT classe FROM classification WHERE group_id=?",
                        (gid,)).fetchone()
        r["mesures"][gid] = {"classe": c["classe"] if c else None}
        if c is None or c["classe"] not in ("PERSONNEL", "MEDIA", "BUSINESS",
                                            "UNKNOWN"):
            r["ok"] = False
            r["problemes"].append(f"groupe {gid} : classe absente ou invalide")
    return r


# ---------------------------------------------------------------- étape 7 bis

def controler_markdown(con, groupes=None) -> dict:
    """8. Le livrable lisible contient TOUS les mots, rien d'autre.

    C'est le contrôle « bout en bout » : le markdown est ce que le propriétaire lit
    (règle 17). Il doit porter EXACTEMENT les mots prononcés, ni perdu, ni
    inventé. On compte les mots des lignes d'intervention (on retire le
    préfixe « **profil_X** : ») et on les compare aux mots des segments.

    Sans ce contrôle, `markdown` n'était pas dans CONTROLES : le
    pipeline affichait un « OK » vert alors qu'il ne vérifiait RIEN
    (trouvé).
    """
    import re as _re
    groupes = groupes or _tous_les_groupes(con)
    r = _res("markdown")
    prefixe = _re.compile(r"^\*\*[^*]+\*\*\s*:\s*")
    for gid in groupes:
        if not _a_tourne(con, "markdown", gid):
            r["non_faits"] += 1
            continue
        r["faits"] += 1
        chemin = config.WORK_TRACE / f"groupe_{gid:04d}_transcript.md"
        if not chemin.exists():
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : markdown absent ({chemin.name})")
            continue
        base = Counter()
        for s in con.execute(
                "SELECT s.text FROM segment s JOIN group_member m"
                " ON m.chunk_id=s.chunk_id WHERE m.group_id=? AND s.text<>''",
                (gid,)):
            base.update(s["text"].split())
        trans = Counter()
        lignes = 0
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            m = prefixe.match(ligne)
            if not m:
                continue
            lignes += 1
            trans.update(ligne[m.end():].split())
        r["mesures"][gid] = {"lignes": lignes,
                             "mots_base": sum(base.values()),
                             "mots_markdown": sum(trans.values())}
        perdus = base - trans
        ajoutes = trans - base
        if sum(perdus.values()):
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : {sum(perdus.values())} mot(s) perdu(s) dans "
                f"le markdown ({[m for m, _ in perdus.most_common(6)]})")
        if sum(ajoutes.values()):
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : {sum(ajoutes.values())} mot(s) en trop dans "
                f"le markdown ({[m for m, _ in ajoutes.most_common(6)]})")
    return r


# ---------------------------------------------------------------- étape 8

def controler_extraction(con, groupes=None) -> dict:
    """8. Un groupe MEDIA/UNKNOWN ne produit AUCUN item. Le LLM doit être
    chocolatine, pas un modèle resté sur le port."""
    groupes = groupes or _tous_les_groupes(con)
    r = _res("extraction")
    for gid in groupes:
        if not _a_tourne(con, "extraction", gid):
            r["non_faits"] += 1
            continue
        r["faits"] += 1
        cl = con.execute("SELECT classe FROM classification WHERE group_id=?",
                         (gid,)).fetchone()
        classe = cl["classe"] if cl else None
        items = con.execute(
            "SELECT kind, llm, chemin FROM extraction WHERE group_id=?",
            (gid,)).fetchall()
        r["mesures"][gid] = {"classe": classe, "items": len(items),
                             "llm": sorted({i["llm"] for i in items}),
                             "origines": sorted({i["chemin"] or "général"
                                                 for i in items})}
        # ⚠ Règle révisée : les MARQUEURS
        #   ORAUX priment sur la classification. Un groupe UNKNOWN peut donc
        #   produire des items si le propriétaire a délimité le passage lui-même
        #   (« début de l'idée », « rajoute ça à la to-do »). Ce qui reste
        #   interdit : un groupe MEDIA/UNKNOWN qui produit des items SANS
        #   marqueur — c'est là qu'on ramasserait les idées d'un YouTubeur.
        sans_marqueur = [i for i in items
                         if (i["chemin"] or "") != "marqueur"]
        if classe in ("MEDIA", "UNKNOWN") and sans_marqueur:
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : classe {classe} mais {len(sans_marqueur)} "
                f"item(s) SANS marqueur — le garde-fou n'a pas joué")
        mauvais = {i["llm"] for i in items
                   if i["llm"] and i["llm"] != config.MODELE_LLM}
        if mauvais:
            r["ok"] = False
            r["problemes"].append(
                f"groupe {gid} : items produits par {mauvais} au lieu de "
                f"{config.MODELE_LLM}")
    return r


def controler_cases(con, debuts=None) -> dict:
    """CONTROLE DU MODELE « CASES DE 5 MINUTES ».

    Invariant unique, et c'est tout le modele : un segment appartient a la
    case de SON heure. La position dans le fichier de la case est l'heure
    reelle, donc un segment ne peut pas « deriver » : il est dans sa case
    par construction.

    Ce controle refuse :
      * un segment sans horaire ;
      * un segment dont le debut tombe hors de toute case existante (donc
        jamais transcrit, jamais attribue).

    Une phrase A CHEVAL sur deux cases est NORMALE, pas un defaut : Whisper
    peut rendre un segment de 30 s a 13:59:45 qui finit a 14:00:15. le propriétaire l'a
    dit explicitement : si ça coupe en deux fichiers, le LLM ira chercher dans
    (30 s) serait le signe d'un decoupage faux.
    """
    from pathlib import Path
    import blocs

    r = _res("cases")
    if debuts is None:
        try:
            debuts = [x["start_time"] for x in
                      con.execute("SELECT start_time FROM bloc ORDER BY start_time")]
        except sqlite3.OperationalError:
            debuts = []
    if not debuts:
        r["mesures"]["detail"] = "aucune case assemblee"
        return r

    connues = {round(float(d), 3) for d in debuts}
    t_min, t_max = min(connues), max(connues) + blocs.DUREE_CASE

    segs = con.execute(
        "SELECT segment_id, start_time, end_time, text FROM segment"
        " WHERE start_time IS NOT NULL AND TRIM(COALESCE(text,'')) <> ''"
        "   AND start_time >= ? AND start_time < ?", (t_min, t_max)).fetchall()

    par_case: dict[float, int] = {d: 0 for d in connues}
    sans_case = 0
    a_cheval = 0
    aberrants = 0
    sans_horaire = con.execute(
        "SELECT COUNT(*) FROM segment WHERE start_time IS NULL OR end_time IS NULL"
        " AND start_time >= ? AND start_time < ?", (t_min, t_max)).fetchone()[0]
    if sans_horaire:
        r["ok"] = False
        r["problemes"].append(f"{sans_horaire} segment(s) sans horaire")

    for s in segs:
        d = blocs.debut_case(float(s["start_time"]))
        if round(d, 3) not in connues:
            sans_case += 1
            continue
        par_case[d] += 1
        depasse = float(s["end_time"]) - (d + blocs.DUREE_CASE)
        if depasse > 0:
            a_cheval += 1
            if depasse > 30.0:
                aberrants += 1

    r["mesures"] = {
        "cases": len(connues),
        "segments": len(segs),
        "segments_par_case": {blocs.nom_case(d): n for d, n in sorted(par_case.items())},
        "segments_hors_case": sans_case,
        "segments_a_cheval": a_cheval,
        "segments_aberrants": aberrants,
    }
    if sans_case:
        r["ok"] = False
        r["problemes"].append(
            f"{sans_case} segment(s) dont l'heure ne tombe dans aucune case assemblée")
    if aberrants:
        r["ok"] = False
        r["problemes"].append(
            f"{aberrants} segment(s) dépassent leur case de plus de 30 s")
    r["faits"] = len(connues)
    return r


CONTROLES = {
    "groupement": controler_groupement,
    "transcription": controler_transcription,
    "voix": controler_voix,
    "alignement": controler_alignement,
    "classification": controler_classification,
    "markdown": controler_markdown,
    "extraction": controler_extraction,
    "cases": controler_cases,
}


def controler(con, etape: str, groupes=None) -> dict:
    if etape not in CONTROLES:
        return {"etape": etape, "ok": True, "faits": 0, "non_faits": 0,
                "problemes": [], "mesures": {},
                "detail": "pas de contrôle automatique pour cette étape"}
    return CONTROLES[etape](con, groupes)


def afficher(rapport: dict) -> None:
    n = rapport.get("faits", 0)
    v = rapport.get("non_faits", 0)
    etat = "OK" if rapport.get("ok") else "PROBLEME"
    print(f"  [{etat:7}] {rapport['etape']:14} "
          f"({n} groupe(s) vérifié(s), {v} pas encore fait(s))")
    for p in rapport.get("problemes", []):
        print(f"            - {p}")
