#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "rich>=13.7", "questionary>=2.0"]
# ///
"""Unit tests for the installer's pure functions (no TTY, no SSH)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Load install_wildwatch.py as a module without running main().
SPEC = importlib.util.spec_from_file_location(
    "install_wildwatch", Path(__file__).parent / "install_wildwatch.py"
)
assert SPEC and SPEC.loader
INSTALL = importlib.util.module_from_spec(SPEC)
sys.modules["install_wildwatch"] = INSTALL  # required so @dataclass resolves correctly
SPEC.loader.exec_module(INSTALL)


def test_arp_scan_filters_rpi_ouis() -> None:
    """The ARP parser only keeps MAC addresses matching a RPi OUI."""
    fake_arp_output = (
        "? (192.168.1.1) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]\n"
        "? (192.168.1.23) at b8:27:eb:aa:bb:cc on en0 ifscope [ethernet]\n"
        "? (192.168.1.42) at dc:a6:32:11:22:33 on en0 ifscope [ethernet]\n"
        "? (192.168.1.99) at 00:1a:2b:33:44:55 on en0 ifscope permanent\n"
    )
    fake_result = type("R", (), {"returncode": 0, "stdout": fake_arp_output})()
    with patch.object(INSTALL.subprocess, "run", return_value=fake_result):
        rpis = INSTALL._arp_scan_rpis()
    ips = [ip for ip, _ in rpis]
    assert "192.168.1.23" in ips
    assert "192.168.1.42" in ips
    assert "192.168.1.1" not in ips  # non-RPi OUI
    assert "192.168.1.99" not in ips  # OUI does not match an RPi prefix


def test_arp_scan_returns_empty_on_failure() -> None:
    fake_result = type("R", (), {"returncode": 1, "stdout": ""})()
    with patch.object(INSTALL.subprocess, "run", return_value=fake_result):
        assert INSTALL._arp_scan_rpis() == []


def test_config_template_substitutes_values() -> None:
    body = INSTALL.CONFIG_TEMPLATE.format(
        server_url="http://example.com:8000",
        api_key="abc-XYZ-123",
    )
    assert 'server_url = "http://example.com:8000"' in body
    assert 'api_key = "abc-XYZ-123"' in body


def test_config_template_is_valid_toml() -> None:
    """The rendered template must produce parseable TOML."""
    import tomllib

    body = INSTALL.CONFIG_TEMPLATE.format(server_url="http://x:8000", api_key="k")
    parsed = tomllib.loads(body)
    assert parsed["upload"]["server_url"] == "http://x:8000"
    assert parsed["upload"]["api_key"] == "k"
    assert parsed["camera"]["capture_width"] == 2304


def test_local_ip_guess_returns_string() -> None:
    """Smoke test: must always return a valid IPv4 (at worst 127.0.0.1)."""
    ip = INSTALL.local_ip_guess()
    assert isinstance(ip, str)
    parts = ip.split(".")
    assert len(parts) == 4
    assert all(0 <= int(p) <= 255 for p in parts)


def test_repo_root_layout() -> None:
    """The installer must be able to find the sub-scripts it calls."""
    assert INSTALL.SETUP_RPI_SCRIPT.name == "setup_rpi.sh"
    assert INSTALL.INSTALL_SYSTEMD_SCRIPT.name == "install_systemd.sh"
    # Sanity check: these files actually exist on disk.
    assert INSTALL.SETUP_RPI_SCRIPT.exists()
    assert INSTALL.INSTALL_SYSTEMD_SCRIPT.exists()


def test_agent_assets_exist() -> None:
    """V1.2 PR1: the installer also deploys the agent package and unit file."""
    # Constants point at the right files.
    assert INSTALL.AGENT_UNIT_FILE.name == "wildwatch-agent.service"
    assert INSTALL.TMPFILES_CONF.name == "wildwatch-tmpfiles.conf"
    assert INSTALL.SUDOERS_FILE.name == "wildwatch-sudoers"
    # And the files exist on disk.
    assert INSTALL.AGENT_UNIT_FILE.exists()
    assert INSTALL.TMPFILES_CONF.exists()
    assert INSTALL.SUDOERS_FILE.exists()


def test_agent_deploy_helpers_exist() -> None:
    """V1.2 PR1: the new agent-deploy phases must be exposed as functions."""
    # These functions are wired into main() to mirror the capture flow.
    assert callable(getattr(INSTALL, "setup_agent_venv", None))
    assert callable(getattr(INSTALL, "install_agent_systemd", None))
    assert callable(getattr(INSTALL, "install_tmpfiles_and_sudoers", None))
    assert callable(getattr(INSTALL, "verify_agent", None))


def test_agent_venv_command_uses_agent_dir() -> None:
    """The agent venv builder must cd into ~/wildwatch-src/agent (not capture)."""
    captured: dict[str, str] = {}

    def fake_ssh_run(_host: str, command: str, *, capture: bool = False):
        captured["command"] = command
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(INSTALL, "ssh_run", side_effect=fake_ssh_run):
        INSTALL.setup_agent_venv("dietpi.local")

    cmd = captured["command"]
    assert "~/wildwatch-src/agent" in cmd
    assert "uv sync" in cmd
    # The agent venv must NOT use --system-site-packages (no picamera2 dep).
    assert "--system-site-packages" not in cmd


def test_install_agent_systemd_uses_unit_file() -> None:
    """install_agent_systemd must copy the unit file, daemon-reload, enable + start."""
    captured: dict[str, str] = {}

    def fake_ssh_run(_host: str, command: str, *, capture: bool = False):
        captured["command"] = command
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(INSTALL, "ssh_run", side_effect=fake_ssh_run):
        INSTALL.install_agent_systemd("dietpi.local")

    cmd = captured["command"]
    assert "agent/systemd/wildwatch-agent.service" in cmd
    assert "/etc/systemd/system/wildwatch-agent.service" in cmd
    assert "systemctl daemon-reload" in cmd
    assert "systemctl enable wildwatch-agent.service" in cmd
    assert "systemctl restart wildwatch-agent.service" in cmd


def test_install_tmpfiles_and_sudoers_command() -> None:
    """tmpfiles + sudoers install must touch both /etc/tmpfiles.d and /etc/sudoers.d."""
    captured: dict[str, str] = {}

    def fake_ssh_run(_host: str, command: str, *, capture: bool = False):
        captured["command"] = command
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(INSTALL, "ssh_run", side_effect=fake_ssh_run):
        INSTALL.install_tmpfiles_and_sudoers("dietpi.local")

    cmd = captured["command"]
    assert "/etc/tmpfiles.d/wildwatch.conf" in cmd
    assert "/etc/sudoers.d/wildwatch" in cmd
    assert "systemd-tmpfiles --create" in cmd
    assert "visudo -c" in cmd


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
