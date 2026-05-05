"""Server-rendered UI routes (no auth)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server.db import get_session
from wildwatch_server.models import Photo
from wildwatch_server.storage import photos_dir

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PAGE_SIZE = 24


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/gallery", status_code=302)


def _parse_date(value: str | None) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc


@router.get("/gallery", response_class=HTMLResponse)
def gallery(
    request: Request,
    page: int = Query(default=1, ge=1),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    hostname: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    from_date = _parse_date(from_)
    to_date = _parse_date(to)

    query = select(Photo)
    count_query = select(func.count()).select_from(Photo)

    if from_date is not None:
        bound = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
        query = query.where(Photo.captured_at >= bound)
        count_query = count_query.where(Photo.captured_at >= bound)
    if to_date is not None:
        bound = datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc)
        query = query.where(Photo.captured_at <= bound)
        count_query = count_query.where(Photo.captured_at <= bound)
    if hostname:
        query = query.where(Photo.hostname == hostname)
        count_query = count_query.where(Photo.hostname == hostname)

    total = int(session.exec(count_query).one())
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    photos = session.exec(
        query.order_by(Photo.captured_at.desc()).limit(PAGE_SIZE).offset(offset)
    ).all()

    hostnames = sorted(
        {row[0] for row in session.exec(select(Photo.hostname).distinct()).all() if row[0]}
    )

    base_qs = {}
    if from_:
        base_qs["from"] = from_
    if to:
        base_qs["to"] = to
    if hostname:
        base_qs["hostname"] = hostname
    prev_qs = urlencode({**base_qs, "page": page - 1}) if page > 1 else ""
    next_qs = urlencode({**base_qs, "page": page + 1}) if page < total_pages else ""

    context = {
        "photos": photos,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_qs": prev_qs,
        "next_qs": next_qs,
        "hostnames": hostnames,
        "filters": {"from_": from_, "to": to, "hostname": hostname},
    }

    template = (
        "_gallery_grid.html" if request.headers.get("HX-Request") else "gallery.html"
    )
    return templates.TemplateResponse(request=request, name=template, context=context)


@router.get("/photos/{photo_id}", response_class=HTMLResponse)
def photo_detail(
    photo_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    # Find prev/next by captured_at order (desc → "previous" is the more recent one).
    prev_row = session.exec(
        select(Photo.id)
        .where(Photo.captured_at > photo.captured_at)
        .order_by(Photo.captured_at.asc())
        .limit(1)
    ).first()
    next_row = session.exec(
        select(Photo.id)
        .where(Photo.captured_at < photo.captured_at)
        .order_by(Photo.captured_at.desc())
        .limit(1)
    ).first()

    return templates.TemplateResponse(
        request=request,
        name="photo_detail.html",
        context={
            "photo": photo,
            "prev_id": prev_row,
            "next_id": next_row,
        },
    )


@router.get("/photos/{photo_id}/download")
def photo_download(
    photo_id: int, session: Session = Depends(get_session)
) -> FileResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    full_path = photos_dir() / photo.file_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(
        full_path, media_type="image/jpeg", filename=full_path.name
    )
