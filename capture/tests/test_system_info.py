"""Tests pour les sondes système (lecture résiliente sur OS sans /sys/proc Linux)."""

from __future__ import annotations

import json

from wildwatch_capture import system_info


def test_snapshot_contains_hostname() -> None:
    snap = system_info.snapshot()
    assert "hostname" in snap
    assert isinstance(snap["hostname"], str)


def test_snapshot_is_json_serializable() -> None:
    """Le snapshot doit pouvoir être sérialisé tel quel pour le JSON de métadonnées."""
    snap = system_info.snapshot()
    serialized = json.dumps(snap)
    assert isinstance(serialized, str)


def test_cpu_temperature_returns_none_or_float() -> None:
    """Sur Mac /sys/class/thermal n'existe pas → None. Sur RPi → float positif."""
    temp = system_info.cpu_temperature_celsius()
    assert temp is None or (isinstance(temp, float) and 0 < temp < 200)


def test_memory_free_returns_none_or_positive() -> None:
    """Sur Mac /proc/meminfo n'existe pas → None. Sur RPi → float positif."""
    mem = system_info.memory_free_mb()
    assert mem is None or (isinstance(mem, float) and mem > 0)
