"""ÉTAPE 1 — REGROUPEMENT PAR 5.

Cinq chunks consécutifs d'une même session sont concaténés en un seul morceau
de travail.

Pourquoi ce découpage : cinq chunks d'une même session partagent un contexte
continu, et le morceau obtenu est assez long pour que la diarisation et le
LLM travaillent sur des phrases entières plutôt que sur des fragments. Il
reste assez court pour tenir en mémoire et pour être rejoué seul.
"""
import hashlib
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import config
import db

# ffmpeg ne lit pas n'importe quoi : l'audio du téléphone est du WAV, le
# V1 a aussi de l'OGG. On normalise en WAV 16 kHz mono, ce que Whisper et
# pyannote attendent tous les deux. Un seul décodage pour les deux étapes.
FMT_GROUPE = ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]

TIMEOUT_FFMPEG = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(chunk_ids: list[int]) -> str:
    """Identité d'un groupe = la liste de ses chunk_id.

    Sert d'UNIQUE : rejouer la construction ne crée pas de doublon
    (règle 19). Volontairement basé sur les chunk_id et non sur le
    contenu audio : deux groupes différents doivent rester distinguables
    même si le WAV concaténé sortait identique (règle 3).
    """
    h = hashlib.sha256()
    for cid in chunk_ids:
        h.update(str(cid).encode())
        h.update(b"|")
    return h.hexdigest()


def _doublons_syncthing(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Retire les copies '--N' d'un même enregistrement.

    ⚠ BUG CONSTATÉ. Le même enregistrement existe
    deux fois en base : `AAAA-MM-JJ_HH-MM-SS_0001--1.ogg` et
    `AAAA-MM-JJ_HH-MM-SS_0001.wav`. Le `--1` est la copie Syncthing
    (cf. ingestion/naming.py, RE_DUPLICATE). Les deux ont le MÊME
    start_time et la MÊME durée.

    Conséquence si on ne filtre pas : un groupe de 5 assemble le même
    enregistrement deux fois, donc 300 s de WAV pour 150 s de vraie
    parole, et les segments qui suivent le doublon retombent sur le mauvais
    chunk d'origine — des chunks se retrouvent à zéro segment, d'autres en
    surnombre.

    On garde le fichier SANS le suffixe `--N` (l'original), et en cas
    d'égalité on garde le plus grand (le WAV non compressé n'est pas
    tronqué, l'OGG l'est parfois). La copie reste en base et en place :
    on ne supprime rien (règle 5).
    """
    meilleur: dict[str, sqlite3.Row] = {}
    for c in rows:
        cle = f"{c['start_time']:.3f}"
        actuel = meilleur.get(cle)
        if actuel is None:
            meilleur[cle] = c
            continue
        nom_actuel = Path(actuel["original_file"]).stem
        nom_nouveau = Path(c["original_file"]).stem
        # sans '--N' = original : il gagne
        if ("--" in nom_actuel) and ("--" not in nom_nouveau):
            meilleur[cle] = c
        elif ("--" in nom_actuel) == ("--" in nom_nouveau):
            # même statut : on garde le plus gros (le moins tronqué)
            taille_actuel = Path(actuel["original_file"]).stat().st_size \
                if Path(actuel["original_file"]).is_file() else 0
            taille_nouveau = Path(c["original_file"]).stat().st_size \
                if Path(c["original_file"]).is_file() else 0
            if taille_nouveau > taille_actuel:
                meilleur[cle] = c
    return sorted(meilleur.values(), key=lambda c: (c["start_time"], c["chunk_id"]))


def chunks_par_session(con: sqlite3.Connection, session_id: int | None = None,
                       limite_chunks: int | None = None,
                       sans_doublons: bool = True) -> list[sqlite3.Row]:
    """Chunks d'une session, dans l'ordre chronologique.

    Un chunk sans position (start_time NULL) ne peut pas être placé dans
    un groupe sans inventer son rang — donc il est exclu (règle 3 : il
    reste en base, il n'est simplement pas dans un groupe).

    `sans_doublons=True` (défaut) retire les copies Syncthing '--N' : sans
    ça, un groupe contient deux fois le même enregistrement et tout le
    reste du pipeline est décalé (voir _doublons_syncthing).
    """
    q = ("SELECT * FROM chunk WHERE start_time IS NOT NULL")
    params: list = []
    if session_id is not None:
        q += " AND session_id=?"
        params.append(session_id)
    q += " ORDER BY session_id, start_time, chunk_id"
    rows = con.execute(q, params).fetchall()

    # ⚠ La déduplication doit passer AVANT le LIMIT. Filtrer après ne
    # servirait à rien : la limite a déjà compté les doublons, donc on
    # obtenait moins de chunks que demandé — c'est ce qui produisait des
    # groupes de 3 quand on en demandait 5.
    if sans_doublons:
        rows = _doublons_syncthing(rows)
    if limite_chunks:
        rows = rows[:limite_chunks]
    return rows


def _racines_audio() -> list[Path]:
    """Tous les dossiers où un WAV ingéré peut vivre, où qu'ait bougé le projet."""
    rac = Path(__file__).resolve().parent.parent
    cand = [rac / "audio", rac / "audio" / "original", rac / "audio" / "archive",
            rac / "chunks", rac / "syncthing-inbox", rac / "work" / "groupes",
            Path(config.V1_AUDIO), Path(config.V1_DONE), Path(config.SYNC_INBOX)]
    return [c for c in cand if c.is_dir()]


def _resoudre_audio(con: sqlite3.Connection, chunk) -> Path | None:
    """Le WAV d'un chunk, retrouvé par son NOM si le chemin en base est périmé.

    `chunk.original_file` peut pointer un chemin obsolète (projet déplacé,
    base recopiée). Quand aucun de ces chemins n'existe, `_audio_manquant`
    écarte tous les chunks et le groupement ne produit plus rien. Le nom
    encode l'horodatage, il survit au déplacement — on le cherche, et on
    corrige le pointeur en base au passage.
    """
    direct = Path(chunk["original_file"] or "")
    if direct.is_file():
        return direct
    nom = direct.name
    if not nom:
        return None
    for r in _racines_audio():
        for f in r.rglob(nom):
            con.execute("UPDATE chunk SET original_file=? WHERE chunk_id=?",
                        (str(f), chunk["chunk_id"]))
            con.commit()
            return f
    return None


def _audio_manquant(con: sqlite3.Connection, chunks: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Écarte les chunks dont le WAV a disparu (règle 3 : on continue)."""
    surs = []
    for c in chunks:
        if _resoudre_audio(con, c) is not None:
            surs.append(c)
    return surs


def concatener(chunks: list[sqlite3.Row], destination: Path) -> dict:
    """Concatène N chunks en UN seul WAV et renvoie ses métadonnées.

    Le filtre `concat` de ffmpeg est utilisé et non la simple juxtaposition
    de `-i` : avec plusieurs `-i` et une seule sortie, ffmpeg ne garde que
    la PREMIÈRE piste et ignore les suivantes silencieusement. C'est
    l'erreur à éviter : un « groupe de 5 » qui ne contient que le premier
    chunk, sans lever la moindre erreur.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    entrees: list[str] = []
    for c in chunks:
        entrees += ["-i", c["original_file"]]

    n = len(chunks)
    filtre = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"

    cmd = (["ffmpeg", "-y", "-v", "error", "-nostdin"]
           + entrees
           + ["-filter_complex", filtre, "-map", "[out]"]
           + FMT_GROUPE
           + [str(destination)])

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_FFMPEG)
    if r.returncode != 0 or not destination.is_file():
        return {"ok": False,
                "erreur": (r.stderr or "ffmpeg n'a rien produit").strip()[:400]}

    duree = duree_wav(destination)
    return {"ok": True, "duree": duree, "octets": destination.stat().st_size}


def duree_wav(path: Path) -> float | None:
    """Durée exacte par l'en-tête RIFF. None si illisible (règle 3).

    On ne se fie PAS à la somme des durées des chunks : un enregistrement
    présent en .ogg ET en .wav compte deux fois (constaté à l'usage).
    La durée du fichier réellement assemblé est la seule vérité.
    """
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except Exception:
        return None


def nom_groupe(session_id: int | None, start_time: float | None,
               n: int, group_id: int | None = None) -> str:
    """Nom lisible et triable : groupe + session + heure de début + taille.

    Le NUMÉRO DE GROUPE est en tête. Sans lui
    on ne pouvait pas relier un fichier à son groupe : `s2_…` désigne la
    SESSION 2, qui n'a aucun rapport avec le numéro de groupe (groupe 7,
    groupe 42…). La table `chunk_group` est le seul lien, et elle n'est pas
    lisible à l'œil. On garde la fin du nom d'origine pour rester lisible
    même si la base est perdue.
    """
    if start_time is None:
        horodatage = "sans-heure"
    else:
        horodatage = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d_%H-%M-%S")
    prefixe = f"g{group_id}_" if group_id is not None else ""
    return f"{prefixe}s{session_id or 0}_{horodatage}_{n}ch.wav"


def construire_groupe(con: sqlite3.Connection, chunks: list[sqlite3.Row],
                      work_dir: Path | None = None,
                      avec_audio: bool = True) -> dict:
    """Construit UN groupe à partir de N chunks et l'enregistre.

    Idempotent (règle 19) : si le fingerprint existe déjà, on le renvoie
    tel quel au lieu d'en créer un second.
    """
    chunks = [c for c in chunks if _resoudre_audio(con, c) is not None]
    if chunks:
        # Les lignes lues avant la résolution portent encore l'ancien chemin
        # (`sqlite3.Row` est immuable). On les relit pour que `concatener`
        # reçoive des chemins qui existent vraiment.
        ids = [c["chunk_id"] for c in chunks]
        chunks = con.execute(
            f"SELECT * FROM chunk WHERE chunk_id IN ({','.join('?' * len(ids))})"
            " ORDER BY start_time, chunk_id", ids).fetchall()
    if not chunks:
        return {"statut": "aucun_audio", "detail": "aucun WAV présent"}

    # Garde-fou n°1 : pas deux chunks au même instant dans un groupe.
    # Deux chunks de même start_time = deux copies du même enregistrement
    # (voir _doublons_syncthing). Assembler les deux donne un WAV deux fois
    # trop long et décale tous les segments qui suivent.
    instants = [round(float(c["start_time"]), 3) for c in chunks]
    if len(set(instants)) != len(instants):
        doublons = {i for i in instants if instants.count(i) > 1}
        return {"statut": "doublon_syncthing",
                "detail": f"chunks au même start_time : {sorted(doublons)}"}

    # Garde-fou n°2 : une seule session par groupe. Un groupe qui mélange
    # deux sessions produit un texte sans sens et envoie tout le monde dans
    # le même profil. Cas typique : un groupe à cheval sur deux sessions
    # séparées par une vingtaine de minutes de silence.
    if len({c["session_id"] for c in chunks}) != 1:
        return {"statut": "sessions_melangees",
                "detail": f"chunks de sessions "
                          f"{sorted({c['session_id'] for c in chunks})}"}

    ids = [c["chunk_id"] for c in chunks]
    fp = fingerprint(ids)

    deja = con.execute(
        "SELECT * FROM chunk_group WHERE fingerprint=?", (fp,)).fetchone()
    if deja is not None:
        return {"statut": "déjà_fait", "group_id": deja["group_id"]}

    ordre = sorted(chunks, key=lambda c: (c["start_time"], c["chunk_id"]))
    start = float(ordre[0]["start_time"])
    fin = max((float(c["end_time"]) for c in ordre
               if c["end_time"] is not None), default=start)
    session_id = ordre[0]["session_id"]

    work_dir = work_dir or config.WORK_GROUPES

    # On réserve la ligne AVANT d'assembler : le numéro de groupe (lastrowid)
    # n'existe qu'après l'INSERT, et c'est lui qui donne son nom au fichier
    # (g<num>_…). Insérer d'abord évite de renommer après coup.
    cur = con.execute(
        "INSERT INTO chunk_group(session_id, taille, n_chunks, start_time,"
        " end_time, duration_sec, audio_path, fingerprint, statut, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (session_id, config.GROUPE_TAILLE, len(ordre), start, fin, None,
         None, fp, "pending", now_iso()))
    group_id = cur.lastrowid

    audio_path = work_dir / nom_groupe(session_id, start, len(ordre), group_id)

    duree = None
    if avec_audio:
        r = concatener(ordre, audio_path)
        if not r.get("ok"):
            # on n'enregistre PAS un groupe sans audio : il serait marked
            # done alors qu'il n'a jamais été assemblé (règle 3)
            con.rollback()
            return {"statut": "ffmpeg_échoué", "detail": r.get("erreur")}
        duree = r["duree"]
        con.execute(
            "UPDATE chunk_group SET audio_path=?, duration_sec=?"
            " WHERE group_id=?", (str(audio_path), duree, group_id))

    # Les membres sont notés ICI, dans l'ordre du WAV assemblé, avec
    # l'offset de chacun. C'est la seule source de vérité (voir le
    # commentaire de group_member dans db.py : la fenêtre temporelle
    # ment sur l'ordre réel des chunks).
    offset = 0.0
    for rang, c in enumerate(ordre):
        con.execute(
            "INSERT OR REPLACE INTO group_member(group_id, chunk_id, rang,"
            " offset_sec) VALUES(?,?,?,?)",
            (group_id, c["chunk_id"], rang, round(offset, 3)))
        offset += float(c["duration_sec"] or 0.0)

    con.commit()
    return {"statut": "ok", "group_id": group_id, "n_chunks": len(ordre),
            "duree": duree, "audio": str(audio_path) if avec_audio else None,
            "start_time": start}


def construire_tout(con: sqlite3.Connection, n_groupes: int | None = None,
                    par_session: int | None = None,
                    avec_audio: bool = True,
                    progress=None) -> dict:
    """Construit les groupes de toute la base, session par session.

    `n_groupes` limite le nombre de groupes produits (pour tester vite).
    `par_session` limite le nombre de chunks pris par session.

    ⚠ Le regroupement ne traverse JAMAIS deux sessions. C'est la règle du
    module. En pratique, les 5 chunks d'un même enregistrement peuvent être
    étiquetés session N pour les premiers, puis session N+1 pour les
    suivants, quand le téléphone s'est arrêté au milieu. Un groupe qui
    mélange les deux donnerait un texte sans sens et enverrait tout le monde
    dans le même profil — un groupe peut ainsi contenir un membre venu de la
    session suivante.
    Donc on itère session par session, et dans chaque session on ne prend
    que des groupes de 5 entièrement inclus dans cette session.
    """
    sessions = con.execute(
        "SELECT session_id FROM session WHERE session_id IS NOT NULL"
        " ORDER BY session_id").fetchall()
    faits: list[dict] = []
    for s in sessions:
        sid = s["session_id"]
        chunks = chunks_par_session(con, sid, par_session)
        chunks = _audio_manquant(con, chunks)
        # pas de mélange entre sessions : on groupe dans l'ordre
        for i in range(0, len(chunks) - config.GROUPE_TAILLE + 1,
                       config.GROUPE_TAILLE):
            if n_groupes is not None and len(faits) >= n_groupes:
                break
            lot = chunks[i:i + config.GROUPE_TAILLE]
            # garde-fou : les 5 chunks doivent être de la MÊME session
            if len({c["session_id"] for c in lot}) != 1:
                continue
            r = construire_groupe(con, lot, avec_audio=avec_audio)
            faits.append(r)
            if progress:
                progress(len(faits), r)
        # Le reliquat de session (1 à 4 chunks) n'a longtemps été traité par
        # personne : `range(0, n - TAILLE + 1)` s'arrête avant la fin, donc
        # les derniers chunks d'un enregistrement restaient hors groupe et
        # n'étaient jamais transcrits — des heures d'audio dans ce cas. Un
        # groupe de 3 chunks vaut mieux qu'aucun : `construire_groupe`
        # accepte n'importe quelle taille.
        reste = len(chunks) % config.GROUPE_TAILLE
        if reste and len(chunks) >= reste and not (
                n_groupes is not None and len(faits) >= n_groupes):
            lot = chunks[-reste:]
            if len({c["session_id"] for c in lot}) == 1:
                r = construire_groupe(con, lot, avec_audio=avec_audio)
                faits.append(r)
                if progress:
                    progress(len(faits), r)
        if n_groupes is not None and len(faits) >= n_groupes:
            break

    compte: dict = {}
    for r in faits:
        compte[r["statut"]] = compte.get(r["statut"], 0) + 1
    return compte


def chunks_du_groupe(con: sqlite3.Connection, group_id: int) -> list[sqlite3.Row]:
    """Les chunks d'un groupe, dans l'ordre EXACT du WAV assemblé.

    On lit `group_member`, pas une fenêtre temporelle. Voir le commentaire
    de la table dans db.py : la fenêtre [start, end] attrape 8 chunks sur
    une session qui mélange des chunks de 60 s et de 300 s, alors que le
    groupe n'en contient que 5. Un chunk de trop fausse l'offset de tous
    les suivants, donc chaque segment atterrit sur le mauvais chunk
    d'origine — une erreur silencieuse, invisible jusqu'à l'écoute.
    """
    return con.execute(
        "SELECT c.*, m.rang, m.offset_sec FROM group_member m"
        " JOIN chunk c ON c.chunk_id = m.chunk_id"
        " WHERE m.group_id = ? ORDER BY m.rang", (group_id,)).fetchall()


def verifier_membres(con: sqlite3.Connection, group_id: int) -> dict:
    """Contrôle qu'un groupe contient bien EXACTEMENT ses chunks.

    Trois vérifications, parce que chacune a déjà échoué seule :
      1. le nombre de membres est bien 5 (la fenêtre en attrapait 8) ;
      2. la somme des durées correspond au WAV assemblé à moins d'1 s
         (sinon les offsets sont faux) ;
      3. les offsets sont croissants et le dernier arrive exactement à la
         durée totale (détecte un chunk manquant en fin de groupe).
    """
    g = con.execute("SELECT * FROM chunk_group WHERE group_id=?",
                    (group_id,)).fetchone()
    if g is None:
        return {"ok": False, "detail": "groupe inexistant"}

    membres = chunks_du_groupe(con, group_id)
    somme = round(sum(float(m["duration_sec"] or 0.0) for m in membres), 3)
    duree = float(g["duration_sec"] or 0.0)
    ecart = abs(somme - duree)

    offsets = [float(m["offset_sec"]) for m in membres]
    croissants = all(offsets[i] <= offsets[i + 1]
                     for i in range(len(offsets) - 1))
    fin = (offsets[-1] + float(membres[-1]["duration_sec"] or 0.0)) if membres else 0.0
    ecart_fin = abs(fin - duree)

    return {
        "group_id": group_id,
        "n_membres": len(membres),
        "n_attendu": g["n_chunks"],
        "taille": g["taille"],
        "somme_durees": somme,
        "duree_wav": duree,
        "ecart_sec": round(ecart, 3),
        "offsets_croissants": croissants,
        "ecart_fin_sec": round(ecart_fin, 3),
        "ok": (len(membres) == g["n_chunks"]
               and ecart < 1.0 and croissants and ecart_fin < 1.0),
    }


def offset_dans_groupe(group: list[sqlite3.Row], chunk: sqlite3.Row) -> float:
    """Position d'un chunk DANS le WAV du groupe, en secondes.

    Lue dans `group_member.offset_sec`, pas recalculée : la valeur notée à
    la construction est celle qui a servi à assembler le WAV, donc c'est
    la seule qui garantit l'alignement.
    """
    for c in group:
        if c["chunk_id"] == chunk["chunk_id"]:
            return float(c["offset_sec"])
    return 0.0


def position_absolue(group: list[sqlite3.Row], t_groupe: float) -> float:
    """Temps dans le groupe → temps ABSOLU dans la source (règle 2)."""
    if not group:
        return round(t_groupe, 3)
    return round(float(group[0]["start_time"]) + t_groupe, 3)


def chunk_de_temps(group: list[sqlite3.Row], t_absolu: float) -> sqlite3.Row | None:
    """Le chunk qui contient ce temps absolu. None aux interstices.

    On ne force jamais : un segment qui tombe dans le trou entre deux
    chunks est rattaché au chunk le plus proche, jamais à un chunk
    arbitraire (règle 3).
    """
    meilleur = None
    ecart = None
    for c in group:
        s = float(c["start_time"])
        e = float(c["end_time"]) if c["end_time"] is not None else s + 60.0
        if s <= t_absolu <= e:
            return c
        d = min(abs(t_absolu - s), abs(t_absolu - e))
        if ecart is None or d < ecart:
            ecart, meilleur = d, c
    return meilleur


def nettoyer_audio(work_dir: Path | None = None) -> int:
    """Supprime les WAV de groupe. Les originaux ne bougent JAMAIS.

    Appelée seulement quand le pipeline a produit ses résultats : le WAV
    groupé est reproductible, le garder ne sert qu'à remplir le disque
    (5 × la taille d'un chunk).
    """
    work_dir = work_dir or config.WORK_GROUPES
    if not work_dir.is_dir():
        return 0
    n = 0
    for p in work_dir.glob("*.wav"):
        try:
            p.unlink()
            n += 1
        except OSError:
            continue
    return n


def copier_vers_poubelle(con: sqlite3.Connection, group_ids: list[int] | None = None,
                          poubelle: Path | None = None) -> dict:
    """Phase 10 — copie les audios traités dans la poubelle.

    COPIE, jamais déplacement : les originaux doivent rester pour pouvoir
    les WAV d'origine restent en place (règle 5).

    On ne copie que ce qui a VRAIMENT été traité (transcrit + diarisé) :
    copier des dizaines de Go entiers prendrait une place inutile.
    """
    poubelle = poubelle or config.POUBELLE
    q = ("SELECT g.* FROM chunk_group g WHERE g.audio_path IS NOT NULL")
    params: list = []
    if group_ids:
        q += f" AND g.group_id IN ({','.join('?' * len(group_ids))})"
        params += list(group_ids)
    q += " ORDER BY g.group_id"

    dest_dir = poubelle / f"{config.POUBELLE_PREFIXE}-{datetime.now():%Y-%m-%d}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    ko = 0
    for g in con.execute(q, params):
        src = Path(g["audio_path"] or "")
        if not src.is_file():
            ko += 1
            continue
        try:
            shutil.copy2(src, dest_dir / src.name)
            con.execute(
                "UPDATE chunk_group SET note = COALESCE(note,'') || ?"
                " WHERE group_id=?",
                (f" | audio copié en poubelle {dest_dir.name}", g["group_id"]))
            n += 1
        except OSError:
            ko += 1
    con.commit()
    return {"copies": n, "erreurs": ko, "dossier": str(dest_dir)}
