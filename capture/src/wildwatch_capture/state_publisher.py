"""Publish wildwatch-capture's current state to /run/wildwatch/.

The agent reads these files (status.json, preview.jpg) and forwards them
to the server. We never read the agent back -- this is one-way only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

DEFAULT_STATE_DIR = Path("/run/wildwatch")
PREVIEW_W, PREVIEW_H = 320, 240


class StatePublisher:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR) -> None:
        self._dir = state_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def write_status(self, snap: dict[str, Any]) -> None:
        """Atomic write of status.json (tmp + replace)."""
        target = self._dir / "status.json"
        tmp = self._dir / "status.json.tmp"
        tmp.write_text(json.dumps(snap, separators=(",", ":")))
        os.replace(tmp, target)

    def write_preview(self, frame: np.ndarray) -> None:
        """Encode a 2D grayscale frame as a 320x240 JPEG.

        `frame` is whatever `camera.read_detection_frame()` returns (Y plane,
        uint8). We resize with PIL and save as quality=70 -- this is a status
        preview, not the canonical capture.
        """
        target = self._dir / "preview.jpg"
        tmp = self._dir / "preview.jpg.tmp"
        img = Image.fromarray(frame).convert("L")
        img.thumbnail((PREVIEW_W, PREVIEW_H), Image.LANCZOS)
        img.save(tmp, "JPEG", quality=70, optimize=True)
        os.replace(tmp, target)
