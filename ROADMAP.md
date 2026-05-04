# Roadmap BirdyPhotobooth

## V0.1 — Setup et preuve de concept

L'objectif est de valider le matériel et la chaîne complète de bout en bout.

- [x] Initialiser le repo Git et la structure du monorepo
- [x] Configurer la caméra Module 3 NoIR sur DietPi (libcamera + dtoverlay imx708)
- [x] Ajouter l'utilisateur `dietpi` aux groupes `video` et `render`
- [x] Vérifier que la caméra capture une photo (via picamera2 — voir note ci-dessous)
- [x] Installer Python 3.13 + uv 0.11 sur le RPi
- [x] Écrire un script minimal qui capture une photo et l'envoie en HTTP à un endpoint de test
- [x] Côté serveur : endpoint FastAPI minimal qui reçoit et stocke une photo
- [ ] Tester le serveur FastAPI en local
- [ ] Déployer le code capture sur le RPi (clone + uv sync --system-site-packages)
- [ ] Valider la chaîne complète : RPi capture → HTTP POST → serveur stocke

### Notes V0.1

**Bug rpicam-apps v1.11.1 sur RPi 2 v1.1 + Camera Module 3** : les outils CLI
`rpicam-still` et `rpicam-jpeg` se terminent en code 0 sans produire de fichier
(et `rpicam-still --version` segfault à la sortie après avoir affiché la version).
La capture via `picamera2` (Python) fonctionne parfaitement. Comme notre code
utilise picamera2 directement, ce bug n'est pas bloquant. À surveiller dans les
mises à jour de `rpicam-apps`.

## V0.2 — Détection de mouvement

- [x] Détection par background subtraction adaptatif (numpy + picamera2 lores YUV)
- [x] Mode preview 640x480 + bascule `switch_mode_and_capture_file` vers 2304x1296
      pour les captures (contournement de la limite CMA 64 Mo du RPi 2 v1.1)
- [x] Seuils de déclenchement configurables (pixel_threshold, area_threshold)
- [x] Cooldown entre les captures (anti-spam, 5s par défaut)
- [x] Capture en rafale (3 photos par défaut, intervalle 0.5s)
- [x] Fichier de configuration TOML (`~/birdy/config.toml`, `/etc/birdy/` en V1.0)
- [x] Tests TDD du détecteur de mouvement (9 tests verts)
- [x] Validation bout en bout sur le RPi : détection → capture rafale → upload HTTP

### Notes V0.2

**CMA limité à 64 Mo sur RPi 2 v1.1** : la pleine résolution 4608×2592 est
inaccessible car le V4L2 driver alloue toujours 4 buffers minimum
(4 × 36 Mo > 64 Mo). On utilise 2304×1296 (3 MP, ~36 Mo) qui tient large.
Augmenter le CMA via `cma=256M` dans `cmdline.txt` cause un kernel panic au boot
sur cette plateforme. À explorer en V2+ : `dtoverlay=...,cma-size=...` dans
`config.txt` (plus sûr car le bootloader peut fallback).

**DietPi blackliste par défaut `bcm2835_isp` et `bcm2835_codec`** dans
`/etc/modprobe.d/dietpi-disable_rpi_camera.conf` et `dietpi-disable_rpi_codec.conf`.
Sans ces modules, libcamera ne voit pas la caméra. Le script `_recovery/setup_rpi.sh`
les supprime.

**Firmware variant** : avec `gpu_mem_1024=16` (défaut DietPi), le firmware utilise
`start_cd.elf` (cut-down) qui ne supporte pas la caméra. On force `gpu_mem_1024=96`
pour avoir le `start.elf` complet.

## V0.3 — Upload fiable et service systemd

- [ ] File d'attente locale (`/var/spool/birdy/queue/`)
- [ ] Upload avec retry automatique en cas d'échec réseau
- [ ] Nettoyage des photos envoyées après X jours
- [ ] Métadonnées JSON accompagnant chaque photo (timestamp, config capture, etc.)
- [ ] Service systemd pour BirdyCapture (démarrage au boot, restart on failure)
- [ ] Authentification par clé API

## V0.4 — Serveur web fonctionnel

- [ ] API REST complète (CRUD photos, pagination, filtres)
- [ ] Stockage photos organisé par date (`/data/photos/YYYY/MM/DD/`)
- [ ] Base SQLite pour les métadonnées
- [ ] Génération automatique de thumbnails (150px, 400px, 800px)
- [ ] Interface web : galerie avec pagination
- [ ] Interface web : vue détail d'une photo avec métadonnées
- [ ] Interface web : filtres par date
- [ ] Authentification web (login/mot de passe)

## V0.5 — Partage et gestion

- [ ] Tags manuels sur les photos
- [ ] Favoris
- [ ] Lien de partage public pour une photo
- [ ] Page de statistiques (photos/jour, activité par heure, etc.)
- [ ] Suppression en masse

## V1.0 — Déploiement production

- [ ] Dockerfile + docker-compose.yml pour le serveur
- [ ] Reverse proxy Caddy avec HTTPS automatique
- [ ] Script d'installation pour le RPi (dépendances, systemd, répertoires)
- [ ] Rate limiting sur l'API
- [ ] Documentation de déploiement
- [ ] Tests

---

## Futur (V2+)

### Identification automatique des espèces
- [ ] Intégrer Google SpeciesNet côté serveur
- [ ] Analyse automatique à la réception des photos
- [ ] Afficher l'espèce détectée et le score de confiance dans l'interface
- [ ] Filtrer les photos par espèce

### Optimisation batterie
- [ ] Ajouter un capteur PIR (HC-SR501 ou AM312) pour réveiller le RPi
- [ ] Mode veille basse entre les détections
- [ ] Désactiver le dongle WiFi entre les envois
- [ ] Mesurer et optimiser la consommation

### Vision nocturne
- [ ] Ajouter un illuminateur IR
- [ ] Basculer automatiquement jour/nuit selon la luminosité
- [ ] Ajuster les paramètres caméra pour la nuit (ISO, exposition)

### Multi-caméras
- [ ] Support de plusieurs RPi envoyant vers le même serveur
- [ ] Identifier chaque caméra dans l'interface
- [ ] Dashboard multi-caméras

### Alertes
- [ ] Notification en temps réel (email, Telegram, webhook) quand un animal est détecté
- [ ] Alertes configurables par espèce

### Vidéo
- [ ] Capture de courtes vidéos en plus des photos
- [ ] Streaming live (optionnel)
