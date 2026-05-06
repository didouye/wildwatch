"""Apply a `desired_config` to ~/wildwatch/config.toml and restart capture.

The agent receives a `desired_config` dict from the heartbeat response, calls
`apply_desired_config(...)`, and on success memorises the apply timestamp so
the next heartbeat carries `applied_at`.

Only the `[camera]`, `[motion]`, `[capture]` sections of the TOML are
writeable. The `[upload]` section is never touched (it carries the camera
token + server URL). Unknown keys in the desired dict are silently dropped.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("wildwatch_agent.apply")

# Map each pushable field to the TOML section it lives in.
FIELD_TO_SECTION: dict[str, str] = {
    "capture_width": "camera",
    "capture_height": "camera",
    "detection_width": "camera",
    "detection_height": "camera",
    "rotation": "camera",
    "pixel_threshold": "motion",
    "area_threshold": "motion",
    "background_alpha": "motion",
    "warmup_frames": "motion",
    "cooldown_seconds": "motion",
    "burst_count": "capture",
    "burst_interval_seconds": "capture",
}


def _format_value(v: object) -> str:
    """Render a Python value as TOML scalar.

    Handles bool, int, float, str. For nested types we'd need tomlkit; YAGNI.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # Escape backslashes and double-quotes -- minimal but enough for filesystem paths.
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML value type: {type(v).__name__}")


def _merge_into_config_toml(config_path: Path, changes: dict) -> None:
    """Edit the file in-place: replace `key = ...` lines under their owning section.

    The strategy is a regex-per-line edit instead of a full TOML round-trip
    because we want to preserve the operator's comments + ordering. We only
    touch lines whose key is in FIELD_TO_SECTION, and only inside that
    field's expected section.
    """
    text = config_path.read_text()

    # Group changes by section.
    by_section: dict[str, dict] = {}
    for k, v in changes.items():
        section = FIELD_TO_SECTION.get(k)
        if section is None:
            log.warning("Ignoring unknown desired field: %s", k)
            continue
        by_section.setdefault(section, {})[k] = v

    if not by_section:
        return  # nothing to do

    # Walk the file line-by-line, tracking the current section header.
    current_section: str | None = None
    new_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^\s*\[([A-Za-z_][A-Za-z_0-9]*)\]\s*$", line)
        if m:
            current_section = m.group(1)
            new_lines.append(line)
            continue

        if current_section in by_section:
            # See if this line sets one of our target keys.
            key_match = re.match(r"^(\s*)([A-Za-z_][A-Za-z_0-9]*)(\s*=\s*).+$", line)
            if key_match:
                indent, key, eq = key_match.group(1), key_match.group(2), key_match.group(3)
                if key in by_section[current_section]:
                    new_value = _format_value(by_section[current_section].pop(key))
                    suffix = "\n" if line.endswith("\n") else ""
                    new_lines.append(f"{indent}{key}{eq}{new_value}{suffix}")
                    continue
        new_lines.append(line)

    # Any leftover changes mean the section was missing the key entirely; append.
    for section, leftovers in by_section.items():
        if not leftovers:
            continue
        if not new_lines or not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"[{section}]\n")
        for key, value in leftovers.items():
            new_lines.append(f"{key} = {_format_value(value)}\n")

    # Atomic write.
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text("".join(new_lines))
    os.replace(tmp, config_path)


SYSTEMCTL_BIN = "/bin/systemctl"


def apply_desired_config(desired: dict, config_path: Path) -> None:
    """Write the change to config.toml then restart wildwatch-capture.

    Raises on failure (subprocess error, file write error). The caller
    catches and decides whether to retry or skip.
    """
    log.info("Applying desired_config: %s", desired)
    _merge_into_config_toml(config_path, desired)
    log.info("Wrote config.toml; restarting wildwatch-capture")
    subprocess.run(
        ["sudo", SYSTEMCTL_BIN, "restart", "wildwatch-capture"],
        check=True,
    )
