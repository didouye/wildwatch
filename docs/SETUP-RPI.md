# RPi setup -- Full guide and known gotchas

This document covers the full installation of wildwatch-capture on a Raspberry
Pi 2 v1.1 with DietPi and the Camera Module 3 NoIR. It walks through the
chronological steps and lists the gotchas encountered during V0.1/V0.2 along
with their workarounds, so the install can be reproduced quickly.

## Hardware

| Component | Tested model         | Notes                                      |
|-----------|----------------------|--------------------------------------------|
| SBC       | Raspberry Pi 2 v1.1  | BCM2836, ARMv7, 1 GB RAM                   |
| Camera    | Camera Module 3 NoIR | IMX708 sensor, CSI-2                       |
| Storage   | microSD 32 GB        | FAT32 + ext4 partitions                    |
| Network   | USB WiFi dongle      | The RPi 2 has no built-in WiFi             |
| Power     | 5 V / 2 A micro-USB  |                                            |

## All-in-one installer (recommended)

To automate every step (system setup, code deployment, runtime config, systemd
service, API key generation), use the Python orchestrator:

```bash
uv run _recovery/install_wildwatch.py
```

It discovers the RPi (mDNS -> ARP -> manual entry), handles both fresh and
existing installs, generates or reuses the API key, and drives everything via
SSH and rsync. It is idempotent: re-run it whenever you want.

Requirements: `uv` and `rsync` installed locally, plus an SSH key already set
up for `dietpi@<host>` (the script tells you the `ssh-copy-id` command to run
otherwise).

The sections below describe the equivalent manual steps -- useful for
debugging or when you want to control each step yourself.

## 1. Flash DietPi

1. Install Raspberry Pi Imager (`brew install --cask raspberry-pi-imager`).
2. Pick "Raspberry Pi 2".
3. OS: "Other specific-purpose OS" -> "DietPi" -> the build for RPi 1/2/3/4
   (ARMv7).
4. Storage: the microSD.
5. Do not apply OS customization in Imager -- we configure things via the
   DietPi files themselves.
6. Write and wait (5-10 min).

After flashing, the boot partition (FAT32) mounts on macOS as
`/Volumes/NO NAME/` (or `bootfs` depending on the version).

## 2. Pre-boot configuration on the SD card

Before the first boot, edit these files on the FAT32 partition:

### `dietpi-wifi.txt`

Uncomment and fill in:

```
aWIFI_SSID[0]='your_ssid'
aWIFI_KEY[0]='your_password'
```

### `dietpi.txt`

```ini
AUTO_SETUP_NET_WIFI_ENABLED=1
AUTO_SETUP_NET_WIFI_COUNTRY_CODE=FR     # or your country code
AUTO_SETUP_LOCALE=en_US.UTF-8
AUTO_SETUP_KEYBOARD_LAYOUT=us
AUTO_SETUP_TIMEZONE=Europe/Paris
AUTO_SETUP_NET_HOSTNAME=DietPi          # default, fine to keep
AUTO_SETUP_HEADLESS=1                   # important: no display attached
AUTO_SETUP_AUTOMATED=1                  # non-interactive install
SURVEY_OPTED_IN=0
```

### `config.txt`

Camera section:

```
#-------RPi camera module-------
#start_x=1
camera_auto_detect=1
dtoverlay=imx708
#disable_camera_led=1
```

GPU memory (critical, see gotcha #4 below):

```
gpu_mem_1024=96
```

Do NOT touch `cmdline.txt` (see gotcha #1).

Eject the SD cleanly (Cmd-E on macOS), insert it in the RPi, plug in power.

## 3. First boot

DietPi first boot: 5 to 10 minutes (initial config, package install, SSH host
keys, automatic reboot).

To follow availability from your dev box:

```bash
until ping -c 1 -W 1000 dietpi.local >/dev/null 2>&1; do sleep 5; done
echo "RPi up"
```

If `dietpi.local` does not resolve after a few minutes, see gotcha #6 (avahi
not installed by default) -- find the IP through your router or via an ARP
scan:

```bash
arp -a | grep "b8:27:eb"   # Raspberry Pi Foundation OUI
```

## 4. System setup

Once SSH works, run the script from your dev box:

```bash
ssh dietpi@dietpi.local 'bash -s' < _recovery/setup_rpi.sh
```

`_recovery/setup_rpi.sh` does, in order:

1. `apt update` + install (`rpicam-apps`, `python3-picamera2`,
   `avahi-daemon`, `libnss-mdns`, `rsync`, `curl`, `ca-certificates`).
2. Enable `avahi-daemon` (mDNS).
3. Remove the DietPi blacklists `dietpi-disable_rpi_camera.conf` and
   `dietpi-disable_rpi_codec.conf` (gotcha #2).
4. Bump `gpu_mem_1024` from `16` to `96` if needed (gotcha #4).
5. Add the `dietpi` user to the `video` and `render` groups.
6. Install `uv` via the official Astral script.
7. Create `~/wildwatch-src/`, `~/wildwatch/queue/`, `~/wildwatch/sent/`.

If the script changed `gpu_mem_1024`, reboot manually afterwards:

```bash
ssh dietpi@dietpi.local 'sudo /sbin/reboot'   # see gotcha #5 (systemctl reboot)
```

## 5. Code deployment

From the repo root:

```bash
rsync -av --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.uv-cache' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='data/' --exclude='_recovery/' --exclude='.git/' \
  ./ dietpi@dietpi.local:~/wildwatch-src/
```

Create the venv on the RPi (`--system-site-packages` is mandatory, see
gotcha #3):

```bash
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  ~/.local/bin/uv venv --system-site-packages --python /usr/bin/python3
  ~/.local/bin/uv sync --no-dev --active
'
```

Smoke test the imports:

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

## 6. Runtime configuration

Create `~/wildwatch/config.toml` on the RPi (see `capture/config.toml.example`
in the repo). At minimum, update `[upload].server_url` with your server IP.

```toml
[upload]
server_url = "http://192.168.1.10:8000"  # dev box / server IP
```

## 7. Run it

### Manual run (test/dev)

```bash
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  .venv/bin/wildwatch-capture --log-level INFO
'
```

### Run as a systemd service (production)

Once the chain works manually, install wildwatch-capture as a service that
starts at boot and restarts on failure:

```bash
ssh dietpi@dietpi.local 'bash -s' < _recovery/install_systemd.sh
```

Follow the logs:

```bash
ssh dietpi@dietpi.local 'sudo journalctl -u wildwatch-capture -f'
```

Stop / restart:

```bash
ssh dietpi@dietpi.local 'sudo systemctl restart wildwatch-capture'
```

Expected logs:

```
Config loaded from /home/dietpi/wildwatch/config.toml
Target server: http://192.168.1.10:8000
Starting camera
Monitoring loop started
```

When motion is detected:

```
Motion detected (score=0.140), capturing burst
Queued /home/dietpi/wildwatch/queue/...
Burst done: 3 photo(s) enqueued
Uploaded ... -> data/photos/...
3 photo(s) uploaded to the server
```

---

## Gotchas and workarounds

### Gotcha #1 -- `cma=256M` in `cmdline.txt` bricks the boot

**Symptom:** after adding `cma=256M` to `/boot/firmware/cmdline.txt` and
rebooting, the RPi 2 v1.1 stops booting. Solid red LED, green LED off ->
kernel panic before any userspace runs. No way to recover via SSH.

**Cause:** `cma=256M` is too large for the BCM2836 memory layout on a RPi 2
v1.1 and triggers a kernel panic when the kernel tries to reserve the CMA
pool.

**Recovery:** physically pull the SD, mount it on macOS, edit `cmdline.txt`
to remove the parameter, re-insert. Because the RPi was hot-unplugged in the
middle of a panic, the ext4 rootfs got corrupted as well -- we ended up
reflashing DietPi from scratch.

**Lesson:** NEVER touch `cmdline.txt` on the RPi 2 v1.1 without an easy
recovery plan (and even then, it is risky).

**CMA workaround:** do not bump CMA. Adjust the resolution to fit the budget
instead (see gotcha #7).

### Gotcha #2 -- DietPi blacklists the camera modules by default

**Symptom:** `rpicam-hello --list-cameras` returns
`ERROR: rpicam-apps currently only supports the Raspberry Pi platforms.`
even on a real RPi. `picamera2` raises `IndexError: list index out of range`
because libcamera registers no camera at all.

**Cause:** DietPi creates two files in `/etc/modprobe.d/` that blacklist
`bcm2835_isp` and `bcm2835_codec`:
- `/etc/modprobe.d/dietpi-disable_rpi_camera.conf`
- `/etc/modprobe.d/dietpi-disable_rpi_codec.conf`

Without `bcm2835_isp`, the libcamera `rpi/vc4` pipeline handler cannot pair
the IMX708 sensor with an ISP device and silently gives up.

**Workaround:** delete both files, reboot.

```bash
sudo rm /etc/modprobe.d/dietpi-disable_rpi_camera.conf \
        /etc/modprobe.d/dietpi-disable_rpi_codec.conf
sudo /sbin/reboot
```

This is wired into `_recovery/setup_rpi.sh`.

### Gotcha #3 -- `picamera2` is not pip-installable

**Symptom:** `uv sync` tries to compile `numpy` 2.4.4 from source for armv7l
(no wheel available) and fails.

**Cause:** `picamera2` ships only via apt on Debian (`python3-picamera2`).
It depends on `numpy` (system) and on C++ bindings to `libcamera` that do
not install via pip.

**Workaround:**
- Install `python3-picamera2` via apt -- it pulls in system `numpy` (2.2.4).
- Create the uv venv with `--system-site-packages` so the venv inherits
  `numpy` and `picamera2` from the system.
- Do NOT list `numpy` in `dependencies` of `capture/pyproject.toml`
  (otherwise uv tries to install a newer one in the venv, which either
  shadows the system version or fails to compile). Keep it only in
  `dependency-groups.dev` for tests on the dev box.

### Gotcha #4 -- `gpu_mem_1024=16` disables the camera firmware

**Symptom:** kernel modules load, IMX708 shows up in dmesg, but libcamera
sees no camera. `vcgencmd version` shows `(start_cd)`.

**Cause:** DietPi defaults to `gpu_mem_1024=16` to save RAM. With 16 MB the
bootloader loads `start_cd.elf` (cut-down), which has no camera/ISP support.

**Workaround:** set `gpu_mem_1024=96` in `/boot/firmware/config.txt`, reboot.
The firmware switches to `start.elf` (full, camera works). `vcgencmd version`
flips from `(start_cd)` to `(start)`.

### Gotcha #5 -- `systemctl reboot` fails with "dbus-org.freedesktop.login1.service"

**Symptom:**
```
Failed to set wall message, ignoring: Unit dbus-org.freedesktop.login1.service failed to load properly...
Call to Reboot failed: Unit dbus-org.freedesktop.login1.service failed to load properly...
```

**Cause:** broken/incomplete systemd service unit on minimal DietPi.

**Workaround:** call `/sbin/reboot` directly instead of `systemctl reboot`.
The reboot still happens despite the dbus error.

### Gotcha #6 -- `dietpi.local` does not resolve

**Symptom:** right after reflash, `ssh dietpi@dietpi.local` fails with
"cannot resolve hostname".

**Cause:** minimal DietPi does not install `avahi-daemon` or `libnss-mdns`,
so there is no mDNS responder.

**Workaround:** find the RPi IP via the router or with an ARP scan
(`arp -a | grep "b8:27:eb"`), then:

```bash
ssh dietpi@<ip> 'sudo apt-get install -y avahi-daemon libnss-mdns && sudo systemctl enable --now avahi-daemon'
```

This is wired into `_recovery/setup_rpi.sh`.

### Gotcha #7 -- V4L2 forces 4 buffers minimum

**Symptom:** high-resolution capture fails with:
```
ERROR V4L2 v4l2_videodevice.cpp:1323 Unable to request 4 buffers: Cannot allocate memory
```

Even with `picamera2.create_still_configuration(buffer_count=1)`.

**Cause:** the RPi V4L2 driver (`bcm2835_unicam_legacy`) always allocates a
minimum of 4 buffers regardless of what picamera2 asks. With 4608x2592
BGR888 = 36 MB per buffer, we end up at 144 MB > 64 MB CMA available.

**Workaround:** capture at 2304x1296 (3 MP). 4 x 9 MB = 36 MB, well within
the 64 MB CMA budget. That resolution is plenty for animal identification
and is compatible with SpeciesNet.

### Gotcha #8 -- `Path.replace()` fails between `/tmp` and `/home`

**Symptom:** after capture, `OSError: [Errno 18] Invalid cross-device link`.

**Cause:** DietPi mounts `/tmp` on tmpfs (RAM), so `/tmp` and
`/home/dietpi` live on different devices. `os.rename(2)` (used by
`Path.replace()`) cannot rename across devices.

**Workaround:** use `shutil.move()`, which falls back to copy + delete in
that case. See `capture/src/wildwatch_capture/uploader.py`.

### Gotcha #9 -- `rpicam-apps` v1.11.1 bug on RPi 2 v1.1

**Symptom:** `rpicam-still` exits 0 without producing a file.
`rpicam-still --version` segfaults at exit after printing the version.

**Cause:** specific bug in `rpicam-apps` v1.11.1 on this combination of
RPi 2 v1.1 + Camera Module 3 + kernel 6.12 + Debian Trixie.

**Workaround:** do not use the `rpicam-*` CLI tools. Use `picamera2`
(Python) directly, which works perfectly on the same hardware. Our code
already uses picamera2 exclusively.

### Gotcha #10 -- `scp` does not work on DietPi

**Symptom:** `scp file dietpi@dietpi.local:` fails with
`bash: line 1: /usr/lib/sftp-server: No such file or directory`.

**Cause:** minimal DietPi does not install `openssh-sftp-server`.

**Workaround:** use `rsync` (installed by the setup script). `rsync` does
not need sftp and goes through plain SSH.

### Gotcha #11 -- SSH host key changes after a reflash

**Symptom:** `Host key verification failed` on SSH after a reflash.

**Cause:** the new install generated new SSH host keys, but your dev box
still has the old key in `~/.ssh/known_hosts`.

**Workaround:**

```bash
ssh-keygen -R dietpi.local
ssh-keyscan -H dietpi.local >> ~/.ssh/known_hosts
```

---

## Critical files cheat sheet

| Path                                          | Purpose                                  |
|-----------------------------------------------|------------------------------------------|
| `/boot/firmware/config.txt`                   | Camera config + `gpu_mem_1024`           |
| `/boot/firmware/cmdline.txt`                  | DO NOT TOUCH                             |
| `/etc/modprobe.d/dietpi-disable_rpi_*.conf`   | Delete                                   |
| `~/wildwatch/config.toml`                     | wildwatch-capture runtime config         |
| `~/wildwatch-src/capture/.venv/`              | uv venv with system-site-packages        |

## Quick reproduction after a reflash

Once a fresh DietPi is flashed with WiFi configured:

```bash
# 1. Wait for the RPi to come up
until ping -c 1 -W 1000 dietpi.local >/dev/null 2>&1; do sleep 5; done

# 2. Auto setup (apt + groups + uv + avahi + blacklists + gpu_mem)
ssh dietpi@dietpi.local 'bash -s' < _recovery/setup_rpi.sh

# 3. Reboot if gpu_mem was changed
ssh dietpi@dietpi.local 'sudo /sbin/reboot'
until ping -c 1 -W 1000 dietpi.local >/dev/null 2>&1; do sleep 5; done

# 4. Sync the code
rsync -av --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.uv-cache' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='data/' --exclude='_recovery/' --exclude='.git/' \
  ./ dietpi@dietpi.local:~/wildwatch-src/

# 5. Set up the venv
ssh dietpi@dietpi.local '
  cd ~/wildwatch-src/capture
  ~/.local/bin/uv venv --system-site-packages --python /usr/bin/python3
  ~/.local/bin/uv sync --no-dev --active
'

# 6. Create ~/wildwatch/config.toml (manual, or copy from capture/config.toml.example)

# 7. Run
ssh dietpi@dietpi.local 'cd ~/wildwatch-src/capture && .venv/bin/wildwatch-capture'
```
