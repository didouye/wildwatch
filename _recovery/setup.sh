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
#   6. Installs and starts the wildwatch-capture systemd service.
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
echo "==> Step 1/6: system packages + uv + groups"
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
echo "==> Step 3/6: clone wildwatch repo + uv venv"
SRC_DIR="$HOME/wildwatch-src"
if [[ ! -d "$SRC_DIR/.git" ]]; then
    git clone --depth 1 "$REPO_URL" "$SRC_DIR"
else
    git -C "$SRC_DIR" pull --ff-only
fi

cd "$SRC_DIR/capture"
if [[ ! -d .venv ]]; then
    "$HOME/.local/bin/uv" venv --system-site-packages --python /usr/bin/python3 >/dev/null
fi
"$HOME/.local/bin/uv" sync --no-dev --active

# ---------- Step 4: enroll with the server ----------
echo "==> Step 4/6: enrolling this camera with the server"
HOSTNAME_TAG="$(hostname)"
ENROLL_RESPONSE="$(curl -fsS -X POST "$SERVER_URL/api/cameras/enroll" \
    -H "Content-Type: application/json" \
    -d "{\"hostname\": \"$HOSTNAME_TAG\"}")"
CAMERA_TOKEN="$(echo "$ENROLL_RESPONSE" | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')"
CAMERA_ID="$(echo "$ENROLL_RESPONSE" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
echo "    enrolled as camera #$CAMERA_ID (status=pending)"

# ---------- Step 5: runtime config ----------
echo "==> Step 5/6: writing ~/wildwatch/config.toml"
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

# ---------- Step 6: systemd service ----------
echo "==> Step 6/6: installing the systemd service"
bash "$SRC_DIR/_recovery/install_systemd.sh"

cat <<DONE

==> WildWatch capture is running.

    The camera is enrolled but PENDING. Approve it at:
      $SERVER_URL/cameras

    Once approved, photos start flowing automatically.

    Useful commands:
      sudo journalctl -u wildwatch-capture -f
      sudo systemctl restart wildwatch-capture
DONE
