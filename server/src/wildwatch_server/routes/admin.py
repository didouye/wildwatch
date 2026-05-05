"""Admin endpoints (reindex, regen_thumbnails)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import Photo, ReindexResponse
from wildwatch_server.reindex import reindex
from wildwatch_server.thumbnails import THUMBNAIL_SIZES, thumbnail_path

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_api_key)])


@router.post("/reindex", response_model=ReindexResponse)
def admin_reindex(session: Session = Depends(get_session)) -> ReindexResponse:
    result = reindex(session)
    return ReindexResponse(**result)


@router.post("/regen_thumbnails")
def admin_regen_thumbnails(session: Session = Depends(get_session)) -> dict[str, int]:
    """Walk the photo catalog and generate any missing thumbnails."""
    from wildwatch_server.thumbnails import generate_one

    generated = 0
    skipped = 0
    photos = session.exec(select(Photo)).all()
    for photo in photos:
        for size in THUMBNAIL_SIZES:
            target = thumbnail_path(photo.file_path, size)
            if target.exists():
                skipped += 1
                continue
            try:
                generate_one(photo.file_path, size)
                generated += 1
            except FileNotFoundError:
                # Source image missing -- nothing we can do, skip silently.
                pass
    return {"generated": generated, "skipped": skipped}
