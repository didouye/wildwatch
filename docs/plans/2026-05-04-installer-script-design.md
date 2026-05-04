# Installer Wildwatch -- Design

Date: 2026-05-04

## Context

Today, deploying onto a fresh RPi takes seven to eight manual steps (rsync,
SSH, running scripts, generating an API key, editing config.toml, restarting
the service). It is documented in `docs/SETUP-RPI.md` but tedious to
reproduce and easy to get wrong. We want a single orchestrator that drives
everything from the operator's PC.

## Goal

A single file `_recovery/install_wildwatch.py`, run from the repo root on
macOS or Linux, that discovers the target, adapts to either a fresh or an
already-configured RPi, manages the API key, deploys the code, configures
the systemd service, and confirms everything is running.

## Stack

| Tool          | Role                                                               |
|---------------|--------------------------------------------------------------------|
| Python 3.11+  | Language. PEP 723 inline metadata -> no separate pyproject needed. |
| `uv run`      | Spins up an ephemeral venv with the deps declared at the top.      |
| `rich`        | Panels, status spinners, timestamped log, command syntax highlight.|
| `questionary` | Interactive prompts: list selection, confirm, text input.          |
| `subprocess`  | SSH and rsync -- already configured tools we reuse.                |

PEP 723 inline metadata makes `uv run install_wildwatch.py` self-bootstrapping.
No dedicated venv, no pyproject to maintain.

## Operator requirements

- `uv` installed locally.
- `rsync` installed locally (already required by the existing deployments).
- An SSH key already authorized for `dietpi@<host>` (the script checks with
  `BatchMode=yes` and prints a clear `ssh-copy-id` hint on failure).
- DietPi already flashed and connected to WiFi on the RPi.
- The script must be run from the repo root (it fails fast otherwise).

## Flow

1. **Target discovery**
   - Ping `dietpi.local`. If it answers, ask the operator to confirm.
   - Otherwise, ARP scan filtered by Raspberry Pi OUIs (`b8:27:eb`,
     `dc:a6:32`, `e4:5f:01`, `2c:cf:67`). A broadcast ping primes the ARP
     table beforehand.
   - Otherwise, prompt the operator for an IP or hostname.

2. **SSH check**
   - `ssh -o BatchMode=yes -o ConnectTimeout=5 dietpi@<target> true`.
   - On failure, print an error with the `ssh-copy-id dietpi@<target>`
     command to run.

3. **RPi inspection**
   - A single grouped SSH call collects: presence of
     `~/wildwatch/config.toml`, current service state, values of
     `server_url` and `api_key` if they exist.
   - Decides between two scenarios: "reinstall" (config exists) vs "fresh".

4. **Server decision**
   - Reinstall: keep the existing URL and API key.
   - Fresh: prompt "Is the server already deployed somewhere?"
     - Yes: prompt for the URL and API key.
     - No: generate a new key via `secrets.token_urlsafe(32)`, save it in
       `_recovery/api_key.secret` (chmod 600, gitignored via `*.secret`),
       and offer to configure the server URL with the local PC IP + port
       8000. At the end the script prints the exact
       `WILDWATCH_API_KEY=... uv run uvicorn ...` command to start the
       server in another terminal.

5. **System setup**
   - Run `_recovery/setup_rpi.sh` over SSH (apt + groups + uv + avahi +
     blacklists + gpu_mem). The existing script is idempotent.
   - If `gpu_mem_1024` was changed (detected via grep before/after), reboot
     and wait for the host to come back.

6. **Code deployment**
   - `rsync` the repo to `~/wildwatch-src/` with the usual excludes
     (`.venv`, `__pycache__`, `data/`, `_recovery/sd_backup/`, `.git/`).

7. **Venv and dependencies**
   - `uv venv --system-site-packages --python /usr/bin/python3`, then
     `uv sync --no-dev --active` on the RPi.

8. **Runtime configuration**
   - Generate `~/wildwatch/config.toml` on the RPi from a heredoc (template
     embedded in the Python source). The preserved values (URL, API key)
     are substituted in.

9. **systemd service**
   - Run `_recovery/install_systemd.sh` over SSH. Idempotent. The service is
     restarted so the new config takes effect.

10. **Final checks**
    - `systemctl is-active wildwatch-capture` must return `active`.
    - Print the last five journal lines to confirm the monitoring loop
      started.
    - Print a final summary: command to follow the logs, command to start
      the server if the local setup branch was taken.

## UX

- `rich.console.Console` for rendering, with a timestamp on every step via
  `console.log()`.
- `Status` (spinner) during long commands (apt install, reboot wait).
- `Panel` for the script title at the top and the final summary at the bottom.
- Minimal palette: green for success, red for errors, blue for in-flight
  steps, yellow for warnings.
- `questionary.select` for list choices (arrow keys),
  `questionary.confirm` for Y/N prompts, `questionary.text` for free-form
  input with validation.
- On SIGINT: clear "Interrupted by user" message and clean exit.

## Idempotency and errors

- A failing step aborts the script with a non-zero exit code and a message
  pointing to the command to re-run manually.
- Operations are designed to be replayable without breaking state: rsync
  with `--delete` (but never inside `~/wildwatch/` runtime),
  `uv sync` reuses the existing venv, the config heredoc overwrites the
  file but uses the values preserved during the initial inspection.
- The script never creates files outside of the repo and `~/wildwatch*` on
  the RPi, so the operator can wipe everything manually if anything goes
  wrong.
