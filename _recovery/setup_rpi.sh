#!/usr/bin/env bash
# Automated setup script for WildWatch after a DietPi reflash.
# Run it on the RPi from your dev box:
#   ssh dietpi@dietpi.local 'bash -s' < _recovery/setup_rpi.sh
#
# Idempotent: safe to re-run on an already configured environment.

set -euo pipefail

echo "=== [1/5] apt update + WildWatch packages ==="
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

echo "=== [1bis/5] Enable avahi-daemon ==="
sudo systemctl enable --now avahi-daemon

echo "=== [1ter/5] Remove DietPi camera/codec blacklists ==="
# DietPi blacklists bcm2835_isp + bcm2835_codec by default, so libcamera
# cannot see the camera. We also bump gpu_mem_1024 to 96 so the firmware
# loads start.elf (full) instead of start_cd.elf (cut-down, no camera).
sudo rm -f /etc/modprobe.d/dietpi-disable_rpi_camera.conf \
           /etc/modprobe.d/dietpi-disable_rpi_codec.conf
if grep -q "^gpu_mem_1024=16" /boot/firmware/config.txt; then
  sudo sed -i "s|^gpu_mem_1024=16$|gpu_mem_1024=96|" /boot/firmware/config.txt
  echo "-> gpu_mem_1024 set to 96 (reboot required to apply)"
fi

echo "=== [2/5] Add dietpi user to video,render groups ==="
sudo usermod -aG video,render dietpi

echo "=== [3/5] Install uv ==="
if [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
"$HOME/.local/bin/uv" --version

echo "=== [4/5] Create project directories ==="
mkdir -p "$HOME/wildwatch-src"
mkdir -p "$HOME/wildwatch/queue" "$HOME/wildwatch/sent"

echo "=== [5/5] Setup complete ==="
echo "Next steps (run them from your dev box):"
echo "  1. rsync the code into ~/wildwatch-src/"
echo "  2. uv venv --system-site-packages + uv sync on the RPi"
echo "  3. create ~/wildwatch/config.toml"
echo "  4. start .venv/bin/wildwatch-capture"
echo ""
echo "Note: if 'dietpi' was just added to video/render, a new SSH session is"
echo "      required for the new groups to apply (logout/login)."
