#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rich>=13.7",
#     "questionary>=2.0",
# ]
# ///
"""Installateur orchestrateur WildWatch.

Lancé depuis la racine du repo sur le PC de l'opérateur :
    uv run _recovery/install_wildwatch.py

Découvre une cible RPi sur le réseau, déploie le code, configure la clé API,
installe le service systemd, et confirme que tout tourne.

Pré-requis :
- uv installé localement (sinon le shebang ne marche pas)
- rsync installé localement
- Clé SSH configurée pour dietpi@<host> (BatchMode)
- DietPi flashé sur le RPi avec WiFi configuré
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

# OUIs Raspberry Pi Foundation (préfixes MAC).
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
    """Retourne la liste (ip, mac) des entrées ARP qui matchent les OUIs RPi."""
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
    """Trouve un hôte RPi et retourne son adresse SSH (hostname ou IP)."""
    console.log("[bold]→[/bold] Découverte des Raspberry Pi sur le réseau…")

    # 1) Essai mDNS dietpi.local (le défaut DietPi).
    if _try_ping("dietpi.local"):
        console.log("[green]✓[/green] [bold]dietpi.local[/bold] répond")
        if questionary.confirm(
            "Cibler dietpi.local ?", default=True, auto_enter=False
        ).ask():
            return "dietpi.local"

    # 2) Scan ARP filtré OUI Raspberry Pi.
    rpis = _arp_scan_rpis()
    if rpis:
        console.log(f"[green]✓[/green] {len(rpis)} RPi détecté(s) dans la table ARP")
        choices = [f"{ip}  ({mac})" for ip, mac in rpis] + ["[ Saisir une autre adresse ]"]
        choice = questionary.select("Cible ?", choices=choices).ask()
        if choice and not choice.startswith("["):
            return choice.split()[0]

    # 3) Saisie manuelle.
    return questionary.text(
        "IP ou hostname du RPi :", validate=lambda v: bool(v.strip()) or "Adresse vide"
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
                    f"[red]✗ SSH non-interactif refusé pour {SSH_USER}@{host}[/red]\n\n"
                    "Configure d'abord ta clé SSH :\n"
                    f"  [bold]ssh-copy-id {SSH_USER}@{host}[/bold]\n\n"
                    "Puis relance ce script."
                ),
                title="SSH",
                border_style="red",
            )
        )
        sys.exit(1)
    console.log(f"[green]✓[/green] SSH OK ({SSH_USER}@{host})")


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
    console.log(f"[bold]→[/bold] Inspection de {host}…")
    res = ssh_run(host, INSPECT_SCRIPT, capture=True)
    if res.returncode != 0:
        console.print(f"[red]✗ Inspection échouée :[/red]\n{res.stderr}")
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
        masked = (state.api_key[:6] + "…") if state.api_key else "(absente)"
        console.log(
            f"[yellow]↻[/yellow] Installation existante détectée — server_url="
            f"{state.server_url}, api_key={masked}, service="
            f"{'actif' if state.service_active else 'inactif'}"
        )
    else:
        console.log("[blue]✦[/blue] Pas d'installation existante — setup neuf")
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
    """Retourne (server_url, api_key, server_local).

    Si state.config_exists, on réutilise. Sinon on demande.
    """
    if state.config_exists and state.server_url and state.api_key:
        console.log("[green]✓[/green] Réutilisation de la config existante")
        return state.server_url, state.api_key, False

    if questionary.confirm(
        "Le serveur WildWatch est-il déjà déployé ailleurs ?", default=False
    ).ask():
        url = questionary.text(
            "URL du serveur (ex: http://192.168.1.10:8000) :",
            validate=lambda v: v.startswith("http") or "URL invalide",
        ).ask()
        key = questionary.text("Clé API existante :", validate=lambda v: bool(v) or "Vide").ask()
        return url, key, False

    # Setup local
    api_key = secrets.token_urlsafe(32)
    API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(api_key + "\n")
    API_KEY_FILE.chmod(0o600)
    default_url = f"http://{local_ip_guess()}:8000"
    url = questionary.text("URL du serveur local :", default=default_url).ask()
    console.log(f"[green]✓[/green] Nouvelle clé API générée → {API_KEY_FILE.relative_to(REPO_ROOT)}")
    return url, api_key, True


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def run_setup_rpi(host: str, gpu_mem_low_before: bool) -> bool:
    """Lance setup_rpi.sh sur le RPi. Retourne True si reboot nécessaire."""
    console.log("[bold]→[/bold] setup_rpi.sh sur le RPi…")
    with SETUP_RPI_SCRIPT.open() as fp:
        res = subprocess.run(
            [*_ssh_args(host), "bash -s"], stdin=fp, check=False
        )
    if res.returncode != 0:
        console.print("[red]✗ setup_rpi.sh a échoué[/red]")
        sys.exit(1)

    # Vérifie si gpu_mem a été modifié (= était low avant et est OK maintenant).
    if gpu_mem_low_before:
        check = ssh_run(
            host,
            "grep -q '^gpu_mem_1024=96' /boot/firmware/config.txt && echo OK",
            capture=True,
        )
        if "OK" in check.stdout:
            console.log("[yellow]↻[/yellow] gpu_mem_1024 modifié, reboot requis")
            return True
    console.log("[green]✓[/green] setup système OK")
    return False


def reboot_and_wait(host: str) -> None:
    console.log("[bold]→[/bold] Reboot du RPi…")
    ssh_run(host, "sudo /sbin/reboot", capture=True)
    with console.status("Attente du retour…", spinner="dots"):
        import time

        time.sleep(10)
        for _ in range(60):
            if _try_ping(host):
                # Donne quelques secondes de plus pour SSH
                time.sleep(3)
                if ssh_run(host, "true", capture=True).returncode == 0:
                    console.log(f"[green]✓[/green] {host} est de retour")
                    return
            time.sleep(5)
    console.print(f"[red]✗ {host} n'est pas revenu après reboot[/red]")
    sys.exit(1)


def rsync_code(host: str) -> None:
    console.log("[bold]→[/bold] rsync du code vers ~/wildwatch-src/…")
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
        console.print("[red]✗ rsync a échoué[/red]")
        sys.exit(1)
    console.log("[green]✓[/green] code synchronisé")


def setup_venv(host: str) -> None:
    console.log("[bold]→[/bold] Création/mise à jour du venv (uv sync)…")
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
        console.print(f"[red]✗ uv sync a échoué :[/red]\n{res.stdout}\n{res.stderr}")
        sys.exit(1)
    console.log("[green]✓[/green] venv prêt")


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
    console.log("[bold]→[/bold] Écriture de ~/wildwatch/config.toml…")
    body = CONFIG_TEMPLATE.format(server_url=server_url, api_key=api_key)
    cmd = "mkdir -p ~/wildwatch && cat > ~/wildwatch/config.toml"
    res = subprocess.run(
        [*_ssh_args(host), cmd], input=body, text=True, check=False, capture_output=True
    )
    if res.returncode != 0:
        console.print(f"[red]✗ Écriture config.toml échouée :[/red]\n{res.stderr}")
        sys.exit(1)
    console.log("[green]✓[/green] config.toml écrit")


def install_systemd(host: str) -> None:
    console.log("[bold]→[/bold] install_systemd.sh sur le RPi…")
    with INSTALL_SYSTEMD_SCRIPT.open() as fp:
        res = subprocess.run([*_ssh_args(host), "bash -s"], stdin=fp, check=False)
    if res.returncode != 0:
        console.print("[red]✗ install_systemd.sh a échoué[/red]")
        sys.exit(1)
    console.log("[green]✓[/green] service systemd installé")


def restart_and_verify(host: str) -> None:
    console.log("[bold]→[/bold] Restart du service et vérification…")
    ssh_run(host, "sudo systemctl restart wildwatch-capture", capture=True)
    import time

    time.sleep(3)
    res = ssh_run(host, "systemctl is-active wildwatch-capture", capture=True)
    if "active" not in res.stdout:
        console.print(f"[red]✗ service inactif après restart : {res.stdout.strip()}[/red]")
        logs = ssh_run(
            host, "sudo journalctl -u wildwatch-capture --no-pager -n 20", capture=True
        )
        console.print(logs.stdout)
        sys.exit(1)
    console.log("[green]✓[/green] service actif")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not SETUP_RPI_SCRIPT.exists() or not INSTALL_SYSTEMD_SCRIPT.exists():
        console.print(
            "[red]✗ Lance ce script depuis la racine du repo "
            "(sous-scripts manquants dans _recovery/).[/red]"
        )
        sys.exit(1)
    if shutil.which("rsync") is None:
        console.print("[red]✗ rsync introuvable. Installe rsync localement.[/red]")
        sys.exit(1)

    console.print(
        Panel(
            Text("WildWatch — Installateur RPi", style="bold cyan", justify="center"),
            border_style="cyan",
        )
    )

    host = discover_target()
    if not host:
        console.print("[yellow]Annulé[/yellow]")
        sys.exit(0)
    assert_ssh_ok(host)

    state = inspect_target(host)
    server_url, api_key, server_local = decide_server(state)

    # Vérifie gpu_mem_1024 avant et après pour décider du reboot.
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

    # Récap final
    summary = Text()
    summary.append("✓ wildwatch-capture est actif sur ", style="bold green")
    summary.append(host, style="bold")
    summary.append("\n\n")
    summary.append("Logs en direct :\n", style="bold")
    summary.append(f"  ssh {SSH_USER}@{host} 'sudo journalctl -u wildwatch-capture -f'\n")
    summary.append("\nRedémarrer le service :\n", style="bold")
    summary.append(f"  ssh {SSH_USER}@{host} 'sudo systemctl restart wildwatch-capture'\n")
    if server_local:
        summary.append("\nLance le serveur sur cette machine :\n", style="bold yellow")
        summary.append(f"  WILDWATCH_API_KEY={api_key} \\\n")
        summary.append("  uv run --directory server uvicorn wildwatch_server.main:app \\\n")
        summary.append("    --host 0.0.0.0 --port 8000\n")
        summary.append(
            f"\nLa clé est aussi dans {API_KEY_FILE.relative_to(REPO_ROOT)} (gitignored).\n",
            style="dim",
        )

    console.print(Panel(summary, title="✓ Déploiement terminé", border_style="green"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompu par l'utilisateur[/yellow]")
        sys.exit(130)
