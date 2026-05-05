"""System probes for the agent heartbeat payload.

All probes are best-effort: they return ``None`` when the underlying source
is unavailable (e.g. ``/sys`` and ``/proc`` on macOS dev boxes).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def cpu_temp_c() -> float | None:
    """Read CPU temperature in Celsius from the thermal_zone0 sysfs entry."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return int(raw) / 1000.0
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        return None


def memory_mb() -> tuple[float | None, float | None]:
    """Return (available, total) memory in MB by parsing /proc/meminfo.

    Returns ``(None, None)`` when /proc/meminfo is unavailable.
    """
    try:
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split()
            if len(value) >= 1 and value[0].isdigit():
                info[key] = int(value[0])
        avail_kb = info.get("MemAvailable") or info.get("MemFree")
        total_kb = info.get("MemTotal")
        avail = avail_kb / 1024.0 if avail_kb is not None else None
        total = total_kb / 1024.0 if total_kb is not None else None
        return avail, total
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        return None, None


def disk_avail_mb(path: str = "/") -> float | None:
    """Return free disk space in MB for the filesystem containing ``path``."""
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def load_avg_1min() -> float | None:
    """Return the 1-minute load average, or ``None`` if unavailable."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def queue_size(queue_dir: Path | None) -> int:
    """Count ``.jpg`` files in ``queue_dir``. Returns 0 if missing or None."""
    if queue_dir is None:
        return 0
    try:
        return sum(1 for p in queue_dir.iterdir() if p.is_file() and p.suffix == ".jpg")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return 0


def snapshot(queue_dir: Path | None) -> dict[str, object]:
    """Aggregate all probes into a JSON-serializable dict."""
    avail, total = memory_mb()
    return {
        "cpu_temp_c": cpu_temp_c(),
        "memory_avail_mb": avail,
        "memory_total_mb": total,
        "load_avg_1min": load_avg_1min(),
        "disk_avail_mb": disk_avail_mb(),
        "queue_size": queue_size(queue_dir),
    }
