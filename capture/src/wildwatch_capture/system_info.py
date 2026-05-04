"""Small system probes used to enrich the photo metadata sidecar."""

from __future__ import annotations

import os
import socket
from pathlib import Path


def hostname() -> str:
    return socket.gethostname()


def cpu_temperature_celsius() -> float | None:
    """Read the CPU temperature from /sys/class/thermal/thermal_zone0/temp."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return int(raw) / 1000.0
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def memory_free_mb() -> float | None:
    """Estimate free memory by parsing /proc/meminfo."""
    try:
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split()
            if len(value) >= 1 and value[0].isdigit():
                info[key] = int(value[0])
        # MemAvailable is more accurate than MemFree (it includes reclaimable cache)
        kb = info.get("MemAvailable") or info.get("MemFree")
        return kb / 1024.0 if kb is not None else None
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def snapshot() -> dict[str, object]:
    """Return a JSON-serializable dict describing the current system state."""
    snap: dict[str, object] = {"hostname": hostname()}
    temp = cpu_temperature_celsius()
    if temp is not None:
        snap["cpu_temp_celsius"] = round(temp, 1)
    mem = memory_free_mb()
    if mem is not None:
        snap["memory_available_mb"] = round(mem, 1)
    load = loadavg()
    if load is not None:
        snap["load_avg_1min"] = round(load[0], 2)
    return snap
