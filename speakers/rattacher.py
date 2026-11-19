"""ÉTAPE 4b — RATTACHEMENT VOCAL : cette voix, on la connaît déjà ?

`attribuer_profils` crée un profil à chaque voix nouvelle d'un groupe.
Sur un petit lot de groupes, l'ancienne méthode créait plusieurs profils
par groupe, dont la majorité restait vide. C'est exactement le défaut de la
V1 (un profil à chaque intervention) que la procédure veut supprimer : une
même personne reconnaissable sort une nouvelle identité tous les 5 minutes.

Ce module fait le matching par empreinte vocale :
  1. on découpe le groupe en tours de parole ;
  2. on calcule une empreinte WeSpeaker ResNet34 (256 dim) par tour ;
  3. on compare à toutes les empreintes connues (cosine) ;
  4. au-dessus de config.SEUIL_MATCH → on rattache au profil existant ;
  5. en dessous → voix nouvelle, AVEC le contrôle anti-doublon :
     une voix ne crée un profil que si elle parle assez longtemps.

Deux garde-fous :
  - MIN_SECONDE_PAR_PROFIL vaut 8,0 s : sous ce seuil, une respiration ou
    un « euh » suffirait à fabriquer une identité ;
  - un profil créé ne reçoit le nom que si le propriétaire le confirme, sinon
    name reste NULL (règle 3).

⚠ Limite honnête : deux personnes qui parlent 4 minutes chacune dans
un groupe de 5 minutes ne seront pas distinguées par une empreinte de
4 s. Le matching est une aide, pas une preuve. Tant que le propriétaire n'a pas
confirmé un nom, status reste UNKNOWN.
"""
import sqlite3
from datetime import datetime, timezone

import config

MODELE_EMPREINTE = "microsoft/wespeaker-voxceleb-resnet34-LM"
DIM_EMPREINTE = 256
MIN_SECONDE_PAR_PROFIL = 8.0
MAX_EMPREINTES_PAR_PROFIL = 50

_EMBEDDER = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def charger():
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    from wespeaker import torch_model
    _EMBEDDER = torch_model
    return _EMBEDDER


def embedder_disponible() -> bool:
    try:
        return hasattr(charger(), "Model")
    except Exception:
        return False


def empreinte(audio_path: str, debut: float, duree: float) -> tuple:
    """Empreinte vocale d'un passage. Renvoie (vecteur float32, dim).

    L'audio est lu par `_lire` (16 kHz mono) puis passé à
    `speakers.voix.empreinte`, qui charge le même ResNet34 que
    pyannote — on n'utilise pas le paquet `wespeaker`, incompatible
    avec torchaudio 2.11.
    """
    try:
        from speakers import voix as _v
    except ImportError:
        import voix as _v
    wav = _lire(audio_path, debut, duree)
    if wav is None:
        return None, 0
    v = _v.empreinte(*wav)
    if v is None:
        return None, 0
    return v, int(v.shape[0])


def _lire(audio_path: str, debut: float, duree: float):
    """Lit un passage et le renvoie en mono 16 kHz. Renvoie (arr, sr).

    ⚠ DEUX pièges corrigés ici, tous deux donnaient None sans message :

    1. `debut` est un OFFSET DANS LE WAV, pas un timestamp absolu.
       Les tours portent des timestamps epoch (secondes depuis 1970) ; le WAV
       concaténé démarre à 0. Il faut retirer `_base_groupe`.

    2. soundfile attend des indices en ÉCHANTILLONS, pas des secondes.
       Passer 36.6 / 38.1 donne :
         TypeError: slice indices must be integers or None
       et le `except Exception` avalait l'erreur en silence.

    Le WAV est déjà en 16 kHz mono (vérifié), donc pas de resampling.
    """
    import numpy as np
    import soundfile as sf
    try:
        i = int(round(debut * 16000))        # secondes -> échantillons
        n = int(round(duree * 16000))
        data, sr = sf.read(audio_path, start=i, frames=n, dtype="float32")
        if data is None or data.size == 0:
            return None
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != 16000:
            # le corpus est en 16 kHz ; si un jour ce n'est plus vrai,
            # on le signale plutôt que de rééchantillonner en silence.
            return np.asarray(data, dtype="float32"), sr
        return np.asarray(data, dtype="float32"), sr
    except Exception:
        return None


def _cosine(a, b) -> float:
    import numpy as np
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def segments_reparables(con, group_id: int) -> list:
    """Tours assez longs pour être représentatifs (≥ 1,5 s).

    En dessous, l'empreinte est du bruit : elle change au mot près.
    """
    tours = con.execute(
        "SELECT start_time, end_time, label_local FROM turn"
        " WHERE group_id=? ORDER BY start_time", (group_id,)).fetchall()
    out = []
    for t in tours:
        d = t["end_time"] - t["start_time"]
        if t["label_local"] and d >= 1.5:
            out.append({"start": t["start_time"], "duree": d,
                        "label": t["label_local"]})
    return out


def embeddings_profils(con, speaker_id: int) -> list:
    """Vecteurs déjà connus pour un profil."""
    import numpy as np
    rows = con.execute(
        "SELECT vecteur, dim FROM speaker_embedding WHERE speaker_id=?",
        (speaker_id,)).fetchall()
    out = []
    for r in rows:
        try:
            v = np.frombuffer(r["vecteur"], dtype="float32")
            out.append(v[:r["dim"]])
        except Exception:
            continue
    return out


def meilleur_profil(con, vecteur) -> tuple:
    """(speaker_id, cosine) du profil le plus proche, ou (None, 0.0)."""
    meilleur, score = None, 0.0
    for r in con.execute("SELECT DISTINCT speaker_id FROM speaker_embedding"):
        sid = r["speaker_id"]
        for v in embeddings_profils(con, sid):
            c = _cosine(vecteur, v)
            if c > score:
                meilleur, score = sid, c
    return meilleur, score


def enregistrer_empreinte(con, speaker_id: int, vecteur, dim: int,
                          group_id: int, cosine=None) -> bool:
    n = con.execute("SELECT COUNT(*) c FROM speaker_embedding"
                    " WHERE speaker_id=?", (speaker_id,)).fetchone()["c"]
    if n >= MAX_EMPREINTES_PAR_PROFIL:
        return False
    con.execute(
        "INSERT INTO speaker_embedding"
        " (speaker_id, group_id, vecteur, dim, cosine_max, model, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (speaker_id, group_id, vecteur.tobytes(), dim, cosine,
         MODELE_EMPREINTE, now_iso()))
    con.commit()
    return True


def _base_groupe(con, group_id: int) -> float:
    """Origine temporelle du groupe : le start_time du 1er chunk.

    Les tours portent des timestamps ABSOLUS (epoch), mais le WAV
    concatené démarre à 0. Il faut donc retirer cette base avant tout
    accès au fichier, sinon l'indice calculé sort du WAV.
    """
    r = con.execute(
        "SELECT MIN(c.start_time) FROM chunk c"
        " JOIN group_member gm ON gm.chunk_id = c.chunk_id"
        " WHERE gm.group_id = ?", (group_id,)).fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


def rattacher_au_repertoire(con, group_id: int, audio_path: str,
                            trace: bool = True) -> dict:
    """Rattache les voix d'un groupe aux profils existants, ou les crée.

    C'est la fonction à appeler sur les groupes 2, 3, 4… : elle regarde
    si la voix est déjà connue et n'en crée une nouvelle que si elle ne
    ressemble à rien de connu ET qu'elle parle assez.
    """
    proprietaire = _profil0(con)
    base = _base_groupe(con, group_id)
    segs = segments_reparables(con, group_id)

    # 1. une empreinte de référence par voix locale.
    #    `s["start"]` est ABSOLU, `_lire` attend un OFFSET : on retire
    #    la base du groupe, sinon indice hors fichier et empreinte None.
    #
    #    ⚠ On prend le passage le PLUS LONG, pas le premier (constaté à
    #    l'usage). Un tour court et faible — chuchoté, loin du micro — donne
    #    une empreinte de référence fausse, et toutes les reconnaissances
    #    suivantes en héritent. Le tour le plus long est le plus parlé, donc
    #    le plus stable. (Limite honnête : un long passage peut aussi
    #    contenir du bruit ; l'idéal serait de trier sur l'énergie, à faire
    #    plus tard.)
    meilleur_seg = {}
    for s in segs:
        lab = s["label"]
        if lab not in meilleur_seg or s["duree"] > meilleur_seg[lab]["duree"]:
            meilleur_seg[lab] = s

    par_label = {}
    for lab, s in meilleur_seg.items():
        rel = max(0.0, s["start"] - base)
        v, d = empreinte(audio_path, rel, min(s["duree"], 6.0))
        if v is not None:
            par_label[lab] = (v, d)
    if not par_label:
        return {"groupe": group_id, "base": base, "voix": {},
                "message": "aucune empreinte calculable"}

    # 2. matching contre le répertoire, du plus proche au plus lointain
    resultat = {}
    restes = []
    for label, (v, d) in par_label.items():
        sid, score = meilleur_profil(con, v)
        if sid is not None and score >= config.SEUIL_MATCH:
            resultat[label] = {"profil": sid, "cosine": round(score, 3),
                               "statut": "MATCH"}
            enregistrer_empreinte(con, sid, v, d, group_id, score)
        else:
            restes.append((label, v, d, score, sid))

    # 3. ce qui reste : soit le propriétaire (profil 0), soit une voix vraiment nouvelle
    par_secondes = _secondes_par_label(con, group_id)

    # ⚠ BUG CORRIGÉ : la voix du propriétaire n'était pas reconnue comme telle.
    #   Le code calculait `proprietaire = _profil0(con)` puis ne s'en servait QUE
    #   pour la trace. Résultat : au tout premier groupe, le répertoire est
    #   vide, aucune correspondance n'est trouvée, et la voix dominante
    #   partait en « prochain profil » (1) au lieu d'être celle du
    #   propriétaire. Il n'était donc JAMAIS enrôlé, n'était reconnu nulle
    #   part, et chaque groupe refabriquait un profil — la dérive de la V1
    #   qu'on veut justement supprimer.
    #
    #   Règle : le profil 0 est celui du propriétaire.
    #   Tant qu'il n'a AUCUNE empreinte, la voix dominante du groupe (celle du
    #   téléphone) est la sienne : on la lui attribue et on l'enrôle comme
    #   référence. Les empreintes suivantes viendront des autres groupes, et
    #   les voix des autres personnes se rattacheront (ou pas) à elles.
    if restes and not embeddings_profils(con, proprietaire):
        label, v, d, score, candidat = max(
            restes, key=lambda z: par_secondes.get(z[0], 0))
        if par_secondes.get(label, 0) >= MIN_SECONDE_PAR_PROFIL:
            enregistrer_empreinte(con, proprietaire, v, d, group_id)
            resultat[label] = {"profil": proprietaire, "cosine": None,
                               "statut": "PROPRIETAIRE_ENROLE"}
            restes = [z for z in restes if z[0] != label]

    for label, v, d, score, candidat in sorted(
            restes, key=lambda z: -par_secondes.get(z[0], 0)):
        sec = par_secondes.get(label, 0)
        if sec >= MIN_SECONDE_PAR_PROFIL:
            sid = prochain_profil(con)
            con.execute("INSERT INTO speaker(speaker_id, name, status,"
                        " created_at) VALUES(?,?,?,?)",
                        (sid, None, "UNKNOWN", now_iso()))
            enregistrer_empreinte(con, sid, v, d, group_id)
            resultat[label] = {"profil": sid, "cosine": None,
                               "statut": "NOUVEAU"}
        else:
            # ⚠ On NE rattache PAS au propriétaire. Attribuer une voix non
            #   reconnue à quelqu'un, c'est deviner — la règle 3.
            #   Elle reste sans profil (speaker_id NULL, confiance
            #   UNKNOWN) et le tour ne portera qu'un nom vide en Markdown.
            #   Le rattacher au profil 0 « pour ne pas fabriquer d'identité »
            #   serait pire : ça fabrique une FAUSSE identité sur une
            #   personne réelle.
            resultat[label] = {"profil": None,
                               "cosine": round(score, 3) if score else None,
                               "statut": "PEU_DE_PARLE",
                               "message": f"{sec:.1f}s < {MIN_SECONDE_PAR_PROFIL}s"
                                          " → voix trop brève, non attribuée"
                                          " (règle 3 : on ne devine pas)"}
    # 4. ÉCRIRE le profil réel sur les tours. Sans cette étape, les
    #    correspondances sont calculées mais jamais écrites : le matching
    #    fonctionne, les tours restent à speaker_id NULL et rien ne remonte
    #    en aval. C'est `identify.associer_locaux` qui le faisait avant ; on le
    #    reproduit ici pour que ce module soit autonome.
    #
    #    `confiance` est un ENUM en base (db.py: MATCH|PROBABLE|AMBIGU|
    #    UNKNOWN) : on ne peut pas y écrire nos statuts internes
    #    NOUVEAU / PEU_DE_PARLE, il faut les traduire.
    _CONF = {"MATCH": "MATCH", "PROBABLE": "PROBABLE",
             "PROPRIETAIRE_ENROLE": "PROBABLE",
             "NOUVEAU": "UNKNOWN", "PEU_DE_PARLE": "UNKNOWN"}
    correspondance = {lab: v["profil"] for lab, v in resultat.items()
                       if v.get("profil") is not None}
    n_ecrits = 0
    for label, spid in correspondance.items():
        conf = _CONF.get(resultat[label].get("statut"), "UNKNOWN")
        cur = con.execute(
            "UPDATE turn SET speaker_id=?, confiance=? WHERE group_id=?"
            " AND label_local=?",
            (spid, conf, group_id, label))
        n_ecrits += cur.rowcount or 0
    con.commit()

    if trace:
        _trace(group_id, resultat, par_secondes, proprietaire)
    return {"groupe": group_id, "profil_proprietaire": proprietaire, "voix": resultat,
            "tours_attribues": n_ecrits,
            "secondes_par_voix": par_secondes}


def _profil0(con) -> int:
    con.execute("INSERT OR IGNORE INTO speaker(speaker_id, name, status,"
                " created_at) VALUES(?,?,?,?)",
                (config.PROFIL_PROPRIETAIRE, "le propriétaire", "confirmed", now_iso()))
    return config.PROFIL_PROPRIETAIRE


def prochain_profil(con) -> int:
    existant = {r["speaker_id"] for r in
                con.execute("SELECT speaker_id FROM speaker")}
    c = config.PREMIER_PROFIL_AUTRE
    while c in existant:
        c += 1
    return c


def _secondes_par_label(con, group_id: int) -> dict:
    totaux = {}
    for t in con.execute("SELECT start_time, end_time, label_local FROM turn"
                         " WHERE group_id=?", (group_id,)):
        if t["label_local"]:
            totaux[t["label_local"]] = round(
                totaux.get(t["label_local"], 0.0)
                + t["end_time"] - t["start_time"], 2)
    return totaux


def _trace(group_id, resultat, totaux, proprietaire) -> None:
    config.WORK_TRACE.mkdir(parents=True, exist_ok=True)
    p = config.WORK_TRACE / f"groupe_{group_id:04d}_rattachement.md"
    l = [f"# Rattachement vocal — groupe {group_id}", "",
         f"- **Profil {proprietaire}** = le propriétaire", "",
         "| voix locale | secondes | profil | cosine | statut |",
         "|-----------|----------|--------|--------|--------|"]
    for label, r in sorted(resultat.items()):
        l.append(f"| {label} | {totaux.get(label, 0)} | {r['profil']} | "
                 f"{r.get('cosine') or '—'} | {r['statut']} |")
    l += ["", "> Un profil créé reste sans nom tant que le propriétaire ne l'a pas "
          "confirmé.", ""]
    p.write_text("\n".join(l), encoding="utf-8")