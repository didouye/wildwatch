# BirdyPhotobooth — Design initial

Date : 2026-05-04

## Contexte

Piège photographique automatique pour animaux sauvages. Un Raspberry Pi équipé d'une caméra infrarouge capture des photos quand un mouvement est détecté, puis les envoie à un serveur web.

## Contraintes matérielles

- RPi 2 v1.1 (pas de WiFi intégré, 1 Go RAM, quad-core 900MHz)
- Camera Module 3 NoIR (12MP, autofocus, sans filtre IR)
- DietPi OS
- Dongle WiFi USB pour la connectivité
- V1 sur secteur, batterie envisagée plus tard
- Pas de capteur PIR pour la V1 (détection logicielle)
- Pas d'illuminateur IR pour la V1 (lumière ambiante)
- Accès SSH : `dietpi@dietpi.local`

## Décisions techniques

| Décision | Choix | Raison |
|----------|-------|--------|
| Langage RPi | Python | picamera2 n'existe qu'en Python. Gain Rust négligeable car la conso vient de la caméra hardware |
| Langage serveur | Python (FastAPI) | Stack homogène. SpeciesNet est en Python |
| Gestion dépendances | uv | Rapide, moderne, standard Python |
| Détection mouvement | Logicielle (comparaison de frames) | Pas de PIR disponible. Acceptable car V1 sur secteur |
| Base de données | SQLite | Léger, pas de serveur DB à gérer. Migration PostgreSQL possible |
| Frontend | Jinja2 + htmx | Léger, pas de build frontend, rendu serveur |
| Déploiement serveur | Docker Compose + Caddy | HTTPS automatique, simple à maintenir |
| Identification espèces | SpeciesNet (V2+) | Open source, 2000+ espèces, tourne en local |

## Architecture

Deux composants indépendants communiquant via HTTP unidirectionnel (RPi → serveur).

### BirdyCapture (RPi)

- Boucle de surveillance avec picamera2
- Preview basse résolution (640x480) pour la détection
- Capture haute résolution (4608x2592) quand mouvement confirmé
- Rafale de 3-5 photos par événement
- File d'attente locale avec retry automatique
- Configuration TOML
- Service systemd

### BirdyServer (VPS)

- API REST FastAPI (upload, CRUD, stats)
- Stockage fichiers par date
- Thumbnails automatiques (150px, 400px, 800px)
- Interface web galerie (Jinja2 + htmx)
- Auth clé API (RPi) + login/mdp (web)
