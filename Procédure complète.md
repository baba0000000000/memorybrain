# 🧠 MemoryBrain V2

## Architecture, pipeline et principes

> **But :** construire un système de mémoire locale, modulaire et évolutif.
>
> Le système reçoit de l'audio (et de la vidéo) depuis une source
> d'enregistrement, transcrit, sépare les voix, comprend le contexte, extrait
> les informations utiles (idées, tâches, notes) et les range dans un dossier
> de notes markdown.
>
> Il doit fonctionner **automatiquement dans la grande majorité des cas**,
> avec une petite interface locale uniquement lorsque l'intervention humaine
> est nécessaire.

Ce document est le mode d'emploi technique : décisions, architecture,
installation, commandes, contrôles et dépannage. Le `README.md` donne la vue
d'ensemble ; ici, on entre dans le détail.

---

# 0. DÉCISIONS V2

> Tout ce qui est paramétrable vit dans `config.py`, **jamais en dur dans le
> code** (règle 18). Ces décisions sont modifiables : on met la procédure à
> jour à chaque fois qu'on s'en écarte.

| Sujet | Décision | Où c'est réglé |
|---|---|---|
| Modèle de transcription | **faster-whisper `large-v3-turbo`** (dépôt `mobiuslabsgmbh/faster-whisper-large-v3-turbo`) — remplace le modèle NVIDIA (Nemotron) | `config.MODELE_ASR` |
| Modèle de diarisation | **pyannote** `speaker-diarization-community-1` (CPU ou GPU, au choix) ; alternative locale Nemotron-3-Diarization | `config.MODELE_DIARISATION` |
| Extraction / idées (LLM) | modèle local servi par `llama-server`, aucun appel d'API facturé | `config.MODELE_LLM`, `config.LLM_REPLI` |
| Regroupement | **5 fichiers = 1 groupe**, quelle que soit la durée (règle 2) | `config.GROUPE_TAILLE` |
| Profil vocal 0 | = **le porteur du micro** ; les autres à partir de 1 | `config.PROFIL_PROPRIETAIRE` |
| Quarantaine | dossier hors dépôt — on **copie**, on ne supprime rien avant validation | `config.CORBEILLE` |
| Ordre des étapes | groupement → transcription → **voix** (diarisation + profils) → alignement → classification → markdown → extraction | `pipeline_v2.py` (`ETAPES`) |

⚠ **Toute étape se vérifie avant de passer à la suivante.** Une étape qui
« a tourné » n'est pas une étape qui « a réussi » : voir §27 (contrôles).

---

# 0 bis. RÈGLES FONDAMENTALES

## Règle 1 — Ne pas tout refaire

La chaîne d'acquisition est considérée comme acquise :

```text
source d'enregistrement
   ↓
synchronisation de fichiers
   ↓
machine locale
   ↓
audio brut
```

Puis :

```text
audio
   ↓
VAD
   ↓
transcription
```

La reconstruction commence principalement **à partir de la transcription /
segmentation**.

---

## Règle 2 — Les fichiers d'1 minute sont des chunks techniques

Un fichier d'1 minute (le découpage de l'application d'enregistrement) n'est
PAS forcément :

* une conversation ;
* une vidéo ;
* une session ;
* une unité logique ;
* une idée.

C'est uniquement une **unité de traitement technique**.

⚠ **1 minute est trop court pour la diarisation** : impossible de séparer
deux voix sur un segment aussi court. Donc **on regroupe par 5 fichiers**
avant tout traitement : 1er, 2ème, 3ème, 4ème, 5ème → **un seul morceau de
5 minutes**. Toujours par 5, quelle que soit la durée des fichiers d'origine.

Exemple :

```text
Unité technique de l'application (1 min)
│
├── chunk 001 — 00:00 → 01:00
├── chunk 002 — 01:00 → 02:00
├── chunk 003 — 02:00 → 03:00
└── ...

Regroupement (décision de conception) : 5 fichiers → 1 morceau
│
├── groupe 001 = chunks 001→005  (≈ 5 min)
├── groupe 002 = chunks 006→010  (≈ 5 min)
└── ...
```

Le regroupement est justifié par la mesure : le RTF de Whisper baisse
nettement quand on lui donne 5 minutes au lieu de 60 secondes, et les phrases
coupées entre deux chunks redeviennent des phrases entières. Les valeurs
exactes dépendent du modèle et de la machine : à re-mesurer avant de
s'appuyer dessus.

Les chunks doivent conserver leur position temporelle **absolue** (elle
se lit dans l'horodatage du nom de fichier, jamais par `index × 60`).

---

## Règle 3 — Une information inconnue ne doit jamais bloquer le pipeline

Exemples d'états parfaitement valides :

```text
speaker = UNKNOWN
source = UNKNOWN
classification = UNCLASSIFIED
session = UNKNOWN
```

Le système continue.

Les informations peuvent être enrichies plus tard.

---

## Règle 4 — Ne jamais forcer une décision incertaine

Le système doit pouvoir dire :

```text
MATCH
PROBABLE
AMBIGU
UNKNOWN
```

Il ne doit jamais inventer une identité ou une classification simplement
parce qu'une étape attend une réponse.

---

## Règle 5 — Les données brutes ne sont pas supprimées immédiatement

Une fois le traitement terminé :

```text
WAV original
   ↓
traitement complet
   ↓
archivage compressé
   ↓
suppression éventuelle du WAV lourd
```

Les données importantes restent :

* transcript ;
* timestamps ;
* speakers ;
* embeddings vocaux ;
* métadonnées ;
* classification ;
* idées ;
* tâches ;
* liens vers les sources.

Le WAV peut être compressé ou supprimé après validation — jamais avant, et
jamais par `rm` : la quarantaine existe pour ça.

---

# 1. ARCHITECTURE GÉNÉRALE

```text
                    SOURCE D'ENREGISTREMENT
                              │
                              ▼
                  SYNCHRONISATION DE FICHIERS
                              │
                              ▼
                    ┌──────────────────┐
                    │ INGESTION LOCALE │
                    │ SHA-256 / HASH   │
                    │ métadonnées      │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ AUDIO / VIDÉO     │
                    │ source brute      │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ VAD / NORMALIS.  │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ CHUNKS 5 MIN     │
                    │ unité technique  │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
      ┌────────────────┐           ┌────────────────┐
      │ TRANSCRIPTION  │           │ DIARISATION    │
      │ ASR            │           │ QUI PARLE ?    │
      └────────┬───────┘           └───────┬────────┘
               │                           │
               └─────────────┬─────────────┘
                             ▼
                  ┌──────────────────────┐
                  │ ALIGNEMENT           │
                  │ texte ↔ locuteur     │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ IDENTIFICATION       │
                  │ speaker_XXXX         │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ CONTEXTE / SIGNAUX   │
                  │ source / marqueurs   │
                  │ type de contenu      │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ CLASSIFICATION       │
                  │ classe du contenu    │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ EXTRACTION LLM       │
                  │ idées / tâches/notes │
                  └──────────┬───────────┘
                             │
                             ▼
                       SOCLE DE NOTES
```

La classification est une propriété calculée par le programme, pas un
classement de la vie de quelqu'un : les classes par défaut sont déclarées dans
`classification/` et peuvent être étendues.

---

# 2. OBJETS FONDAMENTAUX

Le système doit séparer clairement plusieurs concepts.

## SOURCE

Ce qui produit le contenu.

Exemples :

```text
source_001
= microphone d'un téléphone

source_002
= vidéo en ligne

source_003
= film

source_004
= fichier audio importé
```

---

## SESSION

Une période d'utilisation continue du système.

Une session n'est PAS forcément une vidéo.

Exemple :

```text
session_XXXX

HH:00 → contenu (film, vidéo, musique)
HH:MM → pause
HH:MM → reprise
HH:MM → conversation
```

Il n'est pas nécessaire de connaître parfaitement la session pour traiter les
données.

---

## CHUNK

Un morceau technique de 5 minutes.

```text
chunk_0001
chunk_0002
chunk_0003
...
```

Chaque chunk doit conserver :

```text
session_id éventuel
source_id éventuel
start_time
end_time
original_file
hash
```

## GROUPE

Cinq chunks consécutifs d'une même session, concaténés en un WAV de travail.

```text
groupe_0001 = chunk_0001 → chunk_0005
```

Le nom du fichier de travail commence par le numéro de groupe
(`g<N>_s<S>_AAAA-MM-JJ_HH-MM-SS_5ch.wav`) ; la table `chunk_group` reste la
source de vérité.

---

## SPEAKER

Identité vocale persistante.

Exemple :

```text
speaker_0000
speaker_0001
...
speaker_XXXX
```

Le numéro du speaker ne change jamais.

---

# 3. PROFILS VOCAUX

## Principe

Le programme distingue **le porteur du micro** de tout le reste. C'est la
seule identité qu'il cherche à établir : le profil 0, celui de la personne qui
enregistre.

Il ne cherche pas à identifier les autres personnes : leur intervention est
étiquetée `AUTRE` ou reçoit un numéro anonyme, et n'est jamais rattachée
d'office à un individu.

Pipeline :

```text
audio
 ↓
diarisation
 ↓
segments vocaux
 ↓
speaker embedding
 ↓
comparaison avec le profil de référence
 ↓
porteur / AUTRE / UNKNOWN
```

Principe de décision — le meilleur score ne suffit pas à lui seul. On prend
également en compte :

* le score de similarité, comparé au profil de référence ;
* l'écart avec le deuxième candidat ;
* la qualité de l'extrait ;
* la durée de parole (un fragment très court ne décide de rien) ;
* la continuité avec les chunks précédents.

Les seuils sont dans `config.py` (`VOIX_SEUIL_FORT`, `VOIX_PART_FORT`,
`VOIX_PART_GRIS`, `MIN_DUREE_IDENTITE`) : ce sont des valeurs de travail, à
valider par un test à l'aveugle sur vos propres enregistrements. Un seuil
choisi sur un seul corpus se transporte mal.

---

# 4. PROFIL VOCAL ≠ NOM

Le système doit séparer l'identité interne :

```text
speaker_XXXX
```

du libellé humain qu'on peut lui accoler :

```text
nom_exemple
```

Exemple :

```text
speaker_XXXX
    name = nom_exemple
    status = exemple
```

Le profil vocal reste stable.

Le nom humain est une information modifiable, et purement locale : il n'est
jamais déduit du contenu, et le programme ne l'écrit pas dans un nom de
fichier.

---

# 5. PROFILS INCONNUS

Lorsqu'une nouvelle voix apparaît :

```text
speaker_YYYY
status = UNKNOWN
```

Elle peut être conservée même si personne ne sait encore qui c'est : c'est un
numéro, pas une identité.

Un libellé peut être ajouté plus tard, quand l'utilisateur le décide :

```text
speaker_YYYY (libellé : invité)
```

Le libellé sert à relire les transcripts. Il ne transforme pas un numéro en
preuve d'identité, et il ne doit jamais servir à établir qui a dit quoi dans
un cadre où cela aurait des conséquences pour la personne concernée.

---

# 6. EMBEDDINGS VOCAUX

Ne pas conserver une seule empreinte vocale.

Un profil peut contenir plusieurs exemples :

```text
speaker_XXXX/
    embedding_001
    embedding_002
    embedding_003
    embedding_004
```

Pourquoi :

* téléphone ;
* distance au micro ;
* bruit ;
* réverbération ;
* voiture ;
* voix faible ;
* vidéo ;
* conditions différentes.

L'identité logique est stable ; les représentations vocales peuvent évoluer.

⚠ Le profil 0 ne se construit **que** sur les enregistrements déposés dans
`MEMORYBRAIN_VOIX`. Enrôler automatiquement depuis le corpus revient à nourrir
le profil de ce que le pipeline croit déjà être le porteur : une voix de média
mal attribuée y entre alors pour de bon.

---

# 7. CONTINUITÉ ENTRE LES CHUNKS

Ne pas traiter chaque chunk comme un monde indépendant.

Exemple :

```text
chunk 101
speaker local A

chunk 102
speaker local A

chunk 103
speaker local A
```

Le système doit essayer de conserver la continuité :

```text
speaker local A
       ↓
speaker_XXXX
```

Un état/cache de locuteurs peut être conservé entre chunks.

Aux frontières des chunks, utiliser éventuellement une petite fenêtre de
contexte supplémentaire afin d'éviter de couper une phrase ou un locuteur.

---

# 8. SESSION : NE PAS ESSAYER DE TOUT DEVINER

Le système peut parfois savoir qu'une suite de chunks appartient à la même
source.

Signaux possibles :

```text
continuité temporelle
métadonnées
nom de fichier
application connue
URL disponible
titre disponible
continuité audio
locuteurs
contenu transcripté
```

Mais aucune de ces informations ne doit être obligatoire.

Si le système ne sait pas :

```text
source = UNKNOWN
```

et continue.

---

# 9. QUI PARLE ?

Le transcript final doit être lisible humainement.

Format souhaité :

```text
[00:12:43] Profil 1 :
Aujourd'hui je vais tester...

[00:12:51] Profil 7 :
Oui mais attends...

[00:13:02] Profil 1 :
Non, justement...
```

Si le profil 0 est reconnu :

```text
[00:12:43] Profil 0 (porteur) :
...

[00:12:51] Profil 7 :
...
```

Le nom ne doit pas être intégré au nom du fichier.

---

# 10. LA VOIX EST UN SIGNAL, PAS UNE CLASSIFICATION

La voix répond principalement à :

> **QUI parle ?**

Elle ne répond pas automatiquement à :

> **QU'EST-CE QUE c'est ?**

Exemple :

```text
speaker_0001 = porteur du micro
```

peut parler :

* à quelqu'un d'autre ;
* pendant un film ;
* pendant une vidéo en ligne ;
* en réunion ;
* d'un sujet personnel ;
* d'un sujet professionnel ;
* en dictant une idée.

Donc la classification utilise plusieurs signaux.

---

# 11. CLASSIFICATION MULTI-SIGNAUX

Le système peut combiner :

```text
speaker
+
source
+
contexte connu
+
marqueurs explicites
+
contenu transcripté
+
présence d'autres speakers
+
type de média
```

Exemple :

```text
speaker = porteur
source = film
marqueur = IDÉE

→ IDÉE
```

Autre exemple :

```text
speaker = porteur
speaker_XXXX = présent
source = microphone

→ CONVERSATION
```

Autre exemple :

```text
speaker = porteur
source = vidéo en ligne
marqueur = TODO

→ TODO
```

Les classes par défaut sont `PERSONNEL`, `MEDIA`, `BUSINESS` et `UNKNOWN`
(`classification/`). Elles décrivent la **nature du contenu** — produit par le
porteur du micro, ou écouté — et non la personne qui parle.

---

# 12. CONTEXTE FOURNI PAR L'UTILISATEUR

Lorsque l'utilisateur veut être plus précis, il peut fournir un contexte.

Exemples :

```text
Ce passage est un film.
```

ou :

```text
Cette vidéo parle de référencement.
```

Mais ce contexte est OPTIONNEL.

Le système doit fonctionner sans lui.

---

# 13. CAS PARTICULIER : FILM / VIDÉO / MÉDIA

Ne jamais faire :

```text
MEDIA → supprimer tout
```

Faire plutôt :

```text
MEDIA
│
├── contenu externe
│      ↓
│   faible priorité
│
└── interventions du porteur du micro
       ↓
    analysées normalement
```

Exemple :

```text
Film
│
├── dialogue du film → ignorable
│
├── dialogue du film → ignorable
│
├── porteur : "Attends, ça me donne une idée"
│          ↓
│        IMPORTANT
│
├── dialogue du film → ignorable
│
└── porteur : TODO ...
           ↓
         TODO
```

Le contexte modifie la priorité ; il ne détruit pas les données.

---

# 14. MARQUEURS EXPLICITES

Les marqueurs prononcés par le porteur du micro sont des signaux prioritaires.

## IDÉE

```text
IDÉE
...
FIN DE L'IDÉE
```

ou variantes prévues :

```text
nouvelle idée
```

Le système comprend :

```text
début = marqueur
fin = FIN DE L'IDÉE
```

Le LLM reçoit ensuite uniquement la portion pertinente.

---

## TODO

Même principe :

```text
TODO
...
FIN TODO
```

ou formulation équivalente.

Le système doit pouvoir transformer le contenu en action à faire.

---

## FUTURS TYPES POSSIBLES

Architecture extensible :

```text
NOTE
QUESTION
DECISION
RAPPEL
REFERENCE
...
```

Ils ne sont pas obligatoires dans la première version.

---

# 15. LE LLM N'EST PAS LA PLOMBERIE

Le maximum de travail doit être déterministe :

```text
Python
FFmpeg
VAD
timestamps
diarisation
embeddings
matching
classification simple
SQLite
fichiers
```

Le LLM intervient principalement pour :

```text
résumer
extraire une idée
transformer une idée en fiche
extraire une TODO
classer les informations ambiguës
proposer une catégorie
```

Le LLM ne doit pas avoir à deviner simultanément :

```text
qui parle
quand
où commence l'idée
où elle finit
quel est le fichier
quel est le contexte
```

Ces informations doivent lui être fournies.

---

# 16. ARCHIVAGE AUDIO

Le WAV est utilisé pour faciliter le traitement local.

Après traitement complet :

```text
WAV
 ↓
transcription OK
 ↓
diarisation OK
 ↓
embeddings OK
 ↓
classification OK
 ↓
résultats sauvegardés
 ↓
compression audio
 ↓
suppression éventuelle du WAV
```

Le format et le débit de compression doivent être déterminés après test de
qualité, notamment pour la reconnaissance des locuteurs.

Une compression trop agressive dégrade la diarisation : c'est la qualité de
la voix qui compte ici, pas le débit. Et rien ne se jette sans passer par la
quarantaine.

---

# 17. BASE DE DONNÉES

Une petite base SQLite porte l'état technique du pipeline
(`database/memorybrain.sqlite`).

Elle doit notamment permettre de savoir :

```text
quel fichier existe ?
quel hash ?
quelle étape est terminée ?
quel modèle a produit le résultat ?
quelle version ?
quel speaker ?
quelle source ?
quel statut ?
```

Tables principales :

```text
source
session
chunk
chunk_group
group_member
speaker
speaker_embedding
segment
turn
artifact
pipeline_step
classification
extraction
bloc
meta
```

Les fichiers Markdown restent les sorties lisibles.

SQLite devient la mémoire technique du pipeline.

---

# 18. VERSIONNAGE

## CODE

Versionnage : git.

```text
scripts
configuration
prompts
schemas
tests
```

Le code doit être versionné automatiquement si possible.

---

## DONNÉES TECHNIQUES

SQLite / fichiers :

```text
état du pipeline
résultats
métadonnées
embeddings
```

Ne pas mettre inutilement tous les fichiers lourds dans Git : `.gitignore`
exclut déjà les dossiers de travail, les audios, les bases et les empreintes.

---

## MODÈLES

Chaque résultat important doit pouvoir indiquer :

```text
model
model_version
pipeline_version
date
```

Exemple :

```text
transcription
model = faster-whisper
version = X

diarization
model = ...
version = Y
```

Ainsi un changement de modèle ne casse pas les anciens résultats.

---

# 19. PIPELINE REJOUABLE

Chaque étape doit être indépendante et idempotente.

Exemple :

```text
audio
 ↓
transcription
 ↓
diarisation
 ↓
speaker matching
 ↓
classification
 ↓
extraction
 ↓
export
```

Si on change uniquement le modèle de diarisation :

```text
audio
 ├── transcription existante → CONSERVÉE
 │
 └── nouvelle diarisation → REJOUÉE
```

Si on change le LLM :

```text
transcription existante → CONSERVÉE
speaker existants       → CONSERVÉS
LLM extraction          → REJOUÉE
```

---

# 20. INTERFACE HUMAINE

Le pipeline est entièrement automatisé en arrière-plan.

À terme, une petite interface web locale.

Elle ne doit pas être obligatoire pour le fonctionnement normal.

Elle sert uniquement à :

```text
nouveaux profils vocaux
matchs ambigus
corrections
classification manuelle
contexte utilisateur
état du pipeline
```

Exemple :

```text
Nouveau profil détecté

Profil 135

[ écouter ]

Libellé :
[________________]

[ Valider ]
```

Une fois validé :

```text
speaker_YYYY → libellé "invité"
```

L'interface peut être utilisée depuis n'importe quel navigateur du réseau
local ; le moteur reste sur la machine qui exécute le pipeline.

---

# 21. SORTIES FINALES

Une fois les données propres, les sorties sont déposées dans le socle de notes
(`MEMORYBRAIN_SOCLE`).

| Classe | Ce qu'elle contient | Où cela atterrit |
|---|---|---|
| `PERSONNEL` | idées, notes, décisions dictées par le porteur du micro | fiche markdown sous `config.WIKI` (`concepts/idees`, `concepts/notes`, `actions/todos`) |
| `BUSINESS` | idées et tâches relevant d'une activité professionnelle | même mécanique de fiches, aucun dossier réservé |
| `MEDIA` | note d'écoute du contenu externe : de quoi ça parlait, à quelle heure | `wiki/raw/ecoutes/AAAA-MM-JJ-ecoutes.md` |
| `UNKNOWN` | groupe non classé : rien n'est extrait sans marqueur oral | — |

Deux écritures coexistent, et il faut savoir laquelle on utilise :

- `interface/fiches.py` écrit une fiche par item (`Idee-…md`, `Note-…md`,
  `Todo-…md`) dans `config.WIKI`, en mode `--dry-run` par défaut : rien n'est
  publié sans relecture ;
- `outils/ecrire_socle.py` range les mêmes items par année dans le socle
  (`dictees/idees-<année>/`, `dictees/notes-<année>/`, un fichier
  `TODO-AAAA-MM-JJ.md` par jour, et la projection `ecoutes/`).

Un groupe `MEDIA` ou `UNKNOWN` ne produit aucun item sans marqueur oral :
c'est un garde-fou (voir §27, contrôle `extraction`), pas un oubli.

---

# 22. STRUCTURE LOGIQUE CIBLE

```text
MemoryBrain
│
├── ingestion/
│
├── audio/
│   ├── original/
│   └── archive/
│
├── sessions/
│
├── chunks/
│
├── transcripts/
│
├── speakers/
│   ├── speaker_0000/
│   ├── speaker_0001/
│   └── ...
│
├── diarization/
│
├── classification/
│
├── extraction/
│   ├── ideas/
│   ├── todo/
│   ├── notes/
│   └── ...
│
├── database/
│   └── memorybrain.sqlite
│
└── interface/
```

La structure exacte pourra évoluer.
Les concepts doivent rester séparés.

---

# 23. INSTALLATION ET DÉPENDANCES

```bash
git clone <ce-dépôt> memorybrain
cd memorybrain
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dépendances système : Python 3.11+, `ffmpeg`, et `python3-venv`.

```bash
sudo apt install ffmpeg python3-venv
```

Création de la structure de travail et de la base :

```bash
python3 -c "import config; [d.mkdir(parents=True, exist_ok=True) for d in config.ALL_DIRS]"
python3 -c "import db; db.init()"
```

Modèles, tous optionnels au premier lancement :

- **transcription** : `faster-whisper` télécharge son modèle au premier usage
  (aucune donnée envoyée, seuls les poids sont récupérés) ;
- **diarisation** : pyannote demande un jeton Hugging Face (`HF_TOKEN`) ;
  l'alternative locale Nemotron-3-Diarization se déclare par
  `MEMORYBRAIN_NEMOTRON_DIAR` ;
- **extraction** : un serveur compatible OpenAI doit répondre sur
  `config.PORT_LLM`, servant le modèle `config.MODELE_LLM`. Exemple de
  lancement avec `llama-server` :

```bash
llama-server -m <modele>.gguf --port 8000 -c 4096 -np 1 --jinja \
             --temp 0.2 --top-p 0.95 --alias chocolatine-2-4b
```

`--port` doit valoir `config.PORT_LLM` (`MEMORYBRAIN_PORT`, 8000 par défaut) et
`--alias` doit valoir `config.MODELE_LLM` (`MEMORYBRAIN_LLM_MODELE`,
`chocolatine-2-4b` par défaut) : le client vérifie l'alias annoncé par
`GET /v1/models` et refuse de signer une extraction d'un autre modèle.

`-np 1` et une fenêtre de contexte bornée ne sont pas décoratifs : un serveur
qui réserve plusieurs slots multiplie la mémoire nécessaire et peut échouer
silencieusement. Si le serveur ne répond pas, l'étape d'extraction est notée
« sautée » — le pipeline n'invente rien à sa place.

Configuration : `cp .env.example .env`, puis `set -a; . ./.env; set +a` avant
de lancer une passe. Sans `.env`, tout fonctionne avec les valeurs par défaut
de `config.py` : sorties et quarantaine sous `~/.local/share/memorybrain`,
dossiers de travail dans le projet. Deux racines de sortie à ne pas confondre :
`config.SOCLE` (fiches par année, notes d'écoute, transcripts) et
`config.WIKI` (fiches de `interface/fiches.py`) ; surcharges
`MEMORYBRAIN_SOCLE` et `MEMORYBRAIN_WIKI`.

---

# 24. COMMANDES

Trois points d'entrée, à lancer depuis la racine du clone. Ils partagent la
même base et la même configuration.

| script | rôle |
|---|---|
| `build.py` | prépare la base : ingestion, sessions, transcription |
| `pipeline_v2.py` | la chaîne V2 en sept étapes nommées, avec un contrôle par étape |
| `passe.py` | exploitation : état, plage horaire, fiches, ménage |

```bash
# préparation
python3 build.py --ingest
python3 build.py --sessions
python3 build.py --status

# chaîne complète, ou une seule étape
python3 pipeline_v2.py --etape tout --n 3
python3 pipeline_v2.py --etape transcription --groupes 1,2,3
python3 pipeline_v2.py --status
python3 pipeline_v2.py --verifier

# exploitation
python3 passe.py --etat
python3 passe.py --plage AAAA-MM-JJTHH:MM:SS AAAA-MM-JJTHH:MM:SS
python3 passe.py --ecrire --jour AAAA-MM-JJ
python3 passe.py --compte-rendu
python3 passe.py --menage
```

`--plage` attend deux dates ISO 8601 (celles qu'accepte
`datetime.fromisoformat`), pas le format des noms de fichiers. `--dry-run`
montre ce qui serait fait sans rien faire.

---

# 25. ÉTAPES DE CONSTRUCTION

## PHASE 0 — NE PAS TOUCHER

Déjà fonctionnel :

```text
☑ acquisition
☑ synchronisation de fichiers
☑ ingestion
☑ fichiers audio
☑ VAD
☑ transcription
☑ manifest / hash
```

## PHASE 1 — TRANSCRIPT PROPRE

```text
audio
 ↓
transcript 5 min
 ↓
timestamps fiables
```

Corriger notamment : dates, timestamps, correspondance fichier audio ↔
transcript, ordre chronologique.

## PHASE 2 — DIARISATION

```text
audio
 ↓
Profil 1
Profil 2
Profil 3
```

Sans encore chercher à tout classer.

## PHASE 3 — IDENTIFICATION VOCALE

```text
Profil 0
 ↓
speaker_0000 (porteur)

Profil 2
 ↓
speaker_XXXX
```

Créer les profils inconnus.

## PHASE 4 — CONTINUITÉ ENTRE CHUNKS

```text
chunk 1 → speaker_XXXX
chunk 2 → speaker_XXXX
chunk 3 → speaker_XXXX
```

Même lorsque les chunks sont traités séparément.

## PHASE 5 — TRANSCRIPT SPEAKER-AWARE

```text
[00:00:03] Profil 1 :
...

[00:00:17] Profil 7 :
...
```

## PHASE 6 — MARQUEURS

```text
IDÉE
FIN DE L'IDÉE

TODO
FIN TODO
```

Le système découpe les blocs automatiquement.

## PHASE 7 — CLASSIFICATION

```text
PERSONNEL
MEDIA
BUSINESS
```

Puis catégories supplémentaires si nécessaire.

## PHASE 8 — EXTRACTION LLM

Le LLM reçoit des blocs déjà structurés :

```text
TYPE = IDEA

SPEAKER = speaker_0000

CONTEXTE = MEDIA

CONTENU :
...
```

Il produit uniquement la sortie attendue.

## PHASE 9 — INTERFACE WEB

Pour : profils inconnus, corrections, contexte, erreurs, état du pipeline.

## PHASE 10 — ARCHIVAGE

```text
WAV temporaire
 ↓
résultats validés
 ↓
compression
 ↓
suppression du WAV
```

---

# 26. JEU DE TEST / RÉGRESSION

Créer quelques fichiers de référence, tous fictifs :

```text
seul.wav
bruit.wav
porteur_plus_un.wav
porteur_plus_deux.wav
deux_autres.wav
trois_personnes.wav
video_une_voix.mp4
video_deux_voix.mp4
chevauchement.wav
longue_video.wav
```

Tester :

```text
transcription
diarisation
identification
UNKNOWN
continuité
classification
IDÉE
TODO
```

À chaque changement de modèle, rejouer ces tests.

---

# 27. CONTRÔLES PAR ÉTAPE (structure de vérification)

> **Une étape qui « a tourné » n'est pas une étape qui « a réussi ».**
> Chaque étape produit une donnée ; cette donnée est **contrôlée**
> avant que l'étape suivante ne démarre. Un contrôle rouge **arrête la
> chaîne** : on ne construit jamais l'étape 2 sur une étape 1 fausse.

Implémenté dans `verification/controles.py` (une fonction par étape),
branché dans `pipeline_v2.py`.

## Comment on lit un contrôle

| État | Signification | Action |
|---|---|---|
| `non_fait` | l'étape n'a pas encore tourné sur ce groupe | **pas un défaut** (règle 3), on continue |
| `OK` | l'étape a tourné ET la sortie tient | on passe à la suite |
| `PROBLEME` | l'étape a tourné ET la sortie est fausse | **on arrête**, on corrige |

Séparer `non_fait` de `PROBLEME` est volontaire : un rapport qui crie au
bug sur ce qui n'est pas encore passé produit du bruit, et on apprend à
l'ignorer — donc à rater le vrai bug.

## Les contrôles, étape par étape

Les noms d'étapes sont ceux que reconnaît `pipeline_v2.py --etape` :
`groupement`, `transcription`, `voix`, `alignement`, `classification`,
`markdown`, `extraction`.

| # | Étape | Ce qui est vérifié (et pourquoi) |
|---|---|---|
| 1 | `groupement` | chaque groupe a **exactement 5 chunks**, membres contigus et cohérents, WAV présent. *Un groupe mal formé fausse tout l'aval.* |
| 2 | `transcription` | segments présents, **horaires renseignés**, segments bornés dans leur chunk (débordement isolé toléré ; au-delà de 1 % = rouge). Zéro segment sur un groupe silencieux est noté, pas condamné : c'est du silence légitime |
| 3 | `voix` | un seul profil nominatif possible, le profil 0 (le porteur du micro) ; un tour `PROPRIETAIRE` doit porter `speaker_id = 0`, un tour `AUTRE` ne doit pas en porter ; un groupe classé « musique » ne peut pas contenir la voix du porteur. *C'est ce contrôle qui empêche de recréer des dizaines de profils pour une seule personne* |
| 4 | **`alignement`** | **aucun mot ne se perd** : les mots de la base sont exactement ceux des interventions du transcript. C'est LE contrôle : il révèle une perte de texte, invisible autrement |
| 5 | `classification` | chaque groupe traité porte une classe valide : `PERSONNEL`, `MEDIA`, `BUSINESS` ou `UNKNOWN` |
| 6 | `markdown` | le fichier livrable existe, est relu, et contient **exactement** les mots des segments — ni perdus, ni inventés (préfixes de locuteur exclus du décompte) |
| 7 | `extraction` | un groupe `MEDIA` ou `UNKNOWN` ne produit **aucun item sans marqueur oral** (garde-fou : on ne ramasse pas les idées d'un contenu écouté) ; et les items sont signés par le modèle déclaré dans `config.MODELE_LLM`, pas par un autre modèle resté sur le port |

## Comment on lance les contrôles

```bash
cd memorybrain
python3 pipeline_v2.py --verifier-etape tout          # tous les contrôles, rien ne tourne
python3 pipeline_v2.py --verifier-etape alignement    # un seul
python3 pipeline_v2.py --etape tout                   # chaîne complète AVEC portes automatiques
python3 pipeline_v2.py --etape tout --force           # diagnostic : continue malgré un rouge
```

`--etape tout` enchaîne les sept étapes **et** exécute le contrôle de
chacune ; si un contrôle est rouge, la chaîne s'arrête net et l'étape
fautive est nommée.

## Le contrôle qui compte le plus

L'**alignement** compare, mot à mot, le texte en base et le texte du
transcript. Toute autre erreur est visible à l'œil (un groupe à 4 chunks, une
voix inconnue) ; une phrase qui disparaît ne se voit pas. Le contrôle affiche
les mots exacts qui manquent.

---

# 28. DÉPANNAGE

| Symptôme | Cause probable | Ce qu'on fait |
|---|---|---|
| `format de nom non reconnu` à l'ingestion | le fichier ne commence pas par `AAAA-MM-JJ_HH-MM-SS` | vérifier `ingestion/naming.py` et le nom produit par votre source |
| un groupe reste hors de tout groupe | chunks ingérés sans `session_id` | relancer `build.py --sessions` avant le regroupement |
| « N tours, aucun attribué » | groupe sans parole (bruit, silence) : la diarisation invente des tours | normal ; le contrôle `--verifier` les signale, on ne les enrôle pas |
| profil 0 absent d'un rapport | la case n'a pas été traitée jusqu'aux profils | relancer `--etape voix` sur le groupe concerné |
| étape d'extraction « sautée » | aucun serveur ne répond sur `config.PORT_LLM`, ou il ne sert pas le modèle attendu | vérifier `GET /v1/models` ; démarrer `llama-server` avec l'`--alias` de `config.MODELE_LLM` |
| `--etape tout` s'arrête avant markdown | un contrôle rouge en amont : lire le nom de l'étape dans le message | corriger la cause ; `--force` pour voir la suite en diagnostic |
| un `git commit` est refusé | le hook a trouvé un motif (chemin, IP, métrique, secret) | lire le message ; corriger ; `no-fuite` sur une ligne seulement si c'est un faux positif |
| fichiers `…sync-conflict-AAAAMMJJ-HHMMSS-XXXXXXX.wav` | la synchronisation a renommé un fichier en conflit | l'ingestion reconnaît le motif et travaille sur le nom d'origine |
| deux copies d'un même audio | synchronisation qui duplique en `--1`, `--2` | l'ingestion reconnaît la copie : le hash évite le double comptage |

---

# 29. CRITÈRES DE RÉUSSITE

MemoryBrain V2 doit pouvoir :

```text
☑ recevoir automatiquement de l'audio
☑ ne pas retraiter inutilement un fichier
☑ transcrire
☑ découper en segments
☑ identifier les locuteurs
☑ conserver les identités entre chunks
☑ créer de nouveaux profils
☑ reconnaître les profils connus
☑ accepter UNKNOWN
☑ produire Profil X : texte
☑ utiliser les marqueurs IDÉE / TODO
☑ distinguer le contenu produit du contenu écouté
☑ conserver les interventions du porteur pendant un média
☑ fonctionner sans contexte utilisateur obligatoire
☑ permettre d'ajouter du contexte quand nécessaire
☑ rejouer une étape indépendamment
☑ changer de modèle sans casser les résultats existants
☑ archiver les audios lourds
☑ conserver les métadonnées nécessaires
☑ fonctionner entièrement localement
☑ nécessiter une intervention humaine uniquement lorsque nécessaire
```

---

# 30. PRINCIPE DIRECTEUR

> **Le système doit être intelligent là où l'intelligence apporte une vraie valeur, et déterministe partout ailleurs.**

```text
Python / outils
    ↓
font la plomberie

Diarisation
    ↓
dit QUI parle

Embeddings
    ↓
comparent les voix au profil de référence

Contexte + marqueurs
    ↓
disent CE QUI est important

LLM
    ↓
comprend / reformule / extrait

Socle de notes
    ↓
conserve la mémoire finale
```

L'objectif n'est pas de construire une IA qui devine tout.

L'objectif est de construire un **système de mémoire fiable**, dans lequel
chaque brique apporte une information précise et peut être remplacée
indépendamment.
