#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7",
#     "questionary>=2.0",
# ]
# ///
"""WildWatch deployment orchestrator.

Run from the repo root on your dev box:
    uv run _recovery/install_wildwatch.py

Discovers a RPi target on the network, deploys the code, configures the API
key, installs the systemd service, and confirms the service is running.

Requirements:
- uv installed locally (the shebang relies on it)
- rsync installed locally
- SSH key configured for dietpi@<host> (BatchMode)
- DietPi flashed on the RPi with WiFi configured
"""

from __future__ import annotations

import re
import secrets
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_RPI_SCRIPT = REPO_ROOT / "_recovery" / "setup_rpi.sh"
INSTALL_SYSTEMD_SCRIPT = REPO_ROOT / "_recovery" / "install_systemd.sh"
API_KEY_FILE = REPO_ROOT / "_recovery" / "api_key.secret"

# Raspberry Pi Foundation OUIs (MAC prefixes).
RPI_OUIS = ("b8:27:eb", "dc:a6:32", "e4:5f:01", "2c:cf:67")
SSH_USER = "dietpi"


@dataclass
class TargetState:
    host: str
    config_exists: bool
    server_url: str | None
    api_key: str | None
    service_active: bool


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _try_ping(host: str, timeout: int = 1) -> bool:
    res = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout * 1000), host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return res.returncode == 0


def _arp_scan_rpis() -> list[tuple[str, str]]:
    """Return the list of (ip, mac) ARP entries that match a RPi OUI."""
    res = subprocess.run(["arp", "-an"], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return []
    found: list[tuple[str, str]] = []
    pattern = re.compile(r"\(([\d.]+)\) at ([0-9a-f:]+)", re.IGNORECASE)
    for line in res.stdout.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2).lower()
        if any(mac.startswith(oui) for oui in RPI_OUIS) and (ip, mac) not in found:
            found.append((ip, mac))
    return found


def discover_target() -> str:
    """Find a RPi host and return its SSH address (hostname or IP)."""
    console.log("[bold]>[/bold] Discovering Raspberry Pi devices on the network...")

    # 1) Try mDNS dietpi.local (the DietPi default).
    if _try_ping("dietpi.local"):
        console.log("[green]ok[/green] [bold]dietpi.local[/bold] responds")
        if questionary.confirm(
            "Target dietpi.local?", default=True, auto_enter=False
        ).ask():
            return "dietpi.local"

    # 2) ARP scan filtered by Raspberry Pi OUIs.
    rpis = _arp_scan_rpis()
    if rpis:
        console.log(f"[green]ok[/green] {len(rpis)} RPi found in the ARP table")
        choices = [f"{ip}  ({mac})" for ip, mac in rpis] + ["[ Enter another address ]"]
        choice = questionary.select("Target?", choices=choices).ask()
        if choice and not choice.startswith("["):
            return choice.split()[0]

    # 3) Manual entry.
    return questionary.text(
        "RPi IP or hostname:", validate=lambda v: bool(v.strip()) or "Empty address"
    ).ask()


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def _ssh_args(host: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{SSH_USER}@{host}",
    ]


def ssh_run(host: str, command: str, *, capture: bool = False) -> subprocess.CompletedProcess:
    args = [*_ssh_args(host), command]
    if capture:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    return subprocess.run(args, check=False)


def assert_ssh_ok(host: str) -> None:
    res = ssh_run(host, "true", capture=True)
    if res.returncode != 0:
        console.print(
            Panel(
                Text.from_markup(
                    f"[red]x Non-interactive SSH refused for {SSH_USER}@{host}[/red]\n\n"
                    "Set up your SSH key first:\n"
                    f"  [bold]ssh-copy-id {SSH_USER}@{host}[/bold]\n\n"
                    "Then re-run this script."
                ),
                title="SSH",
                border_style="red",
            )
        )
        sys.exit(1)
    console.log(f"[green]ok[/green] SSH OK ({SSH_USER}@{host})")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


INSPECT_SCRIPT = r"""set -e
if [ -f ~/wildwatch/config.toml ]; then
  echo "CONFIG_EXISTS=1"
  awk -F'=' '/^server_url|^api_key/ {gsub(/^ *| *$/, "", $1); gsub(/^ *"|" *$/, "", $2); print toupper($1) "=" $2}' ~/wildwatch/config.toml
else
  echo "CONFIG_EXISTS=0"
fi
systemctl is-active wildwatch-capture 2>/dev/null || true
grep -q "^gpu_mem_1024=16" /boot/firmware/config.txt 2>/dev/null && echo "GPU_MEM_LOW=1" || echo "GPU_MEM_LOW=0"
"""


def inspect_target(host: str) -> TargetState:
    console.log(f"[bold]>[/bold] Inspecting {host}...")
    res = ssh_run(host, INSPECT_SCRIPT, capture=True)
    if res.returncode != 0:
        console.print(f"[red]x Inspection failed:[/red]\n{res.stderr}")
        sys.exit(1)

    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    fields = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    config_exists = fields.get("CONFIG_EXISTS") == "1"
    state = TargetState(
        host=host,
        config_exists=config_exists,
        server_url=fields.get("SERVER_URL"),
        api_key=fields.get("API_KEY"),
        service_active="active" in lines,
    )

    if config_exists:
        masked = (state.api_key[:6] + "...") if state.api_key else "(none)"
        console.log(
            f"[yellow]~[/yellow] Existing install detected -- server_url="
            f"{state.server_url}, api_key={masked}, service="
            f"{'active' if state.service_active else 'inactive'}"
        )
    else:
        console.log("[blue]+[/blue] No existing install -- fresh setup")
    return state


# ---------------------------------------------------------------------------
# Server config
# ---------------------------------------------------------------------------


def local_ip_guess() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def decide_server(state: TargetState) -> tuple[str, str, bool]:
    """Return (server_url, api_key, server_local).

    If state.config_exists, reuse the existing values. Otherwise, prompt.
    """
    if state.config_exists and state.server_url and state.api_key:
        console.log("[green]ok[/green] Reusing existing config")
        return state.server_url, state.api_key, False

    if questionary.confirm(
        "Is the WildWatch server already deployed somewhere?", default=False
    ).ask():
        url = questionary.text(
            "Server URL (e.g. http://192.168.1.10:8000):",
            validate=lambda v: v.startswith("http") or "Invalid URL",
        ).ask()
        key = questionary.text(
            "Existing API key:", validate=lambda v: bool(v) or "Empty"
        ).ask()
        return url, key, False

    # Local server setup
    api_key = secrets.token_urlsafe(32)
    API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(api_key + "\n")
    API_KEY_FILE.chmod(0o600)
    default_url = f"http://{local_ip_guess()}:8000"
    url = questionary.text("Local server URL:", default=default_url).ask()
    console.log(
        f"[green]ok[/green] New API key written to {API_KEY_FILE.relative_to(REPO_ROOT)}"
    )
    return url, api_key, True


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def run_setup_rpi(host: str, gpu_mem_low_before: bool) -> bool:
    """Run setup_rpi.sh on the RPi. Returns True if a reboot is required."""
    console.log("[bold]>[/bold] Running setup_rpi.sh on the RPi...")
    with SETUP_RPI_SCRIPT.open() as fp:
        res = subprocess.run(
            [*_ssh_args(host), "bash -s"], stdin=fp, check=False
        )
    if res.returncode != 0:
        console.print("[red]x setup_rpi.sh failed[/red]")
        sys.exit(1)

    # Detect whether gpu_mem was actually changed (was low, is now ok).
    if gpu_mem_low_before:
        check = ssh_run(
            host,
            "grep -q '^gpu_mem_1024=96' /boot/firmware/config.txt && echo OK",
            capture=True,
        )
        if "OK" in check.stdout:
            console.log("[yellow]~[/yellow] gpu_mem_1024 changed, reboot required")
            return True
    console.log("[green]ok[/green] system setup complete")
    return False


def reboot_and_wait(host: str) -> None:
    console.log("[bold]>[/bold] Rebooting the RPi...")
    ssh_run(host, "sudo /sbin/reboot", capture=True)
    with console.status("Waiting for the host to come back...", spinner="dots"):
        import time

        time.sleep(10)
        for _ in range(60):
            if _try_ping(host):
                # Give SSH a few extra seconds
                time.sleep(3)
                if ssh_run(host, "true", capture=True).returncode == 0:
                    console.log(f"[green]ok[/green] {host} is back")
                    return
            time.sleep(5)
    console.print(f"[red]x {host} did not come back after reboot[/red]")
    sys.exit(1)


def rsync_code(host: str) -> None:
    console.log("[bold]>[/bold] rsync code to ~/wildwatch-src/...")
    cmd = [
        "rsync",
        "-az",
        "--delete",
        "--exclude=.venv",
        "--exclude=__pycache__",
        "--exclude=*.pyc",
        "--exclude=.uv-cache",
        "--exclude=.pytest_cache",
        "--exclude=.ruff_cache",
        "--exclude=data/",
        "--exclude=_recovery/sd_backup/",
        "--exclude=_recovery/api_key.secret",
        "--exclude=.git/",
        f"{REPO_ROOT}/",
        f"{SSH_USER}@{host}:~/wildwatch-src/",
    ]
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        console.print("[red]x rsync failed[/red]")
        sys.exit(1)
    console.log("[green]ok[/green] code synced")


def setup_venv(host: str) -> None:
    console.log("[bold]>[/bold] Creating/updating the venv (uv sync)...")
    cmd = (
        "cd ~/wildwatch-src/capture && "
        "if [ ! -d .venv ]; then "
        "  ~/.local/bin/uv venv --system-site-packages --python /usr/bin/python3 "
        "    >/dev/null 2>&1; "
        "fi && "
        "~/.local/bin/uv sync --no-dev --active 2>&1 | tail -3"
    )
    res = ssh_run(host, cmd, capture=True)
    if res.returncode != 0:
        console.print(f"[red]x uv sync failed:[/red]\n{res.stdout}\n{res.stderr}")
        sys.exit(1)
    console.log("[green]ok[/green] venv ready")


CONFIG_TEMPLATE = """\
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
server_url = "{server_url}"
api_key = "{api_key}"
queue_dir = "~/wildwatch/queue"
sent_dir = "~/wildwatch/sent"
retry_interval_seconds = 30.0
sent_retention_days = 7
request_timeout_seconds = 60.0
"""


def write_config(host: str, server_url: str, api_key: str) -> None:
    console.log("[bold]>[/bold] Writing ~/wildwatch/config.toml...")
    body = CONFIG_TEMPLATE.format(server_url=server_url, api_key=api_key)
    cmd = "mkdir -p ~/wildwatch && cat > ~/wildwatch/config.toml"
    res = subprocess.run(
        [*_ssh_args(host), cmd], input=body, text=True, check=False, capture_output=True
    )
    if res.returncode != 0:
        console.print(f"[red]x writing config.toml failed:[/red]\n{res.stderr}")
        sys.exit(1)
    console.log("[green]ok[/green] config.toml written")


def install_systemd(host: str) -> None:
    console.log("[bold]>[/bold] Running install_systemd.sh on the RPi...")
    with INSTALL_SYSTEMD_SCRIPT.open() as fp:
        res = subprocess.run([*_ssh_args(host), "bash -s"], stdin=fp, check=False)
    if res.returncode != 0:
        console.print("[red]x install_systemd.sh failed[/red]")
        sys.exit(1)
    console.log("[green]ok[/green] systemd service installed")


def restart_and_verify(host: str) -> None:
    console.log("[bold]>[/bold] Restarting the service and checking status...")
    ssh_run(host, "sudo systemctl restart wildwatch-capture", capture=True)
    import time

    time.sleep(3)
    res = ssh_run(host, "systemctl is-active wildwatch-capture", capture=True)
    if "active" not in res.stdout:
        console.print(f"[red]x service inactive after restart: {res.stdout.strip()}[/red]")
        logs = ssh_run(
            host, "sudo journalctl -u wildwatch-capture --no-pager -n 20", capture=True
        )
        console.print(logs.stdout)
        sys.exit(1)
    console.log("[green]ok[/green] service active")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not SETUP_RPI_SCRIPT.exists() or not INSTALL_SYSTEMD_SCRIPT.exists():
        console.print(
            "[red]x Run this script from the repo root "
            "(sub-scripts missing in _recovery/).[/red]"
        )
        sys.exit(1)
    if shutil.which("rsync") is None:
        console.print("[red]x rsync not found. Install rsync locally first.[/red]")
        sys.exit(1)

    console.print(
        Panel(
            Text("WildWatch -- RPi installer", style="bold cyan", justify="center"),
            border_style="cyan",
        )
    )

    host = discover_target()
    if not host:
        console.print("[yellow]Cancelled[/yellow]")
        sys.exit(0)
    assert_ssh_ok(host)

    state = inspect_target(host)
    server_url, api_key, server_local = decide_server(state)

    # Probe gpu_mem_1024 before/after to know whether a reboot is needed.
    pre = ssh_run(
        host, "grep -q '^gpu_mem_1024=16' /boot/firmware/config.txt && echo LOW || echo OK",
        capture=True,
    )
    gpu_mem_low_before = "LOW" in pre.stdout
    needs_reboot = run_setup_rpi(host, gpu_mem_low_before)
    if needs_reboot:
        reboot_and_wait(host)

    rsync_code(host)
    setup_venv(host)
    write_config(host, server_url, api_key)
    install_systemd(host)
    restart_and_verify(host)

    # Final summary
    summary = Text()
    summary.append("ok wildwatch-capture is active on ", style="bold green")
    summary.append(host, style="bold")
    summary.append("\n\n")
    summary.append("Live logs:\n", style="bold")
    summary.append(f"  ssh {SSH_USER}@{host} 'sudo journalctl -u wildwatch-capture -f'\n")
    summary.append("\nRestart the service:\n", style="bold")
    summary.append(f"  ssh {SSH_USER}@{host} 'sudo systemctl restart wildwatch-capture'\n")
    if server_local:
        summary.append("\nStart the server on this machine:\n", style="bold yellow")
        summary.append(f"  WILDWATCH_API_KEY={api_key} \\\n")
        summary.append("  uv run --directory server uvicorn wildwatch_server.main:app \\\n")
        summary.append("    --host 0.0.0.0 --port 8000\n")
        summary.append(
            f"\nThe key is also stored at {API_KEY_FILE.relative_to(REPO_ROOT)} "
            "(gitignored).\n",
            style="dim",
        )

    console.print(Panel(summary, title="Deployment complete", border_style="green"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(130)
