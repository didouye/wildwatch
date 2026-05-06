#!/usr/bin/env bash
# WildWatch -- in-place RPi update.
#
# Usage on the RPi (or via SSH from your laptop):
#   curl -fsSL https://raw.githubusercontent.com/didouye/wildwatch/main/_recovery/update.sh | bash
#
# Preserves ~/wildwatch/config.toml (and therefore the camera token + server URL).
# If the install is missing entirely, this script exits with a clear error --
# in that case run setup.sh instead.

set -euo pipefail

REPO_URL="${WILDWATCH_REPO:-https://github.com/didouye/wildwatch.git}"
SRC_DIR="${WILDWATCH_SRC:-$HOME/wildwatch-src}"
CONFIG_DIR="${WILDWATCH_CONFIG_DIR:-$HOME/wildwatch}"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"

echo "==> WildWatch update"
echo "    Repo : $REPO_URL"
echo "    Src  : $SRC_DIR"
echo "    Cfg  : $CONFIG_DIR"
echo

if [[ ! -d "$SRC_DIR/.git" ]]; then
    echo "error: $SRC_DIR is not a git checkout. Use setup.sh for a fresh install." >&2
    exit 1
fi
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    echo "error: $CONFIG_DIR/config.toml not found. Use setup.sh for a fresh install." >&2
    exit 1
fi
if [[ ! -x "$UV_BIN" ]]; then
    echo "error: uv not found at $UV_BIN. Run setup.sh first to install uv." >&2
    exit 1
fi

echo "==> Step 1/6: pulling latest code"
( cd "$SRC_DIR" && git fetch --all --prune && git reset --hard origin/main )

echo "==> Step 2/6: refreshing capture venv"
( cd "$SRC_DIR/capture" && "$UV_BIN" sync --no-dev --active )

echo "==> Step 3/6: building/refreshing agent venv"
( cd "$SRC_DIR/agent" && "$UV_BIN" sync --no-dev --active )

echo "==> Step 4/6: refreshing tmpfiles.d + sudoers"
sudo cp "$SRC_DIR/_recovery/wildwatch-tmpfiles.conf" /etc/tmpfiles.d/wildwatch.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/wildwatch.conf
sudo install -m 0440 -o root -g root \
    "$SRC_DIR/_recovery/wildwatch-sudoers" /etc/sudoers.d/wildwatch
sudo visudo -c -q

echo "==> Step 5/6: refreshing systemd units"
sudo cp "$SRC_DIR/capture/systemd/wildwatch-capture.service" /etc/systemd/system/
sudo cp "$SRC_DIR/agent/systemd/wildwatch-agent.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo "==> Step 6/6: restart capture, enable + start agent"
sudo systemctl restart wildwatch-capture
sudo systemctl enable --now wildwatch-agent

echo
echo "==> Update done. Status:"
sudo systemctl is-active wildwatch-capture wildwatch-agent || true
echo "    The next heartbeat (within ~30s) should reach the server with the new agent version."
