#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "rich>=13.7", "questionary>=2.0"]
# ///
"""Tests unitaires des fonctions pures de l'installateur (sans TTY ni SSH)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Charge install_wildwatch.py comme module sans exécuter main().
SPEC = importlib.util.spec_from_file_location(
    "install_wildwatch", Path(__file__).parent / "install_wildwatch.py"
)
assert SPEC and SPEC.loader
INSTALL = importlib.util.module_from_spec(SPEC)
sys.modules["install_wildwatch"] = INSTALL  # requis pour que @dataclass fonctionne
SPEC.loader.exec_module(INSTALL)


def test_arp_scan_filters_rpi_ouis() -> None:
    """Le parser ARP ne doit garder que les MAC qui matchent les OUIs RPi."""
    fake_arp_output = (
        "? (192.168.0.1) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]\n"
        "? (192.168.0.23) at b8:27:eb:e1:c1:5e on en0 ifscope [ethernet]\n"
        "? (192.168.0.42) at dc:a6:32:11:22:33 on en0 ifscope [ethernet]\n"
        "? (192.168.0.99) at 96:0d:a6:34:71:89 on en0 ifscope permanent\n"
    )
    fake_result = type("R", (), {"returncode": 0, "stdout": fake_arp_output})()
    with patch.object(INSTALL.subprocess, "run", return_value=fake_result):
        rpis = INSTALL._arp_scan_rpis()
    ips = [ip for ip, _ in rpis]
    assert "192.168.0.23" in ips
    assert "192.168.0.42" in ips
    assert "192.168.0.1" not in ips  # OUI non-RPi
    assert "192.168.0.99" not in ips  # 96:0d… ne matche pas


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
    """Le template formaté doit produire un TOML parseable."""
    import tomllib

    body = INSTALL.CONFIG_TEMPLATE.format(server_url="http://x:8000", api_key="k")
    parsed = tomllib.loads(body)
    assert parsed["upload"]["server_url"] == "http://x:8000"
    assert parsed["upload"]["api_key"] == "k"
    assert parsed["camera"]["capture_width"] == 2304


def test_local_ip_guess_returns_string() -> None:
    """Smoke test : doit toujours retourner une IPv4 valide (au pire 127.0.0.1)."""
    ip = INSTALL.local_ip_guess()
    assert isinstance(ip, str)
    parts = ip.split(".")
    assert len(parts) == 4
    assert all(0 <= int(p) <= 255 for p in parts)


def test_repo_root_layout() -> None:
    """Le script doit pouvoir trouver les sous-scripts qu'il appelle."""
    assert INSTALL.SETUP_RPI_SCRIPT.name == "setup_rpi.sh"
    assert INSTALL.INSTALL_SYSTEMD_SCRIPT.name == "install_systemd.sh"
    # Les fichiers existent vraiment (test d'intégrité).
    assert INSTALL.SETUP_RPI_SCRIPT.exists()
    assert INSTALL.INSTALL_SYSTEMD_SCRIPT.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
