"""Schéma SQLite — la mémoire technique du pipeline (procédure §17).

Principes :
  - Markdown = sorties lisibles. SQLite = état technique (règle 17).
  - Toute information incertaine reste NULL ou 'UNKNOWN' et le pipeline
    continue (règle 3). Aucun état incertain ne doit bloquer.
  - Chaque résultat porte le modèle et la version qui l'a produit
    (règle 18 §18) : changer de modèle ne casse pas l'historique.
  - Idempotence par SHA-256 (règle 19 §19) : une étape rejouée ne
    duplique rien.

⚠ NOTE SQL — ne jamais mettre de commentaire `--` sur une LIGNE DE COLONNE.
SQLite exige `-- ` (tiret-tiret + espace) en début de commentaire ; si une
virgule précède le commentaire, la virgule disparaît dedans et la ligne
suivante se retrouve orpheline → "near ')': syntax error". Les commentaires
vivent donc uniquement sur leurs propres lignes, au-dessus des CREATE TABLE.

Tables par phase :
  source, session, chunk, artifact, segment, pipeline_step, speaker  PHASE 1
  speaker_embedding, turn                                           PHASES 2-5
  classification                                                    PHASE 7
"""
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "2.0.0"

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ===== SOURCE : ce qui produit le contenu (§2) =====
-- source_001 = micro téléphone, source_002 = YouTube, source_003 = film
-- kind : microphone | youtube | film | file | UNKNOWN (règle 3)
CREATE TABLE IF NOT EXISTS source (
    source_id    INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'UNKNOWN',
    label        TEXT,
    uri          TEXT,
    first_seen   TEXT NOT NULL,
    UNIQUE(kind, label, uri)
);

-- ===== SESSION : période d'utilisation continue (§2, §8) =====
-- On ne cherche PAS à tout deviner : session_id peut être NULL
-- tant que la continuité n'est pas établie (règle 3, règle 8).
-- note : contexte fourni par le propriétaire (§12)
CREATE TABLE IF NOT EXISTS session (
    session_id   INTEGER PRIMARY KEY,
    source_id    INTEGER REFERENCES source(source_id),
    started_at   TEXT,
    ended_at     TEXT,
    note         TEXT,
    created_at   TEXT NOT NULL
);

-- ===== CHUNK : unité technique, PAS une unité logique (règle 2) =====
-- start_time = position ABSOLUE dans la source (jamais relative au chunk)
-- start_time NULL quand le nom est illisible : c'est un INCERTAIN, pas
-- un 0. Un 0 afficherait un chunk placé au début de la session, ce qui
-- est faux et impossible à rattraper plus tard (règle 3).
-- chunk_index = numéro d'ordre porté par le nom de fichier
-- original_file + hash = traçabilité vers l'audio d'origine
-- note : drapeaux du nom de fichier (conflit Syncthing, copie --N,
--        nom illisible). On les garde même quand le chunk est valide,
--        sinon on perd la trace des conflits Syncthing (règle 3).
CREATE TABLE IF NOT EXISTS chunk (
    chunk_id       INTEGER PRIMARY KEY,
    session_id     INTEGER REFERENCES session(session_id),
    source_id      INTEGER REFERENCES source(source_id),
    recorded_at    TEXT NOT NULL,
    chunk_index    INTEGER,
    start_time     REAL,
    end_time       REAL,
    duration_sec   REAL,
    original_file  TEXT NOT NULL,
    hash           TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL DEFAULT 'pending',
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_recorded ON chunk(recorded_at);
CREATE INDEX IF NOT EXISTS idx_chunk_session ON chunk(session_id);

-- ===== ARTIFACT : un fichier produit par une étape =====
-- kind : transcript | diarization | classification | extraction | archive
-- path est RELATIF à la racine du projet, pour rester valide s'il bouge.
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id      INTEGER PRIMARY KEY,
    chunk_id         INTEGER NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    path             TEXT,
    hash             TEXT,
    model            TEXT,
    model_version    TEXT,
    pipeline_version TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(chunk_id, kind)
);

-- ===== SPEAKER : identité vocale PERSISTANTE (§3, §5) =====
-- L'ID ne change JAMAIS. Le nom humain est une info séparée et
-- modifiable (règle 4 : profil vocal ≠ nom, §4).
-- status : UNKNOWN | confirmed (UNKNOWN tant que le propriétaire n'a pas confirmé, règle 3)
-- Les embeddings multiples arrivent en PHASE 3 (§6).
CREATE TABLE IF NOT EXISTS speaker (
    speaker_id   INTEGER PRIMARY KEY,
    name         TEXT,
    status       TEXT NOT NULL DEFAULT 'UNKNOWN',
    created_at   TEXT NOT NULL,
    UNIQUE(name)
);

-- ===== SEGMENT : les timestamps fiables (PHASE 1) =====
-- start/end ABSOLUS dans la source (start_time + offset du chunk)
-- text vide = segment de locuteur sans parole attribuée (Phase 5)
-- speaker_id NULL = UNKNOWN (règle 3)
CREATE TABLE IF NOT EXISTS segment (
    segment_id    INTEGER PRIMARY KEY,
    chunk_id      INTEGER NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    start_time    REAL NOT NULL,
    end_time      REAL NOT NULL,
    text          TEXT NOT NULL DEFAULT '',
    speaker_id    INTEGER REFERENCES speaker(speaker_id),
    audio_kind    TEXT                     -- parole | musique
);
CREATE INDEX IF NOT EXISTS idx_segment_chunk ON segment(chunk_id, idx);

-- ===== PIPELINE_STEP : quelle étape est terminée (§17, §19) =====
-- C'est ce qui rend le pipeline REJOUABLE : changer de modèle
-- invalide uniquement les lignes de l'étape concernée, pas les autres.
-- step : vad | transcription | alignment | identification |
--        classification | extraction | archive
-- status : pending | done | failed | skipped
CREATE TABLE IF NOT EXISTS pipeline_step (
    step_id           INTEGER PRIMARY KEY,
    chunk_id          INTEGER NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    step              TEXT NOT NULL,
    status            TEXT NOT NULL,
    model             TEXT,
    model_version     TEXT,
    pipeline_version  TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    error             TEXT,
    UNIQUE(chunk_id, step)
);
CREATE INDEX IF NOT EXISTS idx_step_status ON pipeline_step(step, status);

-- ===== CHUNK_GROUP : le REGROUPEMENT PAR 5 =====
-- L'app découpe en 60 s. On prend 5 fichiers → 1 seul morceau avant de
-- transcrire : le RTF baisse nettement ET le
-- texte retrouve les phrases coupées entre deux chunks (règle 7 : fenêtre
-- de contexte aux frontières).
--
-- start_time = position ABSOLUE du premier chunk du groupe. C'est cet
-- offset qui sert à ramener chaque segment du groupe sur son chunk
-- d'origine : rien n'est perdu, la base des chunks ne change pas.
CREATE TABLE IF NOT EXISTS chunk_group (
    group_id     INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES session(session_id),
    taille       INTEGER NOT NULL,          -- 5 (config.GROUPE_TAILLE)
    n_chunks     INTEGER NOT NULL,          -- chunks réellement regroupés
    start_time   REAL,                      -- absolu, 1er chunk
    end_time     REAL,                      -- absolu, fin du dernier
    duration_sec REAL,                      -- durée totale du WAV groupé
    audio_path   TEXT,                      -- WAV temporaire (work/groupes)
    fingerprint  TEXT UNIQUE,               -- SHA-256 des noms, idempotent
    statut       TEXT NOT NULL DEFAULT 'pending',
    note         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_group_session ON chunk_group(session_id);

-- ===== GROUP_MEMBER : la VRAIE liste des membres d'un groupe =========
-- ⚠ RETOUR EN ARRIÈRE NECESSAIRE. La première version retrouvait les
-- membres par une fenêtre [start_time, end_time]. C'est FAUX sur le
-- en pratique : une session peut mêler des chunks de 60 s et de 300 s,
-- donc la fenêtre contient plus de chunks que le groupe n'en a. Un chunk en
-- trop dans la
-- fenêtre fausse l'offset de tous les suivants → chaque segment se
-- retrouve sur le mauvais chunk d'origine. C'est le genre d'erreur
-- SILENCIEUSE qui ne se voit qu'en écoutant le résultat final.
--
-- La bonne méthode est la seule fiable : on note les membres au moment
-- où on construit le groupe, dans l'ordre exact du WAV assemblé.
CREATE TABLE IF NOT EXISTS group_member (
    group_id     INTEGER NOT NULL REFERENCES chunk_group(group_id) ON DELETE CASCADE,
    chunk_id     INTEGER NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    rang         INTEGER NOT NULL,          -- 0..n-1, ordre du WAV
    offset_sec   REAL NOT NULL,             -- position dans le WAV groupé
    PRIMARY KEY (group_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_member_chunk ON group_member(chunk_id);

-- ===== TURN : un tour de parole, avec son profil (PHASES 2-5) =====
-- pyannote numérote ses locuteurs À CHAQUE FICHIER (SPEAKER_00,
-- SPEAKER_01...). Ce numéro n'est valable que pour le groupe où il a été
-- produit : c'est `label_local`. Le profil RÉEL est speaker_id, résolu
-- par l'identification (phase 3). Sans lui, la valeur est NULL = UNKNOWN
-- (règle 3), jamais un numéro inventé.
--
-- start/end ABSOLUS dans la source, comme segment (règle 2).
CREATE TABLE IF NOT EXISTS turn (
    turn_id      INTEGER PRIMARY KEY,
    group_id     INTEGER NOT NULL REFERENCES chunk_group(group_id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    start_time   REAL NOT NULL,
    end_time     REAL NOT NULL,
    label_local  TEXT,                      -- SPEAKER_00 du groupe
    speaker_id   INTEGER REFERENCES speaker(speaker_id),
    confiance    TEXT,                      -- MATCH|PROBABLE|AMBIGU|UNKNOWN
    UNIQUE(group_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_turn_group ON turn(group_id);

-- ===== SPEAKER_EMBEDDING : plusieurs empreintes par profil (§6) =====
-- Une seule empreinte ne suffit pas : téléphone, distance, bruit de fond
-- changent la voix. On garde jusqu'à N par profil, et on matche sur le
-- meilleur d'entre eux.
CREATE TABLE IF NOT EXISTS speaker_embedding (
    embedding_id INTEGER PRIMARY KEY,
    speaker_id   INTEGER NOT NULL REFERENCES speaker(speaker_id) ON DELETE CASCADE,
    group_id     INTEGER REFERENCES chunk_group(group_id) ON DELETE CASCADE,
    vecteur      BLOB NOT NULL,             -- float32 sérialisé
    dim          INTEGER NOT NULL,
    cosine_max   REAL,                      -- rempli après matching
    model        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emb_speaker ON speaker_embedding(speaker_id);

-- ===== CLASSIFICATION : PERSONNEL / MEDIA / BUSINESS (PHASE 7) =====
-- Le type de média vient du propriétaire (§12) ou reste UNKNOWN (règle 3).
CREATE TABLE IF NOT EXISTS classification (
    class_id      INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES chunk_group(group_id) ON DELETE CASCADE,
    classe        TEXT NOT NULL DEFAULT 'UNKNOWN',  -- PERSONNEL|MEDIA|BUSINESS|UNKNOWN
    signaux       TEXT,                      -- JSON : ce qui a décidé
    confiance     REAL,
    llm           TEXT,                      -- modèle qui a proposé, NULL si déterministe
    created_at    TEXT NOT NULL,
    UNIQUE(group_id)
);

-- ===== EXTRACTION : idées / TODO / notes produites par le LLM (PHASE 8) =====
CREATE TABLE IF NOT EXISTS extraction (
    extract_id  INTEGER PRIMARY KEY,
    group_id    INTEGER NOT NULL REFERENCES chunk_group(group_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,               -- IDEA | TODO | NOTE
    texte       TEXT NOT NULL,
    chemin      TEXT,                        -- fiche écrite, chemin relatif
    speaker_id  INTEGER REFERENCES speaker(speaker_id),
    llm         TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extract_group ON extraction(group_id);

-- ===== BLOC : une case de 5 minutes qui PORTE l'heure =====
-- Le son est rangé dans des cases de 5 minutes dont le NOM donne l'heure
-- exacte qu'elles contiennent (AAAA-MM-JJ_13h45.wav = 13:45:00 -> 13:50:00).
-- Conséquence : la position dans le fichier EST l'heure réelle, donc plus
-- aucune table d'offsets et plus aucune conversion à l'envers.
--
-- Cette table ne sert qu'à suivre l'avancement (quel fichier, quel statut).
-- La vérité du contenu est dans le NOM du fichier : si la base est perdue,
-- on la reconstruit en relisant les noms.
CREATE TABLE IF NOT EXISTS bloc (
    start_time   REAL PRIMARY KEY,           -- heure ronde (multiple de 300 s)
    path         TEXT,
    duree        REAL,
    n_chunks     INTEGER,
    statut       TEXT NOT NULL DEFAULT 'pending',  -- pending|assemblee|transcrite|voix|fini
    note         TEXT,
    created_at   TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Connexion + schéma. Idempotent (CREATE TABLE IF NOT EXISTS).

    Accepte un str : le chemin est normalisé ici plutôt que dans chaque
    appelant. Un appelant qui passe une chaîne obtenait AttributeError sur
    `db_path.parent`, ce qui est incomprehensible à l'endroit où ça casse.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=60)
    # `timeout=60` = busy_timeout de 60 s : sans lui, deux étapes lancées en
    # parallèle (ou le worker qui tourne pendant une reprise manuelle) se
    # tuent mutuellement avec « database is locked » — cas rencontré dès que
    # deux traitements se chevauchent.
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> list[str]:
    """Aligne une base déjà créée sur le schéma actuel.

    `CREATE TABLE IF NOT EXISTS` ne modifie PAS une table existante : sans
    cette étape, une base créée avant l'ajout d'une colonne garde l'ancien
    schéma et les INSERT échouent sur une colonne absente. C'est exactement
    ce qui est arrivé sur la base de production (colonne `note` manquante).

    On ne fait QUE des ajouts, jamais de suppression ni de réécriture : les
    données ne sont jamais touchées (règle 5).
    """
    changes: list[str] = []
    colonnes = {r["name"] for r in con.execute("PRAGMA table_info(chunk)")}
    if colonnes and "note" not in colonnes:
        con.execute("ALTER TABLE chunk ADD COLUMN note TEXT")
        changes.append("chunk.note ajouté")

    old = {r["name"]: r for r in con.execute("PRAGMA table_info(chunk)")}
    if old.get("start_time") and old["start_time"]["notnull"] == 1:
        # un nom illisible donne start_time NULL : la colonne NOT NULL
        # faisait échouer l'INSERT et le fichier disparaissait (règle 3)
        #
        # IMPORTANT : segment et pipeline_step référencent chunk avec
        # ON DELETE CASCADE. Sans désactiver les clés étrangères, le
        # DROP TABLE ci-dessous EFFACERAIT tous les transcripts déjà
        # transcrits. On les coupe donc pendant la migration, puis on
        # restaure — les données ne doivent jamais disparaître (règle 5).
        etait_active = con.execute("PRAGMA foreign_keys").fetchone()[0]
        con.execute("PRAGMA foreign_keys=OFF")
        try:
            con.execute("""
                CREATE TABLE chunk_new (
                    chunk_id       INTEGER PRIMARY KEY,
                    session_id     INTEGER REFERENCES session(session_id),
                    source_id      INTEGER REFERENCES source(source_id),
                    recorded_at    TEXT NOT NULL,
                    chunk_index    INTEGER,
                    start_time     REAL,
                    end_time       REAL,
                    duration_sec   REAL,
                    original_file  TEXT NOT NULL,
                    hash           TEXT NOT NULL UNIQUE,
                    status         TEXT NOT NULL DEFAULT 'pending',
                    note           TEXT,
                    created_at     TEXT NOT NULL
                )""")
            n_avant = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
            n_seg = con.execute("SELECT COUNT(*) FROM segment").fetchone()[0]
            con.execute("""
                INSERT INTO chunk_new
                SELECT chunk_id, session_id, source_id, recorded_at, chunk_index,
                       start_time, end_time, duration_sec, original_file, hash,
                       status, NULL, created_at FROM chunk""")
            con.execute("DROP TABLE chunk")
            con.execute("ALTER TABLE chunk_new RENAME TO chunk")
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunk_recorded"
                        " ON chunk(recorded_at)")
            n_apres = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
            n_seg_apres = con.execute("SELECT COUNT(*) FROM segment").fetchone()[0]
            if n_apres != n_avant or n_seg_apres != n_seg:
                raise RuntimeError(
                    f"migration a perdu des données : "
                    f"chunk {n_avant}->{n_apres}, segment {n_seg}->{n_seg_apres}")
            changes.append("chunk.start_time NOT NULL -> nullable")
        finally:
            if etait_active:
                con.execute("PRAGMA foreign_keys=ON")
    if changes:
        con.commit()
    return changes


def set_schema_version(con: sqlite3.Connection, version: str = SCHEMA_VERSION) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (version,))
    con.commit()
