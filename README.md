# WildWatch

Piège photographique automatique pour animaux sauvages, basé sur un Raspberry Pi et une caméra infrarouge.

Le système capture automatiquement des photos quand un mouvement est détecté, puis les envoie à un serveur web pour consultation, tri et identification des espèces.

## Architecture

Le projet se compose de deux parties :

- **wildwatch-capture** (RPi) : client de capture autonome, détecte les mouvements et envoie les photos
- **wildwatch-server** (VPS) : serveur web qui réceptionne, stocke et expose les photos via une interface web

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
wildwatch/
├── capture/                       # wildwatch-capture (code RPi)
│   ├── pyproject.toml
│   ├── config.toml.example
│   ├── src/
│   │   └── wildwatch_capture/
│   │       ├── __init__.py
│   │       ├── main.py            # Point d'entrée, boucle principale
│   │       ├── camera.py          # Wrapper picamera2 (preview + switch_mode)
│   │       ├── motion.py          # Détection par background subtraction
│   │       ├── uploader.py        # File d'attente + envoi HTTP
│   │       └── config.py          # Lecture config TOML
│   ├── tests/
│   └── systemd/
├── server/                        # wildwatch-server (code serveur)
│   ├── pyproject.toml
│   ├── src/
│   │   └── wildwatch_server/
│   │       ├── __init__.py
│   │       └── main.py            # App FastAPI
│   └── static/
├── docs/
│   ├── SETUP-RPI.md               # Procédure complète + galères/contournements
│   └── plans/
├── _recovery/
│   └── setup_rpi.sh               # Script setup auto RPi
├── README.md
└── ROADMAP.md
```

## Démarrage rapide

Voir **[docs/SETUP-RPI.md](docs/SETUP-RPI.md)** pour la procédure complète d'installation sur le RPi (flash DietPi, config, déploiement) et la liste des galères rencontrées avec leurs contournements.

## Fonctionnement

### wildwatch-capture (RPi)

1. La caméra tourne en config preview basse résolution (640×480 YUV420)
2. La luminance est passée à un détecteur de mouvement par background subtraction adaptatif
3. Quand un mouvement est confirmé, picamera2 bascule en config still pour capturer une rafale en 2304×1296
4. Les photos sont stockées localement dans `~/wildwatch/queue/`
5. Un processus d'upload envoie les photos au serveur via HTTP POST, déplace les photos envoyées vers `~/wildwatch/sent/`
6. En cas de perte WiFi, les photos restent en file d'attente et sont réessayées automatiquement
7. Configuration via `~/wildwatch/config.toml`

### wildwatch-server (VPS)

- Réceptionne les photos via l'API REST (authentification par clé API à venir)
- Génère des thumbnails automatiquement (à venir : 150px, 400px, 800px)
- Stocke les photos sur le système de fichiers, organisées par date
- Expose (à venir) une interface web pour consulter, filtrer, taguer et partager les photos

### API

```
POST   /api/photos           Upload photo + métadonnées
GET    /api/photos           Liste des photos (pagination, filtres) — V0.4
GET    /api/photos/{id}      Détail d'une photo — V0.4
GET    /api/photos/{id}/file Téléchargement de l'image — V0.4
PATCH  /api/photos/{id}      Mise à jour métadonnées (tags, espèce, favoris) — V0.5
DELETE /api/photos/{id}      Suppression — V0.4
GET    /api/stats            Statistiques — V0.5
```

## Sécurité

- Communication RPi → Serveur en HTTPS (V1.0)
- Authentification API par clé (`Authorization: Bearer <token>`) (V0.3)
- Interface web protégée par login/mot de passe (V0.4)
- Rate limiting sur l'endpoint d'upload (V1.0)

## Licence

TODO
