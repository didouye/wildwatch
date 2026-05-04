#!/usr/bin/env bash
# Script de setup automatique pour WildWatch après reflash DietPi.
# À exécuter sur le RPi via :
#   ssh dietpi@dietpi.local 'bash -s' < _recovery/setup_rpi.sh
#
# Idempotent : peut être relancé sans casser un environnement déjà configuré.

set -euo pipefail

echo "=== [1/5] Mise à jour apt + paquets WildWatch ==="
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  rpicam-apps \
  python3-picamera2 \
  python3-pip \
  python3-venv \
  ca-certificates \
  curl \
  rsync \
  avahi-daemon \
  libnss-mdns

echo "=== [1bis/5] Activation avahi-daemon ==="
sudo systemctl enable --now avahi-daemon

echo "=== [1ter/5] Retrait des blacklists DietPi camera/codec ==="
# DietPi blackliste par défaut bcm2835_isp + bcm2835_codec → libcamera ne voit
# pas la caméra. On active aussi gpu_mem_1024=96 (firmware start.elf complet,
# pas la variante cut-down qui n'a pas le support caméra).
sudo rm -f /etc/modprobe.d/dietpi-disable_rpi_camera.conf \
           /etc/modprobe.d/dietpi-disable_rpi_codec.conf
if grep -q "^gpu_mem_1024=16" /boot/firmware/config.txt; then
  sudo sed -i "s|^gpu_mem_1024=16$|gpu_mem_1024=96|" /boot/firmware/config.txt
  echo "→ gpu_mem_1024 réglé à 96 (reboot nécessaire pour appliquer)"
fi

echo "=== [2/5] Ajout dietpi aux groupes video,render ==="
sudo usermod -aG video,render dietpi

echo "=== [3/5] Installation de uv ==="
if [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
"$HOME/.local/bin/uv" --version

echo "=== [4/5] Création du dossier projet ==="
mkdir -p "$HOME/wildwatch-src"
mkdir -p "$HOME/wildwatch/queue" "$HOME/wildwatch/sent"

echo "=== [5/5] Setup terminé ==="
echo "Prochaines étapes (à faire depuis le Mac) :"
echo "  1. rsync du code vers le RPi (~/wildwatch-src/)"
echo "  2. uv venv --system-site-packages + uv sync sur le RPi"
echo "  3. créer ~/wildwatch/config.toml"
echo "  4. lancer .venv/bin/wildwatch-capture"
echo ""
echo "NB: si dietpi vient juste d'être ajouté à video/render, il faut une nouvelle"
echo "session SSH pour que les groupes soient effectifs (logout/login)."
