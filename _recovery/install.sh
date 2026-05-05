#!/usr/bin/env bash
# WildWatch -- one-line operator-side bootstrap.
#
# Run this on YOUR Mac (or any *nix dev box) to enroll a Raspberry Pi.
# The script clones the repo into /tmp, then invokes the Python orchestrator
# which SSHes into the Pi to install and start the capture service.
#
# Usage:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/didouye/wildwatch/main/_recovery/install.sh)" -- --server http://wildwatch.example.com
#
# Any arguments after `--` are forwarded to install_wildwatch.py.
#
# Requirements on this machine: bash, git, uv, rsync, ssh.
# The Pi is reached via mDNS (dietpi.local) or ARP scan; you can also enter
# its IP/hostname manually when prompted.

set -euo pipefail

REPO_URL="${WILDWATCH_REPO:-https://github.com/didouye/wildwatch.git}"
DEST="${WILDWATCH_INSTALLER_DIR:-/tmp/wildwatch-installer}"

require() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "x $1 not found in PATH. Install it first." >&2
        exit 1
    fi
}

require git
require uv

if [[ -e "$DEST" ]]; then
    echo "==> Removing leftover $DEST"
    rm -rf "$DEST"
fi

echo "==> Cloning $REPO_URL into $DEST"
git clone --depth 1 "$REPO_URL" "$DEST"

cd "$DEST"
exec uv run _recovery/install_wildwatch.py "$@"
