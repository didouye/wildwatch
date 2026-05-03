# BirdyPhotobooth

Piège photographique automatique pour animaux sauvages, basé sur un Raspberry Pi et une caméra infrarouge.

Le système capture automatiquement des photos quand un mouvement est détecté, puis les envoie à un serveur web pour consultation, tri et identification des espèces.

## Architecture

Le projet se compose de deux parties :

- **BirdyCapture** (RPi) : client de capture autonome, détecte les mouvements et envoie les photos
- **BirdyServer** (VPS) : serveur web qui réceptionne, stocke et expose les photos via une interface web

Communication unidirectionnelle : le RPi pousse les photos vers le serveur via HTTPS. Le serveur ne contacte jamais le RPi.

## Matériel

| Composant | Modèle | Notes |
|-----------|--------|-------|
| SBC | Raspberry Pi 2 v1.1 | Quad-core ARM Cortex-A7 @ 900MHz, 1 Go RAM |
| OS | DietPi (dernière version) | Distribution légère optimisée pour RPi |
| Caméra | Camera Module 3 NoIR | 12MP, autofocus, sans filtre IR (vision nocturne) |
| Réseau | Dongle WiFi USB | Nécessaire car le RPi 2 n'a pas de WiFi intégré |
| Alimentation | Secteur (V1) | Batterie prévue pour une version ultérieure |

### Accès au RPi

```
ssh dietpi@dietpi.local
```

## Stack technique

| Composant | Technologies |
|-----------|-------------|
| Capture (RPi) | Python, picamera2, libcamera, uv |
| Serveur | Python, FastAPI, SQLite, Jinja2, htmx, uv |
| Déploiement serveur | Docker Compose, Caddy (reverse proxy HTTPS) |
| Identification espèces | Google SpeciesNet (prévu V2+) |

## Structure du projet

```
birdyphotobooth/
├── capture/                    # BirdyCapture (code RPi)
│   ├── pyproject.toml
│   ├── src/
│   │   └── birdy_capture/
│   │       ├── __init__.py
│   │       ├── main.py         # Point d'entrée, boucle principale
│   │       ├── camera.py       # Interface picamera2
│   │       ├── motion.py       # Détection de mouvement
│   │       ├── uploader.py     # Envoi des photos au serveur
│   │       └── config.py       # Lecture config TOML
│   └── systemd/
│       └── birdy-capture.service
├── server/                     # BirdyServer (code serveur)
│   ├── pyproject.toml
│   ├── src/
│   │   └── birdy_server/
│   │       ├── __init__.py
│   │       ├── main.py         # App FastAPI
│   │       ├── api/            # Routes API
│   │       ├── models.py       # Modèles SQLite
│   │       ├── storage.py      # Gestion fichiers photos
│   │       └── templates/      # Templates Jinja2
│   ├── static/                 # CSS/JS
│   └── Dockerfile
├── docker-compose.yml
├── README.md
└── ROADMAP.md
```

## Fonctionnement

### BirdyCapture (RPi)

1. La caméra tourne en mode preview basse résolution (640x480)
2. Un algorithme compare les frames successives pour détecter les mouvements
3. Quand un mouvement est confirmé, une rafale de photos est capturée en haute résolution (4608x2592)
4. Les photos sont stockées localement dans `/var/spool/birdy/queue/`
5. Un processus d'upload envoie les photos au serveur via HTTP POST
6. En cas de perte WiFi, les photos restent en file d'attente et sont réessayées automatiquement
7. Configuration via `/etc/birdy/config.toml`

### BirdyServer (VPS)

- Réceptionne les photos via l'API REST (authentification par clé API)
- Génère des thumbnails automatiquement (150px, 400px, 800px)
- Stocke les photos sur le système de fichiers, organisées par date
- Expose une interface web pour consulter, filtrer, taguer et partager les photos
- Métadonnées stockées en SQLite

### API

```
POST   /api/photos           Upload photo + métadonnées
GET    /api/photos           Liste des photos (pagination, filtres)
GET    /api/photos/{id}      Détail d'une photo
GET    /api/photos/{id}/file Téléchargement de l'image
PATCH  /api/photos/{id}      Mise à jour métadonnées (tags, espèce, favoris)
DELETE /api/photos/{id}      Suppression
GET    /api/stats            Statistiques
```

## Sécurité

- Communication RPi → Serveur en HTTPS
- Authentification API par clé (`Authorization: Bearer <token>`)
- Interface web protégée par login/mot de passe
- Rate limiting sur l'endpoint d'upload

## Licence

TODO
