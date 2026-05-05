"""Read-only access to wildwatch-capture state files.

`wildwatch-capture` (Task 8) writes runtime state to `/run/wildwatch/`:
  - `status.json`: latest snapshot of service config + counters
  - `preview.jpg`: latest captured frame

This module provides a small abstraction over those files. It does NOT
create the state directory -- that is provisioned by the tmpfiles.d entry
installed by setup.sh. All methods degrade gracefully when files are
missing or unreadable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_STATE_DIR = Path("/run/wildwatch")


class StateReader:
    """Reads wildwatch-capture state files in a tolerant, read-only manner."""

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR) -> None:
        self.state_dir = Path(state_dir)

    def _age_s(self, p: Path) -> float | None:
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return None
        # Clamp to 0 to defend against clock skew between mtime and time.time().
        return max(0.0, time.time() - mtime)

    def read_status(self) -> tuple[dict | None, float | None]:
        """Return `(parsed_status, age_s)` or `(None, None)` if unavailable."""
        path = self.state_dir / "status.json"
        age = self._age_s(path)
        if age is None:
            return (None, None)
        try:
            parsed = json.loads(path.read_text())
        except (OSError, ValueError):
            return (None, None)
        if not isinstance(parsed, dict):
            return (None, None)
        return (parsed, age)

    def read_preview(self, max_age_s: float) -> tuple[bytes | None, float | None]:
        """Return `(jpeg_bytes, age_s)`.

        If the file is missing -> `(None, None)`.
        If the file exists but is older than `max_age_s` -> `(None, age)` so
        the caller can distinguish "no preview" from "stale preview".
        """
        path = self.state_dir / "preview.jpg"
        age = self._age_s(path)
        if age is None:
            return (None, None)
        if age > max_age_s:
            return (None, age)
        try:
            blob = path.read_bytes()
        except OSError:
            return (None, age)
        return (blob, age)
