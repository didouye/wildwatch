#!/usr/bin/env bash
# Installe wildwatch-capture comme service systemd sur le RPi.
# À exécuter sur le RPi APRÈS avoir déployé le code et créé le venv :
#   ssh dietpi@dietpi.local 'bash -s' < _recovery/install_systemd.sh
#
# Idempotent.

set -euo pipefail

UNIT_SRC="$HOME/wildwatch-src/capture/systemd/wildwatch-capture.service"
UNIT_DEST="/etc/systemd/system/wildwatch-capture.service"
VENV_BIN="$HOME/wildwatch-src/capture/.venv/bin/wildwatch-capture"
CONFIG_FILE="$HOME/wildwatch/config.toml"

echo "=== Vérifications préalables ==="
[ -f "$UNIT_SRC" ] || { echo "✗ unit file manquant: $UNIT_SRC"; exit 1; }
[ -x "$VENV_BIN" ] || { echo "✗ exécutable manquant: $VENV_BIN (uv sync ?)"; exit 1; }
[ -f "$CONFIG_FILE" ] || { echo "✗ config manquante: $CONFIG_FILE"; exit 1; }
echo "✓ unit file, venv et config présents"

echo "=== Installation du unit file ==="
sudo cp "$UNIT_SRC" "$UNIT_DEST"
sudo systemctl daemon-reload

echo "=== Activation + démarrage ==="
sudo systemctl enable --now wildwatch-capture.service

echo "=== Statut ==="
sleep 2
sudo systemctl status wildwatch-capture --no-pager --lines 5 || true

echo ""
echo "Pour suivre les logs en temps réel :"
echo "  sudo journalctl -u wildwatch-capture -f"
echo ""
echo "Pour arrêter / redémarrer :"
echo "  sudo systemctl stop wildwatch-capture"
echo "  sudo systemctl restart wildwatch-capture"
