# MemoryBrain

**Transformer des enregistrements audio en notes consultables** —
transcription, séparation des voix, attribution au porteur du micro, puis
écriture de fiches markdown.

Le programme part d'un principe simple : une source audio enregistre en
continu, les fichiers sont synchronisés sur une machine, découpés en cases de
5 minutes, transcrits, puis transformés en notes lisibles. Aucune note n'est
écrite à la main : tout part de l'audio.

> Ce dépôt contient **le programme uniquement** : ni audio, ni transcription,
> ni base SQLite, ni empreinte vocale. Les noms de fichiers, les dates et les
> valeurs cités dans la documentation sont des exemples génériques : ils ne
> décrivent aucun enregistrement réel.

---

## Ce que fait le programme

```
 source audio (téléphone, dictaphone, tout outil qui écrit des WAV)
        │  synchronisation de fichiers
        ▼
 syncthing-inbox/      segments de 1 min nommés AAAA-MM-JJ_HH-MM-SS-mmm_NNNN.wav
        │  ingestion      (l'heure de départ est lue dans le nom)
        ▼
 base SQLite `chunk`   start_time, durée, fichier d'origine
        │  assemblage
        ▼
 cases de 5 minutes    audio posé à sa position exacte, silence ailleurs
        │  transcription
        ▼
 `segment`             phrases horodatées (faster-whisper)
        │  diarisation
        ▼
 tours de parole       qui parle quand
        │  attribution
        ▼
 locuteur par phrase   profil 0 = le porteur du micro
        │  écriture
        ▼
 fiches markdown       idées, notes, tâches
```

**La règle centrale** : l'état vit dans la base, jamais dans les fichiers. Une
case a cinq états (`assemblee → transcrite → diarisee → ecrite`), et une passe
ne refait jamais un travail déjà fait.

---

## L'application d'enregistrement (Android)

Le code source de l'application est dans `android/` (Java, Gradle, `minSdk`
34 — Android 14). Aucun binaire n'est versionné, et aucune release n'est
publiée à ce jour : l'APK se compile depuis les sources.

```bash
cd android
gradle assembleDebug        # APK dans app/build/outputs/apk/debug/
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Le dépôt ne contient ni wrapper Gradle (`gradlew`) ni configuration de
signature : utilisez un Gradle installé localement, et ajoutez votre propre
signature si vous voulez distribuer une release.

Ce que fait l'application : elle enregistre par segments d'une minute, puis
publie chaque segment complet dans la bibliothèque audio partagée du
téléphone (MediaStore, type `audio/wav`). Elle ne choisit aucun dossier de
synchronisation : c'est l'outil de synchronisation que vous utilisez qui
reprend ce dossier et le fait arriver dans le dossier d'entrée du pipeline
(`config.SYNC_INBOX`) sur la machine qui l'exécute.

Le sous-dossier de publication est réglable à la compilation dans
`android/gradle.properties` (`memorybrain.audioDir`, `Music/MemoryAudio` par
défaut) : c'est ce nom que votre outil de synchronisation doit reprendre.

Chaque fichier porte son heure de début dans son nom :

```
AAAA-MM-JJ_HH-MM-SS-mmm_NNNN.wav
└─ date ───┘ └heure┘ └ ms┘ └ n° de séquence
```

C'est cet horodatage qui permet de reconstruire les cases de 5 minutes côté
serveur, quel que soit l'ordre d'arrivée des fichiers.

---

## Installation

### 1. Prérequis

- Python 3.11 ou plus récent
- `ffmpeg` (traitement audio)
- de quoi faire tourner Whisper : un CPU récent suffit (compter de quelques
  dizaines de secondes à quelques minutes de calcul par case de 5 minutes
  selon la machine, avec `large-v3-turbo` en int8)

```bash
sudo apt install ffmpeg python3-venv
```

### 2. Le programme

```bash
git clone <ce-dépôt> memorybrain
cd memorybrain
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# garde-fou de confidentialité avant chaque commit (une fois par clone) :
git config core.hooksPath outils/hooks
```

### 3. La structure de travail

```bash
python3 -c "import config; [d.mkdir(parents=True, exist_ok=True) for d in config.ALL_DIRS]"
python3 -c "import db; db.init()"
```

Tous les chemins de travail dérivent de l'emplacement de `config.py` : le
projet fonctionne là où il est cloné. Les sorties, elles, ont deux racines
distinctes : `config.SOCLE` (fiches rangées par année, notes d'écoute,
transcripts) et `config.WIKI` (fiches markdown produites par
`interface/fiches.py`). Les deux sont des valeurs de `config.py`, par défaut
sous `~/.local/share/memorybrain/`, surchargeables par `MEMORYBRAIN_SOCLE` et
`MEMORYBRAIN_WIKI`.

### 4. Configuration (facultative)

Le programme fonctionne sans aucun réglage. Les fiches, la corbeille et les
enregistrements de référence vont sous `~/.local/share/memorybrain/` ; la base
SQLite et les fichiers de travail restent dans le dossier du projet
(`database/`, `work/`, `audio/`, `transcripts/`, `diarization/`, `speakers/`),
tous exclus par `.gitignore`. Pour placer ces dossiers ailleurs (autre disque,
dossier déjà existant) :

```bash
cp .env.example .env
# éditez .env, puis chargez-le avant de lancer une passe :
set -a; . ./.env; set +a
```

Variables reconnues : `MEMORYBRAIN_SOCLE` (racine des fiches),
`MEMORYBRAIN_CORBEILLE` (quarantaine), `MEMORYBRAIN_VOIX` (enregistrements de
référence du profil 0), `MEMORYBRAIN_INBOX` (dossier d'entrée de l'audio),
`MEMORYBRAIN_NEMOTRON_DIAR` (modèle de diarisation GGUF), `HF_TOKEN`
(seulement si vous utilisez pyannote), plus les réglages de répartition du
travail (`MB_WORKER`, `MB_TOTAL_WORKERS`, `MB_LLM`…).

`.env` n'est jamais versionné. `config.py` reste la seule source des valeurs
par défaut : lisez-le avant de changer un chemin, plusieurs sont calculés
relativement au dossier du projet.

### 5. Brancher la source audio

Le programme lit le dossier d'entrée déclaré dans `config.SYNC_INBOX`
(`syncthing-inbox/` par défaut, à côté de `config.py` ; `MEMORYBRAIN_INBOX`
pour le placer ailleurs ou lui donner un autre nom). C'est le dossier où un
téléphone (ou n'importe quel outil) dépose des fichiers WAV **dont le nom
commence par la date-heure de l'enregistrement** :

```
AAAA-MM-JJ_HH-MM-SS-mmm_NNNN.wav
└─ date ───┘ └heure┘         └ numéro de séquence
```

Si votre source nomme autrement, adaptez `ingestion/naming.py` : c'est le seul
endroit qui décide ce qu'est un nom valide.

---

## Utilisation

Trois points d'entrée, trois usages différents. Ils partagent la même base et
la même `config.py`, et s'enchaînent dans cet ordre.

| script | rôle | quand |
|---|---|---|
| `build.py` | prépare la base : ingestion, sessions, transcription à la demande | première mise en route, rattrapage |
| `pipeline_v2.py` | la chaîne V2 complète en sept étapes, avec un contrôle par étape | traitement courant |
| `passe.py` | passe d'exploitation : état, plage horaire, fiches, compte rendu, ménage | suivi quotidien |

```bash
# 1. préparer la base depuis les fichiers déposés
python3 build.py --ingest
python3 build.py --sessions
python3 build.py --status

# 2. la chaîne complète (groupement → … → extraction), contrôles inclus
python3 pipeline_v2.py --etape tout --n 3
python3 pipeline_v2.py --status
python3 pipeline_v2.py --verifier-etape tout      # contrôles seuls, rien ne tourne

# 3. le suivi et les sorties
python3 passe.py --etat
python3 passe.py --plage AAAA-MM-JJTHH:MM:SS AAAA-MM-JJTHH:MM:SS
python3 passe.py --ecrire --jour AAAA-MM-JJ
python3 passe.py --compte-rendu
python3 passe.py --menage
```

`--plage` attend deux dates ISO 8601 (`datetime.fromisoformat`), pas le format
des noms de fichiers. `--dry-run` montre ce qui serait fait sans rien faire.

---

## Choix techniques (et pourquoi)

### Transcription : faster-whisper `large-v3-turbo`

Ordres de grandeur : trois moteurs ont été comparés sur la même tâche. Le
premier est celui que le programme utilise, pour son rapport vitesse/qualité
sur du français parlé :

| moteur | qualité constatée |
|---|---|
| **faster-whisper `large-v3-turbo`** (ctranslate2, int8, CPU) | référence |
| Nemotron-3.5-ASR (NVIDIA) | moins bon en français |
| whisper.cpp (backend GPU) | **jette des passages entiers** |

Aucune durée n'est donnée ici, volontairement : elle dépend entièrement du
matériel, du modèle et de la longueur de la case. Mesurez sur votre machine
avant d'arbitrer.

Deux pièges documentés dans ce dépôt :

1. **Whisper travaille par fenêtres de 30 secondes.** Les horodatages des
   segments se calent sur le début de la fenêtre : jusqu'à 30 s d'écart avec le
   moment réel de la parole. `word_timestamps=True` corrige cela (voir
   `transcription/`).
2. **Whisper invente des génériques** quand on lui donne du silence ou du
   bruit : « Sous-titrage ST' 501 », « Amara.org », « merci d'avoir regardé ».
   Le filtre `transcription/hallucinations.py` les écarte **avant** l'écriture
   en base. C'est indispensable : sans lui, ces phrases polluent les notes.

### Diarisation

`outils/transcript_diarise.py` accepte deux moteurs :

- **pyannote** (historique, nécessite un jeton Hugging Face) ;
- **Nemotron-3-Diarization** via `nemo-speech` (local, aucun jeton).

### Extraction : un LLM local

Les idées et les tâches sont extraites par un modèle de langue servi en local.
Le modèle attendu est déclaré une seule fois, dans `config.MODELE_LLM`
(`chocolatine-2-4b`, servi par `llama-server` sur `config.PORT_LLM`). Aucun
second serveur n'est interrogé en douce : `config.LLM_REPLI` ne contient que
celui-là, et le programme vérifie l'alias annoncé avant de le croire. Si le
serveur ne répond pas, l'étape est notée comme sautée : le pipeline n'invente
jamais une idée à la place du modèle.

## Vie privée et données

Ce dépôt ne contient **que du code**.

- aucun audio, aucune transcription, aucune base SQLite, aucune empreinte
  vocale : ces fichiers sont exclus par `.gitignore` **et** refusés par un
  garde-fou `pre-commit` (activé par `git config core.hooksPath outils/hooks`),
  qui refuse les chemins de compte, les chemins sous `~`, les noms d'hôte
  écrits en dur, les adresses IP privées, les points d'entrée de service local
  écrits en dur, les décomptes de corpus, les adresses e-mail et les extensions
  de données — y compris dans un fichier `.md`. Les mêmes contrôles sont
  rejoués par la CI à chaque push, avec Gitleaks sur tout l'historique. Les
  noms de personnes ne sont filtrés que si
  vous fournissez votre propre liste locale (`.git/pre-commit-local.txt`,
  modèle dans `outils/hooks/pre-commit-local.example`) : dans un clone neuf,
  cette liste n'existe pas ;
- les dossiers de travail (`syncthing-inbox/`, `audio/`, `work/`, `chunks/`,
  `database/`, `transcripts/`, `diarization/`, `speakers/`) sont créés chez
  vous, jamais poussés ;
- pour tout remettre à zéro, il suffit de déplacer ces dossiers dans une
  corbeille : aucun n'est nécessaire au fonctionnement du programme, qui les
  recrée à la demande.

Le profil 0 est créé d'office : c'est le porteur du micro, celui qui
enregistre. Les autres voix reçoivent un numéro dans l'ordre du temps parlé et
restent `UNKNOWN` tant que personne ne leur donne un nom ; une voix trop
brève n'obtient aucun profil. Les enregistrements de référence qui servent à
comparer les voix sont lus dans `MEMORYBRAIN_VOIX` : le programme ne les
cherche pas dans le corpus.

---

## Structure du dépôt

| chemin | rôle |
|---|---|
| `config.py` | tous les chemins et réglages, en un seul endroit |
| `db.py` | schéma et accès SQLite |
| `blocs.py` | assemblage des cases de 5 minutes |
| `build.py` | préparation de la base : ingestion, sessions, transcription |
| `pipeline_v2.py` | **la chaîne V2** : sept étapes nommées, un contrôle par étape |
| `passe.py` | passe d'exploitation : état, plage horaire, fiches, ménage |
| `hub.py`, `worker_v2.py` | ancien mode distribué (conservé, plus utilisé) |
| `ingestion/` | lecture des fichiers déposés, heure tirée du nom |
| `transcription/` | Whisper, filtre anti-hallucination |
| `diarization/` | tours de parole |
| `sessions/` | regroupement des cases en sessions |
| `grouping/` | regroupement des chunks en groupes de 5 |
| `speakers/` | empreinte vocale, attribution des phrases |
| `classification/` | classe du contenu (personnel, média, business) |
| `extraction/` | passages des phrases vers les fiches, appel du LLM |
| `interface/` | écriture des fiches et des transcripts markdown |
| `verification/` | contrôles par étape (`controles.py`) |
| `android/` | application d'enregistrement (Java, Gradle) |
| `outils/` | scripts d'exploitation, garde-fou `hooks/pre-commit` |
| `tests/` | suite de tests (unittest) |

---

## État du projet

C'est un projet **en cours**, pas un produit fini :

- la suite de tests compte 119 cas (`unittest`, aucun `pytest` requis) ; ceux
  qui s'appuient sur un corpus local sont ignorés quand il est absent ; elle
  passe intégralement sur un clone neuf ;
- le code est commenté en français, avec les décisions et leurs raisons ;
- certaines parties sont historiques (V1) et gardées pour comprendre
  l'évolution.

Contributions et rapports de bugs bienvenus.

---

## Licence

MIT — voir [LICENSE](LICENSE).
