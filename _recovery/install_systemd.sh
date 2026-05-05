#!/usr/bin/env bash
# Install wildwatch-capture as a systemd service on the RPi.
# Run on the RPi AFTER the code is deployed and the venv exists:
#   ssh dietpi@dietpi.local 'bash -s' < _recovery/install_systemd.sh
#
# Idempotent.

set -euo pipefail

UNIT_SRC="$HOME/wildwatch-src/capture/systemd/wildwatch-capture.service"
UNIT_DEST="/etc/systemd/system/wildwatch-capture.service"
VENV_BIN="$HOME/wildwatch-src/capture/.venv/bin/wildwatch-capture"
CONFIG_FILE="$HOME/wildwatch/config.toml"

echo "=== Pre-checks ==="
[ -f "$UNIT_SRC" ] || { echo "x unit file missing: $UNIT_SRC"; exit 1; }
[ -x "$VENV_BIN" ] || { echo "x executable missing: $VENV_BIN (run uv sync?)"; exit 1; }
[ -f "$CONFIG_FILE" ] || { echo "x config missing: $CONFIG_FILE"; exit 1; }
echo "ok unit file, venv and config present"

echo "=== Install unit file ==="
sudo cp "$UNIT_SRC" "$UNIT_DEST"
sudo systemctl daemon-reload

echo "=== Enable + (re)start ==="
# `enable --now` is a no-op when the service is already running, which would
# leave a re-install with stale config in memory. Always restart to pick up
# any config.toml or unit-file changes.
sudo systemctl enable wildwatch-capture.service
sudo systemctl restart wildwatch-capture.service

echo "=== Status ==="
sleep 2
sudo systemctl status wildwatch-capture --no-pager --lines 5 || true

echo ""
echo "Follow logs in real time:"
echo "  sudo journalctl -u wildwatch-capture -f"
echo ""
echo "Stop / restart:"
echo "  sudo systemctl stop wildwatch-capture"
echo "  sudo systemctl restart wildwatch-capture"
