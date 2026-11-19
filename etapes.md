# Étapes MemoryBrain V2

Mémoire de travail : la liste des étapes, dans l'ordre, avec ce qui casse
quand on en saute une. Le code des étapes est dans `pipeline_v2.py` ; ce
fichier dit comment elles s'enchaînent et ce qu'il faut vérifier avant de
passer à la suivante.

Trois scripts, trois rôles — ils partagent la même base et la même
`config.py` :

| script | rôle |
|---|---|
| `build.py` | prépare la base : `--ingest`, `--sessions`, `--transcribe N` |
| `pipeline_v2.py` | la chaîne des étapes ci-dessous, un contrôle par étape |
| `passe.py` | exploitation : `--etat`, `--plage`, `--ecrire`, `--menage` |

Les noms d'étapes acceptés par `pipeline_v2.py --etape` sont `groupement`,
`transcription`, `voix` (diarisation + profils), `alignement`,
`classification`, `markdown`, `extraction`. Les huit étapes ci-dessous sont le
découpage conceptuel ; elles s'y ramènent.

---

## Étape 1 — Les fichiers arrivent sur le disque local

Les fichiers audio arrivent dans `syncthing-inbox/` par synchronisation depuis
une source d'enregistrement (téléphone ou autre). Sans fichier, rien à faire.

---

## Étape 2 — L'ingestion

Le script scanne le dossier, calcule le SHA-256 de chaque fichier et inscrit
une ligne `chunk` dans la base (`database/memorybrain.sqlite`).

Il ne copie rien, il ne déplace rien, il ne supprime rien.
Il enregistre un pointeur.

- Le SHA-256 identifie l'enregistrement d'origine et reste rapide sur un gros
  corpus.
- Il empêche le même fichier arrivé par deux routes de compter deux fois.
- Après regroupement, le fichier n'est plus le même : le hash ne peut plus
  jouer ce rôle. Donc c'est bien à l'arrivée.

Code : `build.py --ingest` → `ingestion/ingest.py`

---

## Étape 3 — Le regroupement par 5

Les chunks d'une même session sont concaténés 5 par 5 en un WAV de travail.
Un groupe ne mélange JAMAIS deux sessions.

**Le nom du fichier commence par le NUMÉRO DE GROUPE** :

```
g<N>_s<S>_AAAA-MM-JJ_HH-MM-SS_5ch.wav
 │     │                 └─ 5 chunks regroupés
 │     └─ session S et heure de début
 └─ numéro de groupe  ← c'est ça qu'on cherche
```

Avant, le nom commençait par `s<session>`, qui n'a **aucun rapport** avec le
numéro de groupe : impossible de s'y retrouver à l'œil. La table
`chunk_group` reste la source de vérité, mais le nom la reflète maintenant.

Outils (dossier `outils/`) :
- `groupe.py` — retrouver le fichier d'un groupe, ou l'inverse
  (`python3 outils/groupe.py 42`, `--vides`, `--liste`).
- `renommer_par_groupe.py` — renomme les fichiers existants + met à jour la
  base ; `--annuler` revient en arrière (plan : `work/renommage_groupes.tsv`).

Code : `grouping/group.py` → `nom_groupe()` et `construire_groupe()`.

⚠ **Ordre obligatoire** : `build.py --ingest` → `build.py --sessions` →
`pipeline_v2.py`. Le regroupement ne travaille QUE sur les chunks qui ont un
`session_id` : sans l'étape « sessions », les fichiers fraîchement ingérés
restent `session_id = NULL` et s'accumulent hors groupe. Il n'y a pas de
script qui fasse les trois à votre place — ce sont trois commandes.

⚠ **`--etape tout` s'arrête au premier contrôle rouge** (sauf `--force`), et
le message nomme l'étape en cause. Un groupe sans parole ne produit pas un
rouge : le contrôle le compte comme `non_fait` (règle 3), ce qui n'est pas un
défaut. `--force` sert au diagnostic : la chaîne continue malgré un rouge et
vous montre tout ce qui reste à corriger, au lieu de s'arrêter au premier.

---

## Étape 4 — La transcription

Whisper v3 large fast, avec `vad_filter=True` et `no_speech_threshold=0.6`.

⚠ Un groupe où Whisper ne trouve **aucun segment** (bruit de fond, silence,
clavier) est quand même marqué `transcrit` : le pipeline n'a pas de branche
« jeter ». La diarisation tourne alors sur du bruit et invente des tours
(d'où la mention « N tours, aucun attribué » dans les traces). Le contrôle
`--verifier` les signale.

Code : `transcription/transcribe_group.py`.

---

## Étape 5 — La diarisation

On sépare les voix d'une case de 5 minutes : « locuteur A, de t1 à t2 ».
C'est l'étape la plus lente de la chaîne ; elle tourne sur CPU ou GPU selon
la plateforme.

Code : `diarization/diarize.py`.

---

## Étape 6 — Les profils vocaux

Chaque locuteur identifié reçoit une empreinte, comparée au profil de
référence du porteur du micro (profil 0). Le profil 0 est réservé à la
personne qui enregistre ; les autres locuteurs sont numérotés à partir de 1 et
restent `UNKNOWN` tant qu'aucun nom ne leur est donné.

Les empreintes ne sont jamais versionnées : `.gitignore` exclut `speakers/`,
`*.npy` et `*.npz`.

Code : `speakers/`.

---

## Étape 7 — Markdown et classification

Le transcript devient un fichier markdown lisible, classé selon la nature du
contenu — classes `PERSONNEL`, `MEDIA`, `BUSINESS`, définies dans
`classification/`. Rien n'est publié ni déplacé hors des dossiers de travail et
du socle configuré.

Code : `interface/`, `classification/`.

---

## Étape 8 — L'extraction

Le LLM local (`config.MODELE_LLM`, servi par `llama-server`) transforme le
texte du locuteur principal en idées, tâches et notes. Les marqueurs oraux
(annonce d'ouverture ou de fermeture d'idée, demande explicite d'ajout de
tâche) priment sur la classification.

Code : `extraction/`.
