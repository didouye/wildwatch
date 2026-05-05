"""Thumbnail generation and caching.

Three sizes are produced (150, 400, 800 px max edge) and cached on disk
under a `thumbnails/` directory parallel to the photos tree. Generation is
idempotent and EXIF-aware. The capture pipeline schedules generation in
FastAPI BackgroundTasks at upload time; the GET /thumb endpoint generates
synchronously when the cached file is missing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps

from wildwatch_server.storage import photos_dir

log = logging.getLogger(__name__)

THUMBNAIL_SIZES: tuple[int, ...] = (150, 400, 800)


def thumbnail_dir() -> Path:
    """Directory holding the cached thumbnails (sibling of photos_dir)."""
    base = photos_dir().parent / "thumbnails"
    base.mkdir(parents=True, exist_ok=True)
    return base


def thumbnail_path(file_path: str, size: int) -> Path:
    """Absolute path of the thumbnail, may not exist yet.

    `file_path` is the relative path stored in DB (e.g. "2026/05/04/x.jpg").
    """
    return thumbnail_dir() / str(size) / file_path


def generate_one(file_path: str, size: int) -> Path:
    """Generate a single thumbnail and return its path.

    Idempotent: returns immediately if the target already exists. Errors
    raise; callers decide whether to log or propagate.
    """
    if size not in THUMBNAIL_SIZES:
        raise ValueError(f"Unsupported thumbnail size: {size}")

    source = photos_dir() / file_path
    target = thumbnail_path(file_path, size)
    if target.exists():
        return target
    if not source.exists():
        raise FileNotFoundError(f"Source missing: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as img:
        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((size, size), Image.LANCZOS)
        # Convert RGBA / palette images to RGB so JPEG saves cleanly.
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(target, "JPEG", quality=80, optimize=True)
    return target


def generate_all(file_path: str) -> list[Path]:
    """Generate all configured sizes. Used as a BackgroundTask.

    Failures are logged but do not propagate, so the upload response stays
    successful even if a thumbnail cannot be produced for some reason.
    Missing thumbs will be regenerated lazily by `ensure_thumbnail` later.
    """
    paths: list[Path] = []
    for size in THUMBNAIL_SIZES:
        try:
            paths.append(generate_one(file_path, size))
        except Exception:
            log.exception("Failed to generate thumbnail size=%d for %s", size, file_path)
    return paths


def ensure_thumbnail(file_path: str, size: int) -> Path:
    """Lazy fallback. If the cached thumbnail is missing, generate it now.

    Raises FileNotFoundError if the source image does not exist.
    """
    target = thumbnail_path(file_path, size)
    if target.exists():
        return target
    return generate_one(file_path, size)
