#!/usr/bin/env bash
# WildWatch -- one-line RPi installer.
#
# Usage (run on the RPi as the dietpi user, or any sudoer):
#   curl -fsSL https://raw.githubusercontent.com/didouye/wildwatch/main/_recovery/setup.sh \
#       | bash -s -- --server https://wildwatch.example.com
#
# This script:
#   1. Runs the system setup (apt packages, uv, groups, blacklists, gpu_mem).
#   2. Reboots if gpu_mem had to be changed (and waits for a sudoer to log
#      back in to finish the install -- a one-shot systemd unit handles that
#      automatically).
#   3. Clones the repo into ~/wildwatch-src/ and creates the uv venv.
#   4. Calls the server's POST /api/cameras/enroll to obtain a camera token.
#   5. Writes ~/wildwatch/config.toml with that token + the server URL.
#   6. Installs the tmpfiles.d entry (creates /run/wildwatch) and the
#      sudoers entry for the agent (PR2 will use it to restart capture).
#   7. Installs and starts the wildwatch-capture systemd service.
#   8. Builds the agent venv and starts the wildwatch-agent service.
#
# After the script finishes, the RPi shows up as "Pending approval" on the
# server's /cameras page. Approve it and uploads start within seconds.

set -euo pipefail

REPO_URL="${WILDWATCH_REPO:-https://github.com/didouye/wildwatch.git}"
RAW_BASE="${WILDWATCH_RAW:-https://raw.githubusercontent.com/didouye/wildwatch/main}"

usage() {
    cat <<EOF
Usage: $0 --server <URL>

  --server URL    Public URL of the WildWatch server (required), e.g.
                  https://wildwatch.example.com

Environment overrides:
  WILDWATCH_REPO  git URL of the repo (default: $REPO_URL)
  WILDWATCH_RAW   base URL for raw files (default: $RAW_BASE)
EOF
}

SERVER_URL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) SERVER_URL="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$SERVER_URL" ]]; then
    echo "error: --server URL is required" >&2
    usage
    exit 1
fi

echo "==> WildWatch RPi setup"
echo "    Server: $SERVER_URL"
echo "    Repo  : $REPO_URL"
echo

# ---------- Step 1: system setup ----------
echo "==> Step 1/8: system packages + uv + groups"
curl -fsSL "$RAW_BASE/_recovery/setup_rpi.sh" | bash

# ---------- Step 2: reboot if gpu_mem was bumped ----------
if grep -q "^gpu_mem_1024=96" /boot/firmware/config.txt 2>/dev/null \
    && ! vcgencmd version 2>/dev/null | grep -q "(start)"; then
    echo "==> gpu_mem changed; the RPi must reboot before the camera works."
    echo "    Schedule a one-shot resume after reboot..."
    cat >/tmp/wildwatch-resume.sh <<RESUME
#!/usr/bin/env bash
set -e
curl -fsSL "$RAW_BASE/_recovery/setup.sh" | bash -s -- --server "$SERVER_URL"
RESUME
    chmod +x /tmp/wildwatch-resume.sh
    sudo cp /tmp/wildwatch-resume.sh /etc/wildwatch-resume.sh
    sudo tee /etc/systemd/system/wildwatch-resume.service >/dev/null <<UNIT
[Unit]
Description=Resume WildWatch setup after gpu_mem reboot
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/etc/wildwatch-resume.sh
ExecStartPost=/bin/systemctl disable wildwatch-resume.service
[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl enable wildwatch-resume.service
    echo "==> Rebooting; setup will resume automatically. SSH back in 1 minute."
    sleep 3
    sudo /sbin/reboot
    exit 0
fi

# ---------- Step 3: clone repo + venv ----------
echo "==> Step 3/8: clone wildwatch repo + uv venv"
SRC_DIR="$HOME/wildwatch-src"
if [[ -d "$SRC_DIR/.git" ]]; then
    git -C "$SRC_DIR" pull --ff-only
else
    # Dir may exist without a .git/ (created by setup_rpi.sh's mkdir -p, or
    # left over from a prior failed install). git clone refuses a non-empty
    # destination, so wipe it -- wildwatch-src holds code + venv only, no
    # user data (which lives in ~/wildwatch/).
    rm -rf "$SRC_DIR"
    git clone --depth 1 "$REPO_URL" "$SRC_DIR"
fi

cd "$SRC_DIR/capture"
if [[ ! -d .venv ]]; then
    "$HOME/.local/bin/uv" venv --system-site-packages --python /usr/bin/python3 >/dev/null
fi
"$HOME/.local/bin/uv" sync --no-dev --active

# ---------- Step 4: enroll with the server ----------
echo "==> Step 4/8: enrolling this camera with the server"
HOSTNAME_TAG="$(hostname)"

# Resolve redirects (e.g. Caddy http -> https) so we end up storing the
# canonical URL in config.toml. This avoids a per-upload redirect roundtrip.
RESOLVED_URL="$(curl -fsSLo /dev/null -w '%{url_effective}' "$SERVER_URL/health" || true)"
if [[ -n "$RESOLVED_URL" && "$RESOLVED_URL" == *"/health" ]]; then
    SERVER_URL="${RESOLVED_URL%/health}"
    SERVER_URL="${SERVER_URL%/}"
    echo "    resolved server URL: $SERVER_URL"
fi

ENROLL_RESPONSE="$(curl -fsSL -X POST "$SERVER_URL/api/cameras/enroll" \
    -H "Content-Type: application/json" \
    -d "{\"hostname\": \"$HOSTNAME_TAG\"}")" || {
    echo "error: enrollment request failed (curl exit $?)" >&2
    echo "       check that $SERVER_URL is reachable and serving WildWatch" >&2
    exit 1
}

if ! echo "$ENROLL_RESPONSE" | python3 -c 'import json, sys; json.load(sys.stdin)' 2>/dev/null; then
    echo "error: server response was not valid JSON. Got:" >&2
    echo "----- response start -----" >&2
    echo "$ENROLL_RESPONSE" >&2
    echo "----- response end -----" >&2
    exit 1
fi

CAMERA_TOKEN="$(echo "$ENROLL_RESPONSE" | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')"
CAMERA_ID="$(echo "$ENROLL_RESPONSE" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
echo "    enrolled as camera #$CAMERA_ID (status=pending)"

# ---------- Step 5: runtime config ----------
echo "==> Step 5/8: writing ~/wildwatch/config.toml"
mkdir -p "$HOME/wildwatch"
cat >"$HOME/wildwatch/config.toml" <<TOML
[camera]
capture_width = 2304
capture_height = 1296
detection_width = 640
detection_height = 480
rotation = 0

[motion]
pixel_threshold = 25
area_threshold = 0.02
background_alpha = 0.05
warmup_frames = 30
cooldown_seconds = 5.0

[capture]
burst_count = 3
burst_interval_seconds = 0.5

[upload]
server_url = "$SERVER_URL"
api_key = "$CAMERA_TOKEN"
queue_dir = "~/wildwatch/queue"
sent_dir = "~/wildwatch/sent"
retry_interval_seconds = 30.0
sent_retention_days = 7
request_timeout_seconds = 60.0
TOML
chmod 600 "$HOME/wildwatch/config.toml"

# ---------- Step 6: tmpfiles.d + sudoers ----------
# Install these BEFORE the capture service starts so /run/wildwatch exists
# the first time wildwatch-capture comes up (it writes status.json there).
echo "==> Step 6/8: installing /etc/tmpfiles.d/wildwatch.conf + /etc/sudoers.d/wildwatch"
sudo cp "$SRC_DIR/_recovery/wildwatch-tmpfiles.conf" /etc/tmpfiles.d/wildwatch.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/wildwatch.conf
sudo install -m 0440 -o root -g root \
    "$SRC_DIR/_recovery/wildwatch-sudoers" /etc/sudoers.d/wildwatch
sudo visudo -c -q  # validate

# ---------- Step 7: capture systemd service ----------
echo "==> Step 7/8: installing the wildwatch-capture systemd service"
bash "$SRC_DIR/_recovery/install_systemd.sh"

# ---------- Step 8: agent venv + systemd service ----------
echo "==> Step 8/8: building agent venv + installing wildwatch-agent.service"
cd "$SRC_DIR/agent"
if [[ ! -d .venv ]]; then
    "$HOME/.local/bin/uv" venv --python /usr/bin/python3 >/dev/null
fi
"$HOME/.local/bin/uv" sync --no-dev --active

sudo cp "$SRC_DIR/agent/systemd/wildwatch-agent.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wildwatch-agent.service
sudo systemctl restart wildwatch-agent.service

cat <<DONE

==> WildWatch capture + agent are running.

    The camera is enrolled but PENDING. Approve it at:
      $SERVER_URL/cameras

    Once approved, photos start flowing automatically.

    Useful commands:
      sudo journalctl -u wildwatch-capture -f
      sudo journalctl -u wildwatch-agent -f
      sudo systemctl restart wildwatch-capture
      sudo systemctl restart wildwatch-agent
DONE
