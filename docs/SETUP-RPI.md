# Setup RPi — Guide complet et galères rencontrées

Ce document couvre l'installation complète de wildwatch-capture sur un Raspberry Pi
2 v1.1 avec DietPi et Camera Module 3 NoIR. Il regroupe toutes les étapes
chronologiques, les pièges rencontrés en V0.1/V0.2 et leurs contournements,
afin de pouvoir reproduire l'installation rapidement.

## Matériel requis

| Composant | Modèle utilisé | Notes |
|-----------|----------------|-------|
| SBC | Raspberry Pi 2 v1.1 | BCM2836, ARMv7, 1 Go RAM |
| Caméra | Camera Module 3 NoIR | Capteur IMX708, CSI-2 |
| Stockage | microSD 32 Go | Format FAT32 + ext4 |
| Réseau | Dongle WiFi USB | Le RPi 2 n'a pas de WiFi intégré |
| Alimentation | 5 V / 2 A micro-USB | |

## 1. Flash de DietPi

1. Télécharger Raspberry Pi Imager (`brew install --cask raspberry-pi-imager`).
2. Choisir « Raspberry Pi 2 ».
3. OS : « Other specific-purpose OS » → « DietPi » → version pour RPi 1/2/3/4 (ARMv7).
4. Storage : la microSD.
5. Ne pas appliquer de customisation OS dans Imager — on fait la configuration via les fichiers DietPi.
6. Write et attendre la fin (5-10 min).

Une fois flashé, la partition de boot (FAT32) se monte sur Mac sous `/Volumes/NO NAME/` (ou `bootfs` selon les versions).

## 2. Configuration pré-boot sur la SD

Avant le premier boot, éditer ces fichiers sur la partition FAT32 :

### `dietpi-wifi.txt`

Décommenter et renseigner :

```
aWIFI_SSID[0]='ton_ssid'
aWIFI_KEY[0]='ton_mot_de_passe'
```

### `dietpi.txt`

```ini
AUTO_SETUP_NET_WIFI_ENABLED=1
AUTO_SETUP_NET_WIFI_COUNTRY_CODE=FR     # ou ton code pays
AUTO_SETUP_LOCALE=fr_FR.UTF-8
AUTO_SETUP_KEYBOARD_LAYOUT=fr
AUTO_SETUP_TIMEZONE=Europe/Paris
AUTO_SETUP_NET_HOSTNAME=DietPi          # défaut, peut rester
AUTO_SETUP_HEADLESS=1                   # important : pas d'écran
AUTO_SETUP_AUTOMATED=1                  # install non-interactive
SURVEY_OPTED_IN=0
```

### `config.txt`

Section caméra :

```
#-------RPi camera module-------
#start_x=1
camera_auto_detect=1
dtoverlay=imx708
#disable_camera_led=1
```

Section GPU memory (⚠️ critique, voir galère #4 plus bas) :

```
gpu_mem_1024=96
```

Ne PAS toucher à `cmdline.txt` (voir galère #1).

Éjecter proprement la SD (Cmd-E sur Mac), insérer dans le RPi, brancher l'alim.

## 3. Premier boot

Premier boot DietPi : 5 à 10 minutes (config initiale, install paquets, génération clés SSH, reboot autonome).

Pour suivre la disponibilité depuis le Mac :

```bash
until ping -c 1 -W 1000 dietpi.local >/dev/null 2>&1; do sleep 5; done
echo "RPi up"
```

Si `dietpi.local` ne résout pas après quelques minutes, voir galère #6 (avahi pas installé par défaut) — il faut alors trouver l'IP via le routeur ou un scan ARP :

```bash
arp -a | grep "b8:27:eb"   # OUI Raspberry Pi Foundation
```

## 4. Setup automatique

Une fois SSH disponible, lancer le script depuis le Mac :

```bash
ssh dietpi@dietpi.local 'bash -s' < _recovery/setup_rpi.sh
```

Ce script (`_recovery/setup_rpi.sh`) fait dans l'ordre :

1. `apt update` + install des paquets (`rpicam-apps`, `python3-picamera2`, `avahi-daemon`, `libnss-mdns`, `rsync`, `curl`, `ca-certificates`).
2. Activation `avahi-daemon` (mDNS).
3. Suppression des blacklists DietPi `dietpi-disable_rpi_camera.conf` et `dietpi-disable_rpi_codec.conf` (galère #2).
4. Réglage `gpu_mem_1024=96` si encore à `16` (galère #4).
5. Ajout de l'utilisateur `dietpi` aux groupes `video` et `render`.
6. Installation de `uv` via le script officiel Astral.
7. Création des dossiers `~/wildwatch-src/`, `~/wildwatch/queue/`, `~/wildwatch/sent/`.

Si le script a touché `gpu_mem_1024`, rebooter manuellement après :

```bash
ssh dietpi@dietpi.local 'sudo /sbin/reboot'   # voir galère #5 (systemctl reboot)
```

## 5. Déploiement du code

Depuis la racine du repo :

```bash
rsync -av --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.uv-cache' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='data/' --exclude='_recovery/' --exclude='.git/' \
  ./ dietpi@dietpi.local:~/wildwatch-src/
```

Création du venv côté RPi (⚠️ `--system-site-packages` impératif, voir galère #3) :

```bash
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  ~/.local/bin/uv venv --system-site-packages --python /usr/bin/python3
  ~/.local/bin/uv sync --no-dev --active
'
```

Vérifier les imports :

```bash
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  .venv/bin/python -c "
import numpy, picamera2, httpx
from wildwatch_capture.camera import Camera
from wildwatch_capture.motion import MotionDetector
print(\"OK\")
"
'
```

## 6. Configuration runtime

Créer `~/wildwatch/config.toml` sur le RPi (voir `capture/config.toml.example` à la racine du repo). Au minimum, mettre à jour `[upload].server_url` avec l'IP de ton serveur.

```toml
[upload]
server_url = "http://192.168.0.21:8000"  # IP du Mac/serveur
```

## 7. Lancement

```bash
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  .venv/bin/wildwatch-capture --log-level INFO
'
```

Logs attendus :

```
Config chargée depuis /home/dietpi/wildwatch/config.toml
Serveur cible : http://192.168.0.21:8000
Démarrage de la caméra
Boucle de surveillance démarrée
```

Ensuite quand un mouvement est détecté :

```
Mouvement détecté (score=0.140), capture rafale
Queued /home/dietpi/wildwatch/queue/...
Rafale terminée : 3 photo(s) enqueue(s)
Uploaded ... -> data/photos/...
3 photo(s) envoyée(s) au serveur
```

---

## Galères rencontrées et contournements

### Galère #1 — `cma=256M` dans `cmdline.txt` brick le boot

**Symptôme** : après ajout de `cma=256M` à `/boot/firmware/cmdline.txt` et reboot, le RPi 2 v1.1 ne boote plus. LED rouge fixe seule, verte éteinte → kernel panic très précoce, AVANT tout userspace. Pas de récupération possible via SSH.

**Cause** : `cma=256M` est trop élevé pour les structures mémoire du BCM2836 sur RPi 2 v1.1 et provoque un kernel panic au moment où le kernel essaie de réserver le pool CMA.

**Récupération** : retrait physique de la SD, montage sur Mac, édition de `cmdline.txt` pour retirer le paramètre, ré-insertion. Mais comme on a aussi débranché à chaud le RPi en panique, le rootfs ext4 a été corrompu. Il a fallu reflasher complètement DietPi.

**Leçon** : ne JAMAIS toucher à `cmdline.txt` sur RPi 2 v1.1 sans plan de récupération facile (et même comme ça, c'est risqué).

**Contournement pour le CMA** : on n'augmente plus le CMA. On adapte la résolution à ce qu'il permet (voir galère #7).

### Galère #2 — DietPi blackliste les modules caméra par défaut

**Symptôme** : `rpicam-hello --list-cameras` retourne `ERROR: rpicam-apps currently only supports the Raspberry Pi platforms.` alors que la machine est bien un RPi. `picamera2` lève `IndexError: list index out of range` car libcamera n'enregistre aucune caméra.

**Cause** : DietPi crée par défaut deux fichiers dans `/etc/modprobe.d/` qui blacklistent `bcm2835_isp` et `bcm2835_codec` :
- `/etc/modprobe.d/dietpi-disable_rpi_camera.conf`
- `/etc/modprobe.d/dietpi-disable_rpi_codec.conf`

Sans `bcm2835_isp`, le pipeline handler `rpi/vc4` de libcamera ne peut pas associer le sensor IMX708 à un device ISP, et abandonne sans erreur explicite.

**Contournement** : supprimer les deux fichiers, reboot.

```bash
sudo rm /etc/modprobe.d/dietpi-disable_rpi_camera.conf \
        /etc/modprobe.d/dietpi-disable_rpi_codec.conf
sudo /sbin/reboot
```

C'est intégré dans `_recovery/setup_rpi.sh`.

### Galère #3 — `picamera2` n'est pas pip-installable

**Symptôme** : `uv sync` essaie de compiler `numpy` 2.4.4 from source pour armv7l (pas de wheel disponible) et échoue.

**Cause** : `picamera2` est distribué uniquement via apt sur Debian (`python3-picamera2`). Il dépend de `numpy` (système) et de bindings C++ vers `libcamera` qui ne s'installent pas via pip.

**Contournement** :
- Installer `python3-picamera2` via apt — il tire `numpy` (2.2.4) du système.
- Créer le venv uv avec `--system-site-packages` pour que le venv hérite de `numpy` et `picamera2` du système.
- Ne PAS lister `numpy` dans `dependencies` côté `capture/pyproject.toml` (sinon uv tente de l'installer dans le venv, où il sera plus récent et masquera celui du système, ou échouera à compiler). Le mettre uniquement dans `dependency-groups.dev` pour les tests sur Mac.

### Galère #4 — `gpu_mem_1024=16` empêche le firmware caméra

**Symptôme** : modules kernel chargés, IMX708 détecté en dmesg, mais libcamera ne voit aucune caméra. `vcgencmd version` montre `(start_cd)`.

**Cause** : DietPi met `gpu_mem_1024=16` par défaut pour économiser RAM. Avec 16 Mo, le bootloader charge la variante `start_cd.elf` (cut-down) qui n'a pas le support caméra/ISP.

**Contournement** : régler `gpu_mem_1024=96` dans `/boot/firmware/config.txt` puis reboot. Le firmware passe alors à `start.elf` (complet, caméra OK). Visible dans `vcgencmd version` qui passe de `(start_cd)` à `(start)`.

### Galère #5 — `systemctl reboot` échoue avec « dbus-org.freedesktop.login1.service »

**Symptôme** :
```
Failed to set wall message, ignoring: Unit dbus-org.freedesktop.login1.service failed to load properly...
Call to Reboot failed: Unit dbus-org.freedesktop.login1.service failed to load properly...
```

**Cause** : un service systemd corrompu/incomplet sur DietPi minimaliste.

**Contournement** : utiliser `/sbin/reboot` directement plutôt que `systemctl reboot`. Le reboot s'effectue malgré le message d'erreur dbus.

### Galère #6 — `dietpi.local` ne résout pas

**Symptôme** : juste après reflash, `ssh dietpi@dietpi.local` échoue avec « cannot resolve hostname ».

**Cause** : DietPi minimaliste n'installe pas `avahi-daemon` ni `libnss-mdns` par défaut, donc pas de mDNS.

**Contournement** : trouver l'IP du RPi via le routeur ou un scan ARP (`arp -a | grep "b8:27:eb"`), puis :

```bash
ssh dietpi@<ip> 'sudo apt-get install -y avahi-daemon libnss-mdns && sudo systemctl enable --now avahi-daemon'
```

C'est intégré dans `_recovery/setup_rpi.sh`.

### Galère #7 — V4L2 force 4 buffers minimum

**Symptôme** : capture haute résolution échoue avec :
```
ERROR V4L2 v4l2_videodevice.cpp:1323 Unable to request 4 buffers: Cannot allocate memory
```

Même avec `picamera2.create_still_configuration(buffer_count=1)`.

**Cause** : le driver V4L2 du RPi (`bcm2835_unicam_legacy`) impose un minimum de 4 buffers, indépendamment de ce que picamera2 demande. Avec 4608×2592 BGR888 = 36 Mo par buffer, ça donne 144 Mo > 64 Mo CMA disponibles.

**Contournement** : capturer en 2304×1296 (3 MP). 4 × 9 Mo = 36 Mo, tient large dans les 64 Mo CMA. C'est largement suffisant pour identifier des animaux et compatible avec SpeciesNet.

### Galère #8 — `Path.replace()` échoue entre `/tmp` et `/home`

**Symptôme** : après capture, `OSError: [Errno 18] Invalid cross-device link`.

**Cause** : DietPi monte `/tmp` en tmpfs (RAM), donc `/tmp` et `/home/dietpi` sont sur des devices différents. `os.rename(2)` (utilisé par `Path.replace()`) ne sait pas faire de rename cross-device.

**Contournement** : utiliser `shutil.move()` qui fait copy + delete dans ce cas. Voir `capture/src/wildwatch_capture/uploader.py`.

### Galère #9 — Bug `rpicam-apps` v1.11.1 sur RPi 2 v1.1

**Symptôme** : `rpicam-still` se termine en code 0 sans produire de fichier. `rpicam-still --version` segfaults au shutdown après avoir affiché la version.

**Cause** : bug spécifique à `rpicam-apps` v1.11.1 sur cette combinaison RPi 2 v1.1 + Camera Module 3 + kernel 6.12 + Debian Trixie.

**Contournement** : ne pas utiliser les outils CLI `rpicam-*`. Utiliser `picamera2` (Python) directement, qui fonctionne parfaitement sur le même hardware. Notre code utilise déjà picamera2 exclusivement.

### Galère #10 — `scp` ne fonctionne pas sur DietPi

**Symptôme** : `scp file dietpi@dietpi.local:` échoue avec `bash: line 1: /usr/lib/sftp-server: No such file or directory`.

**Cause** : DietPi minimaliste n'installe pas `openssh-sftp-server`.

**Contournement** : utiliser `rsync` (installé par le setup script). `rsync` n'utilise pas sftp et passe par SSH directement.

### Galère #11 — Clé SSH du host change après reflash

**Symptôme** : `Host key verification failed` sur SSH après un reflash.

**Cause** : la nouvelle install a généré de nouvelles clés SSH host, mais le Mac a encore l'ancienne dans `~/.ssh/known_hosts`.

**Contournement** :

```bash
ssh-keygen -R dietpi.local
ssh-keyscan -H dietpi.local >> ~/.ssh/known_hosts
```

---

## Récapitulatif des fichiers critiques

| Fichier | Rôle |
|---------|------|
| `/boot/firmware/config.txt` | Config caméra + `gpu_mem_1024` |
| `/boot/firmware/cmdline.txt` | NE PAS TOUCHER |
| `/etc/modprobe.d/dietpi-disable_rpi_*.conf` | À supprimer |
| `~/wildwatch/config.toml` | Config runtime wildwatch-capture |
| `~/wildwatch-src/capture/.venv/` | venv uv avec system-site-packages |

## Reproduction rapide après reflash

Une fois la fresh DietPi flashée et le WiFi configuré :

```bash
# 1. Attendre que le RPi soit joignable
until ping -c 1 -W 1000 dietpi.local >/dev/null 2>&1; do sleep 5; done

# 2. Setup auto (apt + groupes + uv + avahi + blacklists + gpu_mem)
ssh dietpi@dietpi.local 'bash -s' < _recovery/setup_rpi.sh

# 3. Si gpu_mem a été modifié, reboot
ssh dietpi@dietpi.local 'sudo /sbin/reboot'
until ping -c 1 -W 1000 dietpi.local >/dev/null 2>&1; do sleep 5; done

# 4. Sync code
rsync -av --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.uv-cache' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='data/' --exclude='_recovery/' --exclude='.git/' \
  ./ dietpi@dietpi.local:~/wildwatch-src/

# 5. Setup venv
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  ~/.local/bin/uv venv --system-site-packages --python /usr/bin/python3
  ~/.local/bin/uv sync --no-dev --active
'

# 6. Créer ~/wildwatch/config.toml (manuel ou copier depuis capture/config.toml.example)

# 7. Lancer
ssh dietpi@dietpi.local 'cd ~/wildwatch-src/capture && .venv/bin/wildwatch-capture'
```
