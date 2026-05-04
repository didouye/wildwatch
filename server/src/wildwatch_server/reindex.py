"""Filesystem scanner that ingests existing photos and JSON sidecars into SQLite.

Used by `POST /api/admin/reindex` to migrate from the V0.3 layout (sidecars
written to disk) to the V0.4 catalog. The operation is idempotent: photos
already present in DB (matched on `file_path`) are skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, select

from wildwatch_server.models import Photo
from wildwatch_server.routes.photos import _photo_from_metadata
from wildwatch_server.storage import photos_dir


def reindex(session: Session) -> dict[str, int]:
    base = photos_dir()
    scanned = 0
    inserted = 0
    skipped = 0

    for jpg in sorted(base.rglob("*.jpg")):
        scanned += 1
        relative = str(jpg.relative_to(base))

        existing = session.exec(
            select(Photo).where(Photo.file_path == relative)
        ).first()
        if existing is not None:
            skipped += 1
            continue

        meta_path = jpg.with_suffix(jpg.suffix + ".meta.json")
        metadata: dict = {}
        captured_at: datetime
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                metadata = {}

        captured_raw = metadata.get("captured_at")
        if captured_raw:
            try:
                captured_at = datetime.fromisoformat(
                    str(captured_raw).replace("Z", "+00:00")
                )
            except ValueError:
                captured_at = datetime.fromtimestamp(jpg.stat().st_mtime, tz=timezone.utc)
        else:
            captured_at = datetime.fromtimestamp(jpg.stat().st_mtime, tz=timezone.utc)

        photo = _photo_from_metadata(
            file_path=relative,
            file_size=jpg.stat().st_size,
            captured_at=captured_at,
            metadata=metadata,
        )
        session.add(photo)
        inserted += 1

    session.commit()
    return {"scanned": scanned, "inserted": inserted, "skipped": skipped}
