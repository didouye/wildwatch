"""Public share endpoints (no auth, accessed via opaque share_token)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from wildwatch_server.db import get_session
from wildwatch_server.models import Photo
from wildwatch_server.storage import photos_dir
from wildwatch_server.thumbnails import THUMBNAIL_SIZES, ensure_thumbnail

router = APIRouter(prefix="/share")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _photo_for_token(token: str, session: Session) -> Photo:
    photo = session.exec(select(Photo).where(Photo.share_token == token)).first()
    if photo is None:
        raise HTTPException(status_code=404, detail="Share link unknown or revoked")
    return photo


@router.get("/{token}", response_class=HTMLResponse)
def share_view(
    token: str, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    photo = _photo_for_token(token, session)
    return templates.TemplateResponse(
        request=request,
        name="share_view.html",
        context={
            "token": token,
            "photo": photo,
            "tag_names": [t.name for t in photo.tags],
        },
    )


@router.get("/{token}/file")
def share_download(
    token: str, session: Session = Depends(get_session)
) -> FileResponse:
    photo = _photo_for_token(token, session)
    full_path = photos_dir() / photo.file_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(full_path, media_type="image/jpeg", filename=full_path.name)


@router.get("/{token}/thumb/{size}")
def share_thumb(
    token: str, size: int, session: Session = Depends(get_session)
) -> FileResponse:
    if size not in THUMBNAIL_SIZES:
        raise HTTPException(status_code=400, detail=f"size must be one of {list(THUMBNAIL_SIZES)}")
    photo = _photo_for_token(token, session)
    try:
        path = ensure_thumbnail(photo.file_path, size)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source image missing") from exc
    return FileResponse(path, media_type="image/jpeg")
