"""GET /thumb/{size}/{id}: serve thumbnails with a lazy fallback.

The route is unauthenticated (the UI uses it directly from the browser).
If the cached thumbnail is missing, it is generated synchronously before
serving. Source-missing or unknown id both return 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from wildwatch_server.db import get_session
from wildwatch_server.models import Photo
from wildwatch_server.thumbnails import THUMBNAIL_SIZES, ensure_thumbnail

router = APIRouter(prefix="/thumb")


@router.get("/{size}/{photo_id}")
def get_thumbnail(
    size: int, photo_id: int, session: Session = Depends(get_session)
) -> FileResponse:
    if size not in THUMBNAIL_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"size must be one of {list(THUMBNAIL_SIZES)}",
        )

    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    try:
        path = ensure_thumbnail(photo.file_path, size)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source image missing") from exc

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
