"""Rotate historical photos for a camera by a delta in {90, 180, 270}.

Walks every Photo row whose camera_id matches and whose captured_at is at
or before `ack_time`. For each, rotates the JPEG in place at quality 95,
deletes cached thumbnails (regenerated lazily next view), and (if delta is
a quarter-turn) swaps `camera_width`/`camera_height` in the DB row.

In-memory progress counter `reorient_progress[camera_id] = (done, total)`
is updated as the job runs. The counter disappears on server restart;
photos already rotated stay rotated, the rest don't.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

from PIL import Image
from sqlmodel import Session, select

from wildwatch_server.db import get_engine
from wildwatch_server.models import Camera, Photo
from wildwatch_server.storage import photos_dir
from wildwatch_server.thumbnails import THUMBNAIL_SIZES, thumbnail_path

log = logging.getLogger(__name__)

_lock = Lock()
reorient_progress: dict[int, tuple[int, int]] = {}


def _pil_rotation_for_delta(delta: int) -> int:
    """Convert a clockwise delta to PIL's counter-clockwise rotation argument.

    PIL's Image.rotate(angle) rotates counter-clockwise. Operator wants
    clockwise. So for delta=90 (CW), we pass -90 to PIL (= 270 CCW).
    """
    return (-delta) % 360


def _rotate_one_photo(photo: Photo, delta: int, base_dir: Path) -> bool:
    """Rotate one photo file in place + delete its thumbs. Returns True on success."""
    src = base_dir / photo.file_path
    if not src.exists():
        log.warning("Source missing, skipping reorient: %s", src)
        return False
    try:
        with Image.open(src) as img:
            rotated = img.rotate(_pil_rotation_for_delta(delta), expand=True)
            if rotated.mode != "RGB":
                rotated = rotated.convert("RGB")
            tmp = src.with_suffix(src.suffix + ".tmp")
            rotated.save(tmp, "JPEG", quality=95, optimize=True)
        os.replace(tmp, src)
    except Exception:
        log.exception("Failed to rotate %s", src)
        return False

    for size in THUMBNAIL_SIZES:
        thumbnail_path(photo.file_path, size).unlink(missing_ok=True)
    return True


def reorient_camera_photos(
    camera_id: int, delta: int, ack_time: datetime
) -> dict:
    """Synchronously rotate every photo for a camera, captured at or before ack_time.

    Designed to be invoked from a FastAPI BackgroundTask. Updates the
    in-memory progress counter as it goes. Clears `pending_reorient_delta`
    on the Camera row when finished.
    """
    if delta not in (90, 180, 270):
        raise ValueError(f"delta must be one of 90/180/270, got {delta}")

    engine = get_engine()
    base = photos_dir()
    rotated_count = 0
    skipped_count = 0

    with Session(engine) as session:
        photos = session.exec(
            select(Photo)
            .where(Photo.camera_id == camera_id)
            .where(Photo.captured_at <= ack_time)
        ).all()
        total = len(photos)
        with _lock:
            reorient_progress[camera_id] = (0, total)

        for i, photo in enumerate(photos, 1):
            if _rotate_one_photo(photo, delta, base):
                rotated_count += 1
                if delta in (90, 270):
                    photo.camera_width, photo.camera_height = (
                        photo.camera_height,
                        photo.camera_width,
                    )
                    session.add(photo)
            else:
                skipped_count += 1
            with _lock:
                reorient_progress[camera_id] = (i, total)

        # Commit dim-swap updates in one batch.
        session.commit()

        # Clear the pending delta so the UI knows the job finished.
        cam = session.get(Camera, camera_id)
        if cam is not None:
            cam.pending_reorient_delta = None
            session.add(cam)
            session.commit()

    with _lock:
        reorient_progress.pop(camera_id, None)

    return {"rotated": rotated_count, "skipped": skipped_count, "total": total}
