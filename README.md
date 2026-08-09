# Workflow IA

Scraping Instagram / TikTok → **Nano Banana Pro** → **Kling 3.0 Motion Control**, avec
interface web, validation manuelle et garde-fou budgétaire.

---

## Le pipeline

```
IG / TikTok / Pinterest              (ou tes propres fichiers)
   │                                            │
   ├─ 1. DÉCOUVERTE   Apify ou yt-dlp           │
   ├─ 2. TÉLÉCHARGE   httpx depuis le CDN       │ import direct
   │                                            │
   ├────────────────────┬───────────────────────┘
   │
   ├─ ⏸  VALIDATION   l'utilisateur choisit dans l'UI
   │
   ├─ 3. FRAME        ffmpeg : 4 candidates, scoring, filtres de recevabilité
   ├─ 4. ÉDITION      Nano Banana Pro (frame + image de réf + prompt)
   ├─ 5. MOTION       Kling 3.0 Motion Control (image générée + vidéo source)
   └─ 6. COLLECTE     téléchargement, galerie, export .zip
```

**SQLite est la source de vérité.** Chaque transition est committée avant la
suivante. Un crash, un `Ctrl+C` ou un redémarrage ne fait rien repayer : la
relance reprend exactement où ça s'est arrêté. Le `kling_task_id` est persisté
dès la soumission — si le serveur redémarre pendant une génération, le polling
reprend au lieu de resoumettre.

---

## Installation

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**ffmpeg est indispensable** (extraction des frames, préparation des vidéos) :

```bash
winget install Gyan.FFmpeg
```

Puis copier la configuration :

```bash
copy .env.example .env
```

---

## Configuration

| Clé | Où l'obtenir |
|---|---|
| `APIFY_TOKEN` | https://console.apify.com/account/integrations |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `KLING_ACCESS_KEY` / `KLING_SECRET_KEY` | https://app.klingai.com/global/dev/ |

### Le point à vérifier en premier : `PUBLIC_BASE_URL`

Kling télécharge la vidéo de référence depuis une **URL HTTPS publique**. Nos
fichiers sont sur disque. Deux modes :

- **`ASSET_HOST_MODE=local`** — l'app sert elle-même les fichiers sous
  `/public/<token>/…`. C'est le mode naturel sur un VPS. Il faut renseigner
  `PUBLIC_BASE_URL` avec l'adresse publique du serveur (ex.
  `https://workflow.mondomaine.com`). **Avec `localhost`, Kling échouera.**
- **`ASSET_HOST_MODE=source`** — on transmet l'URL CDN d'origine issue du
  scraping. Gratuit et instantané, mais ces URLs sont signées et expirent : à
  réserver au cas où la génération suit immédiatement le scraping.

### L'endpoint Kling Motion Control

```
KLING_MOTION_CONTROL_PATH=/v1/videos/motion-control
KLING_MODEL_NAME=kling-v3
```

⚠️ **C'est le seul paramètre que je n'ai pas pu vérifier** : la documentation de
Kling bloque la consultation automatisée. Le chemin ci-dessus suit la convention
de leurs autres endpoints vidéo, et l'authentification JWT est certaine.

**Vérifie-le en 10 secondes :** lance l'app, clique **Diagnostic**. Si la ligne
« Kling Motion Control » est rouge avec une erreur de chemin, corrige
`KLING_MOTION_CONTROL_PATH` dans `.env` d'après la doc de ton compte. Si c'est
un nom de champ qui est refusé, le dictionnaire `FIELDS` en haut de
[app/clients/kling.py](app/clients/kling.py) centralise toute la correspondance.

---

## Lancement

```bash
.venv\Scripts\python.exe run.py
```

Puis http://127.0.0.1:8000

### Paramètres par défaut

L'écran **Paramètres** (en haut à droite) mémorise ce qui ne change pas d'une
campagne à l'autre : **prompt Nano Banana Pro**, **image de référence**, réglages
Kling, filtres de scraping et plafond budgétaire. Chaque nouveau job en hérite,
et reste modifiable au cas par cas dans l'étape Génération.

### Pinterest

Colle l'URL d'un **tableau** (`pinterest.com/utilisateur/tableau`), d'un **pin**,
ou un lien court `pin.it` — les domaines nationaux (`fr`, `ca`, `de`…) sont
acceptés. Le préfixe `pinterest:` fonctionne aussi sur un chemin court
(`pinterest:utilisateur/tableau`).

Quatre différences avec Instagram et TikTok :

- **Pinterest passe toujours par yt-dlp**, même avec `SCRAPER_BACKEND=apify` :
  aucun acteur Apify n'est configuré pour lui. C'est gratuit et sans session.
- **Un profil nu ne fonctionne pas.** Il faut un tableau ou un pin : Pinterest
  n'expose pas le flux d'un profil, seulement celui d'un tableau.
- **Le filtre « vues minimum » est ignoré**, parce que Pinterest ne publie aucun
  compteur de vues. Les autres filtres (durée, date) s'appliquent normalement.
- **Un tableau mélange images et vidéos.** Les pins image sont écartés
  silencieusement ; sur un tableau surtout composé d'images, il faut donc en
  parcourir beaucoup pour trouver peu de vidéos, et le scraping prend plus de
  temps. Un tableau dédié aux vidéos est bien plus efficace.

### Prévisualiser avant de valider

Le bouton **▶** sur une vignette remplace celle-ci par un lecteur et joue la
vidéo en entier, sans quitter l'écran de validation. Piloter le lecteur ne coche
ni ne décoche la carte.

Une vidéo scrapée n'existe à ce stade que sous forme de métadonnées : seule sa
frame est sur disque. Le serveur rapatrie donc le fichier à la demande, **au
premier clic seulement**. C'est gratuit — aucune API facturée n'est sollicitée —
et le fichier atterrit là où le pipeline l'attend : la génération ne le
retéléchargera pas. L'état de la vidéo ne bouge pas, elle reste « à valider ».

En contrepartie, prévisualiser beaucoup de vidéos remplit `data/media/<job>/`
avec des sources que tu écarteras peut-être. Supprimer le job ne les efface pas :
la suppression ne retire que les lignes en base, les fichiers restent à effacer
à la main.

### Importer ses propres vidéos

Le scraping n'est pas obligatoire. **Créer sans scraper** (écran *Mes jobs*) crée
un job vide ; l'étape **Scraping** expose alors une zone de dépôt qui accepte
`mp4`, `mov`, `m4v`, `webm`, `mkv` et `avi`, jusqu'à **500 Mo par fichier**. Les
conteneurs autres que mp4 sont convertis à l'arrivée, et chaque fichier est sondé
immédiatement : durée, dimensions et vignette sont disponibles dès la validation,
donc l'estimation de coût est exacte au lieu de supposer le pire cas.

Ces vidéos rejoignent la file commune à partir de l'extraction de frame. Deux
différences avec les vidéos scrapées :

- **Les bornes de durée du scraping ne s'y appliquent pas** — ce sont des filtres
  éditoriaux, et tu as choisi ces fichiers délibérément. Restent les contraintes
  dures : 3 s minimum, ratio 0,4–2,5, frame exploitable, et le plafond appliqué à
  l'envoi vers Kling.
- **Aucun coût de scraping** dans l'estimation.

La zone de dépôt reste accessible sur un job scrapé : rien n'empêche de compléter
une campagne avec quelques fichiers à toi.

### Tester sans dépenser un centime

`DRY_RUN=true` dans `.env` : les vidéos sont fabriquées localement par ffmpeg,
aucun appel facturé n'est émis, et le pipeline se valide de bout en bout.

```bash
.venv\Scripts\python.exe smoke_test.py
```

Tests hors-ligne du Batch API (format JSONL, redimensionnement, décodage des
réponses y compris les refus de sécurité) :

```bash
.venv\Scripts\python.exe test_batch.py
```

---

## Durée de sortie

**Motion Control n'expose aucun paramètre de durée.** La vidéo générée fait
exactement la longueur de la vidéo de référence envoyée. Le seul réglage possible
est un **plafond** : au-delà, la source est tronquée avant l'envoi ; en dessous,
elle part intégralement.

L'API plafonne la référence à **30 s**.

L'orientation du personnage est fixée à `video` : le personnage reprend
mouvements, expressions **et orientation du corps** de la vidéo de référence.
C'est le mode qui autorise 30 s de référence.

Comme Kling facture **à la seconde produite**, la durée de chaque vidéo pilote
directement son coût. L'estimation dans l'UI utilise les durées réelles des
vidéos sélectionnées, pas un forfait.

## Coût par vidéo

Pour une sortie de **10 s** en Kling 3.0 Pro :

| Poste | Calcul | Coût |
|---|---|---|
| Scraping (amorti sur ~42 % de recevabilité) | | 0,003 $ |
| Nano Banana Pro | 1,47 × 0,134 $ | 0,197 $ |
| Kling 3.0 Motion Control | 1,54 × 10 s × 0,1134 $ | 1,746 $ |
| **Total** | | **≈ 1,95 $** |

Avec le **mode batch** activé, la ligne Nano Banana tombe à 0,099 $ → **≈ 1,85 $**.
En passant aussi Kling en `std` (720p) : **≈ 1,18 $**.

Les facteurs 1,47 et 1,54 sont les réessais : blocages de sécurité côté Gemini
(~15 %), rebuts qualité (~20 % sur l'image, ~35 % sur la vidéo à 10 s). **Ce sont
des estimations à valider sur tes propres données** — l'UI affiche la dépense
réelle en temps réel.

### Mode batch Nano Banana Pro — **−50 %**

Case à cocher dans l'étape Génération (ou par défaut dans **Paramètres**). Le
Batch API de Google facture **0,067 $ au lieu de 0,134 $** par image, en échange
d'un traitement asynchrone avec une cible à 24 h — souvent bien plus rapide.

Ça convient à ce pipeline : tu traites des centaines de vidéos scrapées, tu n'as
aucun besoin de réponse immédiate.

Ce que ça change concrètement :

- Le pipeline passe **en phases** : toutes les vidéos atteignent la frame, puis
  un seul lot part chez Google, puis Kling démarre. L'étape image perd son suivi
  vidéo par vidéo au profit d'un état « Batch en cours ».
- Les lots sont découpés par tranches de `GEMINI_BATCH_CHUNK_SIZE` (250 par
  défaut) : les résultats arrivent par paquets plutôt qu'en une seule fois.
- Les images d'entrée sont redimensionnées à 1280 px avant envoi. Sur une source
  2160×3840, ça fait passer le poids de plusieurs centaines de Ko à ~5 Ko, sans
  rien changer à ce que le modèle perçoit.
- **Le nom du batch est persisté dès la soumission.** Un batch soumis est déjà
  facturé : si le serveur redémarre, la relance reprend le polling au lieu de
  resoumettre.

**Les trois leviers qui comptent :**

1. **Le plafond de durée** — la sortie suit la source, donc raccourcir le plafond
   réduit la facture proportionnellement. 8 s au lieu de 12 s : −30 %.
2. **Mode `std` au lieu de `pro`** — 0,07 $/s contre 0,1134 $/s, soit −38 % sur
   90 % de la facture. Calibre en `std`, publie en `pro`.
3. **Le filtre de recevabilité** — chaque vidéo écartée avant les appels API,
   c'est ~1,95 $ non dépensé.

### Garde-fous

- **Plafond par job**, réglé dans **Paramètres**. Le pipeline s'arrête net et
  passe le job en pause. Rien ne redémarre tout seul. `MAX_SPEND_USD` dans
  `.env` sert de valeur initiale.
- **Bouton « Mettre en pause »** pendant la génération, avec **« Reprendre »**
  qui repart des paramètres mémorisés. La pause ne perd rien : les tâches déjà
  soumises à Kling continuent chez eux, et la reprise récupère leurs résultats
  sans repayer.
- La pastille en haut à droite affiche la dépense en temps réel, et passe en
  orange à 80 % du plafond.
- **Les blocages de sécurité ne sont jamais rejoués.** Renvoyer la même requête à
  Nano Banana Pro sur un visage refusé donne le même refus en consommant du
  crédit à chaque fois. Seules les erreurs réseau/5xx sont réessayées, 3 fois
  maximum avec backoff.

---

## Filtres de recevabilité

Appliqués **avant** tout appel payant, dans
[app/media.py](app/media.py) :

| Filtre | Raison |
|---|---|
| Durée < 3 s | Minimum imposé par Kling |
| Durée hors des bornes choisies | Filtre éditorial (paramètres de scraping) |
| Frame noire / saturée / plate | Fondu au noir, flash de transition |
| Ratio hors 0,4–2,5 | Bornes acceptées par Kling |

La première frame n'est pas prise brutalement à `t=0` : on extrait 4 candidates
(0 / 0,3 / 0,8 / 1,5 s), on les score sur l'exposition, le détail et la
colorimétrie, et on garde la meilleure.

---

## Structure

```
run.py                    point d'entrée
smoke_test.py             test bout-en-bout hors-ligne
app/
  config.py               configuration .env
  models.py               enums, états, schémas, tarifs
  db.py                   SQLite : machine à états, reprise sur crash
  events.py               bus SSE vers l'UI
  media.py                ffmpeg : download, probe, frames, cuts, filtres
  assets.py               exposition des fichiers en URLs publiques
  budget.py               plafond de dépense
  pipeline.py             orchestrateur asynchrone
  main.py                 API FastAPI
  clients/
    apify.py              scraping IG + TikTok, normalisation multi-acteurs
    gemini.py             Nano Banana Pro, classification des refus
    kling.py              JWT officiel, motion control, polling
  static/                 UI (HTML/CSS/JS, sans build)
data/                     base, médias, images de référence
```

---

## Points d'attention

- **TikTok sans filigrane** : le normaliseur privilégie `videoUrlNoWaterMark` et
  `downloadAddr`. Un logo TikTok incrusté sur la frame serait régénéré en
  charabia par Nano Banana Pro.
- **Refus sur les visages réels** : c'est le risque principal. Teste ton prompt
  sur 10 vidéos avant de lancer un batch. Les refus apparaissent en rouge dans
  le journal avec le `finish_reason`.
- **Acteurs Apify** : si tu changes d'acteur dans `.env`, ajoute son schéma
  d'entrée dans `_instagram_input` / `_tiktok_input`
  ([app/clients/apify.py](app/clients/apify.py)) — chaque acteur valide ses
  propres champs.
- **Scraping et CGU** : le scraping de contenus publics est contraire aux CGU des
  deux plateformes, et la réutilisation de vidéos de tiers soulève des questions
  de droit d'auteur et de droit à l'image. C'est ton appel.
