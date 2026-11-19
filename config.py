"""Chemins V2 — dérivés de l'emplacement du fichier.

Jamais de chemin codé en dur : tous les scripts à chemin fixe cassent
silencieusement dès que le projet est déplacé.
"""
import os
from pathlib import Path

V2_DIR = Path(__file__).resolve().parent      # racine du projet (code remonté)
PROJECT_DIR = V2_DIR                          # racine du projet
PIPELINE_DIR = PROJECT_DIR / "pipeline"   # V1, lecture seule pour la migration

# §22 STRUCTURE LOGIQUE CIBLE — à la lettre
INGESTION = V2_DIR / "ingestion"
AUDIO = V2_DIR / "audio"
AUDIO_ORIGINAL = AUDIO / "original"
AUDIO_ARCHIVE = AUDIO / "archive"
SESSIONS = V2_DIR / "sessions"
CHUNKS = V2_DIR / "chunks"
TRANSCRIPTS = V2_DIR / "transcripts"
SPEAKERS = V2_DIR / "speakers"
DIARIZATION = V2_DIR / "diarization"
CLASSIFICATION = V2_DIR / "classification"
EXTRACTION = V2_DIR / "extraction"
EXTRACTION_IDEAS = EXTRACTION / "ideas"
EXTRACTION_TODO = EXTRACTION / "todo"
EXTRACTION_NOTES = EXTRACTION / "notes"
DATABASE = V2_DIR / "database"
DB_PATH = DATABASE / "memorybrain.sqlite"
# Sauvegardes de la base avant migration : hors Git (.gitignore), jamais
# dans la corbeille — une sauvegarde n'est pas un déchet, c'est un retour
# arrière possible.
DATABASE_BACKUP = DATABASE / "backups"
INTERFACE = V2_DIR / "interface"

ALL_DIRS = [
    INGESTION, AUDIO_ORIGINAL, AUDIO_ARCHIVE, SESSIONS, CHUNKS,
    TRANSCRIPTS, SPEAKERS, DIARIZATION, CLASSIFICATION,
    EXTRACTION_IDEAS, EXTRACTION_TODO, EXTRACTION_NOTES, DATABASE, INTERFACE,
]

# Sources audio à ingesting. V1 = banque de tests (§24) + migration.
# Le téléphone envoie dans syncthing-inbox (Phase 0, ne pas toucher).
V1_DONE = PIPELINE_DIR / "done"
V1_AUDIO = PIPELINE_DIR / "audio"
SYNC_INBOX = PROJECT_DIR / "syncthing-inbox"

WIKI = Path(os.environ.get(
    "MEMORYBRAIN_WIKI",
    Path.home() / ".local" / "share" / "memorybrain" / "wiki"))


# ===== CONFIGURATION — un seul endroit pour tout changer =====================
# Convention : les chemins sont regroupés ici et surchargeables par variable
# d'environnement, pour qu'on puisse changer un lien à un seul endroit.
# Tout ce qui est paramétrable vit ici, PAS dans le code.

# 1. REGROUPEMENT PAR 5
#    Cinq chunks consécutifs d'une même session sont concaténés en un seul
#    morceau. Toujours par 5, quelle que soit la longueur des chunks
#    d'origine : la diarisation a besoin d'une durée minimale pour séparer
#    les voix.
GROUPE_TAILLE = 5

# 2. MODÈLE DE TRANSCRIPTION
#    Whisper V3 LARGE FAST. Il REMPLACE le modèle Nvidia (Nemotron-3.5-ASR)
#    qui traîne dans le cache HF : large-v3-turbo est plus rapide
#    ET meilleur, et le RTF baisse nettement sur un groupe de 5.
MODELE_ASR = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODELE_ASR_NOM = "whisper-v3-large-fast"
MODELE_DIARISATION = "pyannote/speaker-diarization-community-1"

# 3. PROFILS VOCAUX (§3, §5)
#    Le profil 0 est réservé au propriétaire du micro (celui qui enregistre) ;
#    les autres locuteurs sont numérotés à partir de 1.
PROFIL_PROPRIETAIRE = 0
PREMIER_PROFIL_AUTRE = 1

# 4. CORBEILLE
#    On COPIE, on ne déplace rien tant que la V2 n'est pas validée : les
#    originaux doivent pouvoir être rejoués.
POUBELLE = Path(os.environ.get(
    "MEMORYBRAIN_POUBELLE",
    Path.home() / ".local" / "share" / "memorybrain" / "quarantaine"))
POUBELLE_PREFIXE = "memorybrain-v2"

# 5. LLM D'EXTRACTION (phase 8)
#    Modèle local retenu pour sa qualité en français :
#    `jpacifico/Chocolatine-2-4B-Instruct-DPO-v2.1` (affiné sur du français),
#    quantisé Q4_K_M. Il est servi par un serveur OpenAI-compatible local.
#
#    Vérifier le modèle réellement servi via `/v1/models` de l'API, jamais
#    via `/api/tags` (endpoint Ollama) ni via un cache local : un cache ne
#    dit pas quel modèle répond.
#
#    ⚠ Le serveur doit réserver UN seul slot et un contexte borné : quand
#    le GPU est partagé avec d'autres processus (serveur d'embeddings), la
#    réservation par défaut (plusieurs slots × le contexte) épuise la
#    mémoire et fait tomber le GPU en silence.
PORT_LLM = int(os.environ.get("MEMORYBRAIN_PORT", "8000"))
MODELE_LLM = os.environ.get("MEMORYBRAIN_LLM_MODELE", "chocolatine-2-4b")
# Replis, essayés dans l'ordre : le premier sur l'API OpenAI-compatible,
# le second sur Ollama (port 11434 par défaut).
LLM_REPLI = [
    (MODELE_LLM, PORT_LLM, "openai"),
    (os.environ.get("MEMORYBRAIN_LLM_MODELE_OLLAMA", "llama3.1:8b"),
     11434, "ollama"),
]

# 6. EMPLACEMENTS DE TRAVAIL
#    Rien de figé : tout est relatif à V2_DIR, donc un déplacement du
#    projet ne casse rien.
WORK = V2_DIR / "work"
WORK_GROUPES = WORK / "groupes"   # WAV concaténés (temporaires)
WORK_TRACE = WORK / "traces"       # livrable par étape, lisible par le propriétaire

# Les fichiers wav concaténés font 5x la taille d'un chunk. Inutile de les
# garder : le transcript et la base suffisent à rejouer (règle 5).
GARDER_AUDIO_GROUPE = False

# 7. SEUILS DE MATCHING VOCAL (§3)
#
#    Choix de conception : le seuil se place entre deux populations de
#    scores supposées séparées (même voix / autre voix), avec une marge de
#    part et d'autre. Une voix qui tombe entre les deux relève du PROBABLE,
#    pas du MATCH. À recalibrer si le modèle d'empreinte ou le découpage
#    change.
SEUIL_MATCH = 0.36
SEUIL_PROBABLE = 0.31

# Extrait minimum pour calculer une empreinte : 1,5 s. En dessous le
# vecteur change au mot près et ne veut rien dire.
MIN_EXTRAIT_EMPREINTE = 1.5

# Une voix ne crée un profil que si elle parle au moins ce temps : un
# souffle court ne doit pas produire d'identité.
MIN_SECONDE_PAR_PROFIL = 8.0

# ---------------------------------------------------------------- 8. VOIX
#    Décision unique : un seul profil est en jeu, celui du propriétaire.
#    Deux populations se dessinent — les passages qu'il dicte, proches du
#    micro, et les voix de contenu (émissions, vidéos, musique) — séparées
#    par un trou où aucun score ne tombe.
#
#    On garde deux seuils : FORT = on affirme, GRIS = on attribue au
#    propriétaire en le marquant « PROBABLE ». Rien n'est jamais attribué à
#    quelqu'un d'autre : il n'existe qu'un profil, le sien.
#
# Le seuil se place au milieu du trou, à égale distance des deux
# populations. Un seuil plus bas tombait en pleine voix de contenu : c'est
# lui qui produisait les faux positifs.
SEUIL_PROPRIETAIRE = 0.55          # au-dessus : c'est lui, sans discussion
SEUIL_PROPRIETAIRE_GRIS = 0.50     # au-dessus : à faire écouter (jamais compté)
# Fenêtres de décision : 2,5 s, pas de 1,25 s. Le coût d'une empreinte reste
# négligeable devant celui d'un diariseur complet, ce qui autorise le
# recouvrement.
FENETRE_VOIX = 2.5
PAS_VOIX = 1.25
# Part de fenêtres fortes pour déclarer sa présence dans un segment.
# En recouvrement avec une vidéo, la part de fenêtres reconnues décroît avec
# le volume du contenu et tombe à zéro dès qu'il domine nettement la voix.
PART_PROPRIETAIRE = 0.25

# Le profil ne grossit que par des extraits SANS DOUTE possible (0,75).
# Sans ce garde-fou, des voix de contenu entrent dans le profil et y
# produisent des reconnaissances fantômes.
SEUIL_ENROLEMENT = 0.75
MAX_EMPREINTES_PROPRIETAIRE = 240

# ------------------------------------------------------------ 9. MUSIQUE
#    Modèle AudioSet (AST, 87 M paramètres) appliqué par fenêtres de 10 s
#    sur CPU. Sur un extrait franchement musical, Whisper produit
#    « Sous-titrage ST' 501 » : c'est la signature d'un modèle de parole
#    livré à de la musique, et le détecteur l'attrape à la source. La
#    musique n'est jamais transcrite : elle est rangée
#    à part, sauf si le propriétaire parle par-dessus (sa voix prime toujours).
MODELE_MUSIQUE = "MIT/ast-finetuned-audioset-10-10-0.4593"
SEUIL_MUSIQUE = 0.50
BLOC_MUSIQUE = 10.0        # granularité d'analyse musicale
PART_MUSIQUE_GROUPE = 0.80  # au-delà, le groupe entier part en « musique »


# ------------------------------------------------------- 10. CASES DE 5 MIN
#    Le son est rangé dans
#    des cases qui PORTENT l'heure ; position dans le fichier = heure
#    réelle. Remplace le « regroupement par 5 fichiers ».
DUREE_CASE = 300.0
WORK_CASES = WORK / "cases"

# ------------------------------------------- 11. LA VOIX DE RÉFÉRENCE
#    ⚠ Le dossier de référence peut avoir été déplacé : il est configurable
#    (voir plus bas). Les fichiers qu'il contient sont les SEULS
#    enregistrements dont on sait qu'ils sont du propriétaire.
#
#    Règle qui a coûté cher : le profil vocal ne se construit QUE sur ces
#    références. Enrôler automatiquement depuis le corpus revient à nourrir le
#    profil de ce que le pipeline croit déjà être le propriétaire : une voix
#    de média mal attribuée y entre alors pour de bon.
#
#    `clone_*.wav` sont volontairement EXCLUS : ce sont des sorties de
#    synthèse vocale, pas des enregistrements de voix réelle.
# Dossier des enregistrements de référence : hors dépôt, jamais versionné.
# `MEMORYBRAIN_VOIX` permet de le placer où l'on veut sur sa machine.
REFERENCE_VOIX = Path(os.environ.get(
    "MEMORYBRAIN_VOIX",
    Path.home() / ".local" / "share" / "memorybrain" / "reference"))
FICHIERS_REFERENCE = (
    "ref_01.wav",
    "ref_02.wav",
    "ref_03.wav",
    "ref_04.wav",
)

# Seuils de décision sur un SEGMENT (plusieurs fenêtres de 2,5 s).
# Deux populations nettement séparées : voix du propriétaire d'un côté,
# contenu de l'autre. Provisoires tant que le test à l'aveugle n'a pas tranché.
VOIX_SEUIL_FORT = 0.55     # une fenêtre au-dessus = elle ressemble au propriétaire
VOIX_PART_FORT = 0.50      # et il faut que la moitié des fenêtres y soient
VOIX_PART_GRIS = 0.35      # 35 % : « probable », affiché en orange

# Niveau en dessous duquel une fenêtre ne contient pas de parole.
# Le seuil est placé juste au-dessus du silence numérique et très en dessous
# d'une voix lointaine, qui reste faible mais parfaitement exploitable.
RMS_MIN = 0.001


def ensure_dirs() -> None:
    for d in ALL_DIRS + [WORK, WORK_GROUPES, WORK_TRACE, WORK_CASES]:
        d.mkdir(parents=True, exist_ok=True)

# -------------------------------------------- 12. DIARISATION (NVIDIA)
#    Modèle : Nemotron-3-Diarization (Sortformer, jusqu'à 8 locuteurs),
#    déjà sur le disque, exécuté par le runtime natif NeMo-Speech.cpp.
#
#    Il rend UNE décision par locuteur et non par fenêtre : le locuteur qui
#    parle longtemps est jugé d'un bloc, les autres restent du contenu. C'est
#    cette séparation qui remplace le marquage fenêtre par fenêtre.
#
#    Pourquoi pas pyannote : plus lent sur CPU que l'approche par fenêtres,
#    à résultat équivalent. Les profils en surnombre venaient de
#    `identify.attribuer_profils`, pas de lui.
NEMO_SPEECH = Path(os.environ.get(
    "MEMORYBRAIN_NEMO_SPEECH",
    Path.home() / ".local" / "bin" / "nemo-speech"))
MODELE_DIARISATION_GGUF = Path(os.environ.get(
    "MEMORYBRAIN_NEMOTRON_DIAR",
    Path.home() / ".local" / "share" / "memorybrain" / "models"
    / "Nemotron-3-Diarization" / "Nemotron-3-Diarization.q8_0.gguf"))
WORK_DIARISATION = WORK / "diarisation"

# Un locuteur doit parler au moins ça pour recevoir une identité.
# Quelques secondes de voix faible suffisent à donner un score net, alors
# qu'un long passage de contenu reste bas : une règle des 10 s jetait donc la
# voix lointaine pour rien. À 4 s, le seuil de score (0,55) suffit à écarter
# un autre locuteur. En dessous, on ne décide pas — un fragment très court
# peut sortir haut par accident.
MIN_DUREE_IDENTITE = 4.0

# Réglages du diariseur : avec les valeurs par défaut, une voix faible tombe
# dans des locuteurs de quelques dixièmes de seconde — sous la durée minimale
# d'identification, donc jamais reconnue.
# `--onset 0,3 --offset 0,25` (abaisser les seuils d'activation) la fait
# rentrer dans le groupe principal, sans créer d'attribution sur du contenu
# pur (podcast, émission).
# On abaisse donc les seuils d'activation : mieux vaut capter une voix faible
# que la perdre, l'identification par empreinte tranche ensuite.
DIARISATION_OPTS = ["--onset", "0.3", "--offset", "0.25"]

# ------------------------------------- 13. OÙ VONT LES CHOSES
#    Contrat de rangement : ce module ne range rien, il produit des
#    fichiers markdown dans le dossier de sortie configuré
#    (`MEMORYBRAIN_SOCLE`). Quatre destinations, pas une de plus.
SOCLE = Path(os.environ.get(
    "MEMORYBRAIN_SOCLE",
    Path.home() / ".local" / "share" / "memorybrain"))
DICTEES = SOCLE / "dictees"
ECOUTES = SOCLE / "wiki" / "raw" / "ecoutes"
TRANSCRIPTS_SOCLE = SOCLE / "wiki" / "raw" / "transcripts"

# Rangement par ANNÉE (une année tient dans un dossier lisible),
# avec un index généré par année. La date reste dans le nom du fichier, donc
# on peut découper par mois plus tard sans rien perdre.
def dossier_idees(annee: int) -> Path:
    return DICTEES / f"idees-{annee}"


def dossier_notes(annee: int) -> Path:
    return DICTEES / f"notes-{annee}"


def dossier_transcripts(annee: int) -> Path:
    return TRANSCRIPTS_SOCLE / str(annee)


# ⚠ RÈGLE ABSOLUE : on ne supprime jamais rien. Tout ce qu'on retire passe par la corbeille, datée, pour
# qu'on puisse fouiller et revenir en arrière. Jamais de `rm`.
CORBEILLE = Path(os.environ.get(
    "MEMORYBRAIN_CORBEILLE",
    Path.home() / ".local" / "share" / "memorybrain" / "corbeille"))
