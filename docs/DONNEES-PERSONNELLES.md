# Données personnelles

Ce que MemoryBrain manipule, où cela vit, ce qui peut sortir de la machine, et
comment effacer. Le dépôt public ne contient que du code.

## 1. Quelles données

| Donnée | Nature | Sensibilité |
|---|---|---|
| Audio brut | voix du porteur du micro, des personnes présentes et du contenu écouté | élevée |
| Transcriptions | contenu des propos | élevée |
| Empreintes vocales | vecteurs mathématiques calculés sur la voix | **biométrique** |
| Métadonnées | hash SHA-256, dates, durées, source, statut | moyenne |
| Sorties | idées, tâches, notes, fiches markdown | élevée |

Les empreintes vocales ne sont pas un identifiant anodin : ce sont des données
biométriques au sens de l'article 9 du RGPD (catégorie particulière). Les
héberger, les copier ou les publier n'a pas le même régime qu'un simple
fichier texte.

## 2. Où elles vivent

Deux familles de dossiers, toutes deux hors du dépôt au sens de Git :

```
syncthing-inbox/     audio entrant                  (ignoré par Git)
database/            base SQLite : chunks, segments, profils
work/                WAV de travail, traces, plans
audio/               audio original et archive
chunks/              cases de 5 minutes
transcripts/         transcripts bruts
diarization/         tours de parole
speakers/            profils et empreintes vocales   (ignoré par Git)
classification/      classe du contenu par groupe
extraction/          idées, tâches, notes extraites
```

et, hors du dossier du projet, sous `MEMORYBRAIN_SOCLE` (défaut
`~/.local/share/memorybrain`) :

```
dictees/             fiches markdown par année
wiki/raw/ecoutes/    notes d'écoute
wiki/raw/transcripts/ transcripts rangés par année
reference/           enregistrements de référence (MEMORYBRAIN_VOIX)
corbeille/           quarantaine datée (MEMORYBRAIN_CORBEILLE)
```

Le programme ne touche pas à vos fichiers d'origine, et ce qu'il retire de son
propre chef, ce sont ses fichiers de travail (les WAV concaténés d'un groupe,
supprimés une fois le groupe traité ; `GARDER_AUDIO_GROUPE = True` pour les
conserver). Tout le reste passe par la corbeille, datée, pour qu'on puisse
revenir en arrière.

## 3. Ce qui sort de la machine

Par défaut : **rien**.

- La transcription est locale (faster-whisper).
- La diarisation est locale.
- L'extraction des idées utilise un LLM servi en local (`llama-server` sur le
  port déclaré dans `config.PORT_LLM`).
- Aucune télémétrie, aucun appel réseau au démarrage.

Deux exceptions, explicites et sous votre contrôle :

1. **Téléchargement de modèles** depuis Hugging Face au premier lancement
   (aucune donnée envoyée, seulement des poids récupérés).
2. **Si vous configurez un LLM distant** à la place du modèle local, le texte
   des passages à extraire lui est envoyé. C'est un choix de configuration,
   pas un défaut.

## 4. Obligations à garder en tête (RGPD)

À valider avec un conseil juridique : ce qui suit décrit les questions à se
poser, pas une conformité garantie.

- **Enregistrer des tiers** (réunion, appel, lieu public) demande une
  information et, hors exceptions domestiques, une base légale.
- **Empreintes vocales** : catégorie particulière (art. 9), donc base légale
  renforcée et minimisation stricte.
- **Minimisation** : ne conserver que ce qui sert. Un audio traité peut
  souvent être supprimé une fois la fiche validée.
- **Durées de conservation** : à fixer explicitement. Une proposition
  raisonnable — audio brut : 30 jours après validation de la fiche ;
  empreintes : tant que le profil sert, révocables à tout moment ; sorties :
  selon l'usage.
- **Droits des personnes** : accès, rectification, effacement. Sur un corpus
  local, cela signifie pouvoir retrouver tous les passages d'une personne et
  les supprimer.

## 5. Purger

```bash
# 1. Vérifier qu'aucune donnée n'est suivie par Git
git ls-files | grep -Ei '\.(wav|mp3|m4a|ogg|flac|npy|npz|sqlite|db)$'   # doit être vide
git ls-files -ci --exclude-standard                                      # doit être vide

# 2. Contrôler le dépôt avant publication
python3 outils/verifier_confidentialite.py
gitleaks dir . && gitleaks git .
```

Effacer un corpus local est une opération destructrice : elle se fait avec
l'accord du propriétaire des données, jamais « par précaution de rangement ».
Un `rm` ne se rejoue pas.

## 6. Historique Git

Un fichier supprimé reste dans l'historique, les caches et les fourchettes
des autres clones. Pour un dépôt public, la seule méthode fiable est :

1. révoquer ce qui a fuité (si c'est un secret) ;
2. purger l'historique (`git filter-repo`), ou supprimer et recréer le dépôt
   avec un unique commit assaini ;
3. faire re-cloner les autres copies.

Une fois l'historique purgé, considérez que toute copie antérieure circule
encore : révoquez les secrets concernés, ils sont la seule partie de la fuite
qui puisse être neutralisée.

## 7. Limites

- Aucun chiffrement au repos : disque lu = données lues.
- Aucune gestion d'utilisateurs ni de droits par locuteur.
- Les empreintes ne sont pas protégées par mot de passe.
- Les sauvegardes et la synchronisation recopient tout, y compris les données
  sensibles : `speakers/`, `reference/`, `database/` et `dictees/` doivent
  être exclus des partages que vous ne maîtrisez pas.
