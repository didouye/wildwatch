"""Filesystem storage helpers (PHOTOS_DIR resolution + path layout)."""

from __future__ import annotations

import os
from pathlib import Path


def photos_dir() -> Path:
    """Return the directory where photos are stored, creating it if needed."""
    override = os.environ.get("WILDWATCH_PHOTOS_DIR")
    if override:
        path = Path(override).resolve()
    else:
        path = Path(__file__).resolve().parents[3] / "data" / "photos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def previews_dir() -> Path:
    base = photos_dir().parent / "previews"
    base.mkdir(parents=True, exist_ok=True)
    return base
