# Sécurité

MemoryBrain traite des enregistrements vocaux, des transcriptions et des
empreintes de voix. Ce document dit ce que le programme protège, comment le
dépôt s'en protège, et ce qui n'est pas protégé.

## Versions suivies

| Version | Support |
|---|---|
| 2.0.x (`PIPELINE_VERSION` / `SCHEMA_VERSION` = `2.0.0`) | corrections de sécurité |
| < 2.0 | non supportée |

## Signaler une vulnérabilité

N'ouvrez **pas** d'issue publique : elle exposerait le problème avant qu'il
soit corrigé.

Utilisez l'onglet **Security → Report a vulnerability** du dépôt GitHub
(rapport privé, visible seulement par les mainteneurs).

Décrivez : version, système, étapes de reproduction, effet observé.
Réponse attendue sous 7 jours.

## Ce que le dépôt ne contient pas

Le dépôt public contient le code du programme ; il ne contient pas les
données que ce programme produit. Concrètement, à la racine du dépôt :

- aucun fichier audio (`.wav`, `.ogg`, `.mp3`, `.m4a`, `.flac`, `.opus`) ;
- aucune transcription, aucune fiche markdown issue d'un corpus ;
- aucune base SQLite, aucune empreinte vocale (`.npy`, `.npz`) ;
- aucun fichier `.env`, aucune clé, aucun jeton ;
- aucun chemin de compte, aucune adresse IP privée ;
- aucun nom de personne, aucun nom de machine.

Ces points portent sur l'état de l'arbre de fichiers, et ils sont contrôlés
par les garde-fous décrits plus bas. Aucun contrôle automatique ne peut
prouver l'absence totale d'une chaîne dans l'historique Git d'un dépôt
existant : pour cette raison, les motifs recherchés sont volontairement
larges, et un signalement reste le bienvenu même si le fichier fautif a
depuis été supprimé.

Les données vivent sur la machine qui fait tourner le programme, dans des
dossiers exclus par `.gitignore`. Voir
[`docs/DONNEES-PERSONNELLES.md`](docs/DONNEES-PERSONNELLES.md).

## Contrôles automatiques

Deux garde-fous, deux familles différentes — un secret n'est pas une donnée
personnelle, et aucun outil ne couvre les deux :

| Contrôle | Ce qu'il refuse |
|---|---|
| `gitleaks` | jetons, clés, identifiants dans une URL, clé privée |
| `outils/verifier_confidentialite.py` | chemins de compte (Unix, Windows), chemins sous `~`, dossiers numérotés d'un disque personnel, identifiant d'appareil de synchronisation, identifiant de locuteur numéroté, nom d'hôte écrit en dur (option passée à un worker, libellé, forme `hôte@fichier`, domaine `.local`), adresses IP privées, point d'entrée de service local écrit en dur, décomptes de corpus (un effectif suivi d'un nom de corpus), volumes d'audio ou de calcul, dates de relevé, dispersion de mesures dans un commentaire, modèles de matériel identifiants, adresses e-mail |
| `git ls-files -ci --exclude-standard` | fichier ignoré qui reste malgré tout suivi par Git |
| Liste d'extensions | `.wav`, `.npy`, `.sqlite`, `.apk`, `.keystore`, `.p12`… |

Une fois le hook activé (§ ci-dessous), ces contrôles s'exécutent **avant
chaque commit**. Ils sont rejoués par la CI à chaque push et à chaque pull
request, y compris sur un clone où le hook n'a jamais été activé.

> **État réel :** deux barrières, et elles ne couvrent pas la même chose. Le
> hook local protège les commits d'un clone où il a été activé. Le workflow
> `.github/workflows/confidentialite.yml` rejoue les mêmes contrôles plus
> Gitleaks sur tout l'historique, mais seulement pour ce qui passe par GitHub
> (un push direct, ou une pull request). Rien ne protège un commit qui reste
> sur votre disque. Les motifs sont volontairement larges : un faux positif se
> contourne avec le marqueur `no-fuite` sur la ligne concernée, un faux négatif
> ne se rattrape pas.
>
> **Limite à connaître :** ces motifs décrivent des *formes* (un chemin, une
> IP, un effectif suivi d'un nom de corpus), pas des *valeurs*. Un chiffre
> écrit hors de ces formes — « 0,91 », une date sans le mot « mesuré » — passe
> sans alerte. Le garde-fou attrape les fuites de structure ; il ne remplace
> pas une relecture humaine avant publication.

## Activer le garde-fou local (une commande, une fois par clone)

Le hook est versionné dans `outils/hooks/`, avec le script qu'il appelle :
une seule source, mise à jour par `git pull`, sans copie à maintenir.

```bash
git config core.hooksPath outils/hooks
```

(La copie manuelle dans `.git/hooks/pre-commit` produit le même effet, mais
elle se désynchronise au premier `git pull` : c'est cette commande-là qu'il
faut retenir.)

Il s'exécute à chaque `git commit` et refuse le commit si un secret ou une
donnée personnelle est mis en scène. Il appelle aussi
`outils/verifier_confidentialite.py --staged` quand ce script est présent.

Les noms de personnes et de machines ne sont pas dans le fichier versionné —
un garde-fou qui liste ce qu'il protège le publie. Ils vivent dans un fichier
local, à créer vous-même et jamais publié : `.git/pre-commit-local.txt`, au
format `nom<TABULATION>expression régulière`. Un modèle commenté est versionné
dans `outils/hooks/pre-commit-local.example`. Absent, ce contrôle est
simplement inactif.

Lancer les contrôles à la main :

```bash
python3 outils/verifier_confidentialite.py      # tout le dépôt
python3 outils/verifier_confidentialite.py --staged
gitleaks dir . ; gitleaks git .
```

## Si un secret a été publié

Un dépôt public ne s'efface pas : un fichier supprimé reste dans l'historique
et dans les caches. L'ordre des opérations compte.

1. **Révoquer** le secret auprès du service concerné (jeton GitHub, clé d'API,
   mot de passe). C'est la seule mesure qui rend la fuite inoffensive.
2. Générer un remplaçant, stocké hors du dépôt (`.env`, gestionnaire de
   secrets).
3. Purger l'historique **seulement ensuite** : `git filter-repo`, ou
   suppression et recréation du dépôt. Voir `docs/DONNEES-PERSONNELLES.md`.
4. Prévenir les collaborateurs : chacun doit re-cloner, pas simplement
   `git pull`.

Une fois l'historique purgé, considérez que toute copie antérieure circule
encore : la révocation reste la mesure qui compte, pas la réécriture.

## Limites connues — à ne pas confondre avec des garanties

- **Aucun chiffrement au repos.** Les audios et la base SQLite sont en clair
  sur le disque : quiconque lit le disque lit les données.
- **Aucune authentification réseau.** Le serveur LLM et le hub de workers
  écoutent en local ; ne les exposez pas sur un réseau non maîtrisé.
- **La diarisation se trompe.** Elle attribue des propos à des tours de
  parole ; ce n'est pas une preuve d'identité et cela ne doit pas servir à en
  produire.
- **Les empreintes vocales sont des données biométriques** au sens du RGPD
  (article 9). Les héberger n'est pas anodin : voir le document dédié.
- **Les tests ne couvrent pas la sécurité.** Ils vérifient le comportement du
  pipeline, pas la robustesse à un adversaire.
