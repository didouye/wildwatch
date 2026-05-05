"""/api/photos/* endpoints: upload, list, detail, download, delete, patch, bulk."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import os

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    Camera,
    Photo,
    PhotoListResponse,
    PhotoRead,
    PhotoTagLink,
    PhotoUpdate,
    Tag,
)
from wildwatch_server.rate_limit import UPLOAD_LIMIT, limiter
from wildwatch_server.storage import photos_dir
from wildwatch_server.thumbnails import THUMBNAIL_SIZES, generate_all, thumbnail_path

# We do NOT mount require_api_key on the whole router any more: POST /api/photos
# supports two auth modes (camera token or admin key), and we want each
# admin route to declare its dependency explicitly.
router = APIRouter(prefix="/api/photos")


def _photo_upload_auth(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Camera | None:
    """Authenticate POST /api/photos.

    Returns the matching Camera (if a camera token was used and it is
    approved) or None (if WILDWATCH_API_KEY was used). Raises 401 / 403
    in every other case.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()

    admin_key = os.environ.get("WILDWATCH_API_KEY") or None
    if admin_key and token == admin_key:
        return None

    camera = session.exec(select(Camera).where(Camera.token == token)).first()
    if camera is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if camera.status != "approved":
        raise HTTPException(
            status_code=403,
            detail=f"Camera is {camera.status}; waiting for admin approval",
        )
    return camera


def _photo_from_metadata(
    file_path: str,
    file_size: int,
    captured_at: datetime,
    metadata: dict,
) -> Photo:
    """Build a Photo row from the metadata JSON blob sent by the capture client."""
    camera = metadata.get("camera") or {}
    sensor = metadata.get("sensor") or {}
    system = metadata.get("system") or {}
    return Photo(
        captured_at=captured_at,
        file_path=file_path,
        file_size=file_size,
        hostname=system.get("hostname"),
        motion_score=metadata.get("motion_score"),
        frame_index=metadata.get("frame_index"),
        burst_size=metadata.get("burst_size"),
        camera_width=camera.get("width"),
        camera_height=camera.get("height"),
        sensor_model=sensor.get("model"),
        exposure_time_us=sensor.get("exposure_time_us"),
        analogue_gain=sensor.get("analogue_gain"),
        lux=sensor.get("lux"),
        cpu_temp=system.get("cpu_temp_celsius"),
        memory_avail_mb=system.get("memory_available_mb"),
        load_avg_1min=system.get("load_avg_1min"),
    )


def _photo_to_read(photo: Photo) -> PhotoRead:
    """Convert a Photo row (with tags relationship loaded) to its API representation."""
    data = photo.model_dump()
    data["tags"] = sorted(t.name for t in photo.tags)
    return PhotoRead.model_validate(data)


def upsert_tag(session: Session, name: str) -> Tag:
    """Find a tag by case-insensitive name, creating it if it does not exist."""
    name = name.strip()
    if not name:
        raise ValueError("Tag name cannot be empty")
    existing = session.exec(
        select(Tag).where(func.lower(Tag.name) == name.lower())
    ).first()
    if existing is not None:
        return existing
    tag = Tag(name=name)
    session.add(tag)
    session.flush()  # so tag.id is available immediately
    return tag


def set_photo_tags(session: Session, photo: Photo, names: list[str]) -> None:
    """Replace the tags attached to a photo with the given names."""
    deduped = []
    seen = set()
    for raw in names:
        cleaned = raw.strip()
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        deduped.append(cleaned)
    photo.tags = [upsert_tag(session, name) for name in deduped]


# ---------- Upload ----------


@router.post("")
@limiter.limit(UPLOAD_LIMIT)
async def upload_photo(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    captured_at: str | None = Form(default=None),
    metadata: str | None = Form(default=None),
    session: Session = Depends(get_session),
    camera: Camera | None = Depends(_photo_upload_auth),
) -> dict:
    if file.content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(
            status_code=415, detail=f"Unsupported media type: {file.content_type}"
        )

    now = datetime.now(timezone.utc)
    captured_at_dt: datetime
    if captured_at:
        try:
            captured_at_dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            captured_at_dt = now
    else:
        captured_at_dt = now

    base = photos_dir()
    target_dir = base / f"{now:%Y/%m/%d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "photo.jpg").suffix or ".jpg"
    target_path = target_dir / f"{now:%Y%m%dT%H%M%S%f}{suffix}"
    contents = await file.read()
    target_path.write_bytes(contents)

    relative = str(target_path.relative_to(base))
    parsed_meta: dict = {}
    if metadata:
        try:
            parsed_meta = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="metadata must be valid JSON"
            ) from None

    photo = _photo_from_metadata(
        file_path=relative,
        file_size=len(contents),
        captured_at=captured_at_dt,
        metadata=parsed_meta,
    )
    photo.received_at = now
    if camera is not None:
        photo.camera_id = camera.id
        # Override hostname from the camera record so the gallery filter and
        # the metadata panel always reflect the operator-managed name.
        photo.hostname = camera.hostname
        camera.last_seen_at = now
        session.add(camera)
    session.add(photo)
    session.commit()
    session.refresh(photo)

    background_tasks.add_task(generate_all, relative)

    return {
        "id": photo.id,
        "stored_path": f"data/photos/{relative}",
        "size_bytes": str(len(contents)),
        "received_at": now.isoformat(),
        "captured_at": captured_at_dt.isoformat(),
    }


# ---------- List + filters ----------


def _parse_date_param(value: str | None, name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{name} must be YYYY-MM-DD"
        ) from exc


@router.get("", response_model=PhotoListResponse, dependencies=[Depends(require_api_key)])
def list_photos(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    hostname: str | None = Query(default=None),
    favorite: bool | None = Query(default=None),
    tag: str | None = Query(default=None),
    camera_id: int | None = Query(default=None),
    order: Literal["captured_at_desc", "captured_at_asc"] = Query(
        default="captured_at_desc"
    ),
    session: Session = Depends(get_session),
) -> PhotoListResponse:
    from_date = _parse_date_param(from_, "from")
    to_date = _parse_date_param(to, "to")

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
    if camera_id is not None:
        query = query.where(Photo.camera_id == camera_id)
        count_query = count_query.where(Photo.camera_id == camera_id)
    if favorite:
        query = query.where(Photo.is_favorite.is_(True))
        count_query = count_query.where(Photo.is_favorite.is_(True))
    if tag:
        # Subquery: photo_ids that have a tag matching the name (case-insensitive)
        tag_q = (
            select(PhotoTagLink.photo_id)
            .join(Tag, Tag.id == PhotoTagLink.tag_id)
            .where(func.lower(Tag.name) == tag.lower())
        )
        query = query.where(Photo.id.in_(tag_q))
        count_query = count_query.where(Photo.id.in_(tag_q))

    if order == "captured_at_asc":
        query = query.order_by(Photo.captured_at.asc())
    else:
        query = query.order_by(Photo.captured_at.desc())

    query = query.limit(limit).offset(offset)

    items = session.exec(query).all()
    total = session.exec(count_query).one()

    return PhotoListResponse(
        items=[_photo_to_read(item) for item in items],
        total=int(total),
        limit=limit,
        offset=offset,
    )


# ---------- Detail / download / delete ----------


@router.get("/{photo_id}", response_model=PhotoRead, dependencies=[Depends(require_api_key)])
def get_photo(photo_id: int, session: Session = Depends(get_session)) -> PhotoRead:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return _photo_to_read(photo)


@router.get("/{photo_id}/file", dependencies=[Depends(require_api_key)])
def download_photo(photo_id: int, session: Session = Depends(get_session)) -> FileResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    full_path = photos_dir() / photo.file_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(full_path, media_type="image/jpeg", filename=full_path.name)


def _delete_photo_assets(photo: Photo) -> None:
    """Remove the source file and every cached thumbnail for the given photo."""
    full = photos_dir() / photo.file_path
    full.unlink(missing_ok=True)
    for size in THUMBNAIL_SIZES:
        thumbnail_path(photo.file_path, size).unlink(missing_ok=True)


@router.delete("/{photo_id}", dependencies=[Depends(require_api_key)])
def delete_photo(photo_id: int, session: Session = Depends(get_session)) -> dict:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    _delete_photo_assets(photo)
    session.delete(photo)
    session.commit()
    return {"deleted": photo_id}


# ---------- PATCH ----------


@router.patch("/{photo_id}", response_model=PhotoRead, dependencies=[Depends(require_api_key)])
def patch_photo(
    photo_id: int,
    payload: PhotoUpdate,
    session: Session = Depends(get_session),
) -> PhotoRead:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    if payload.is_favorite is not None:
        photo.is_favorite = payload.is_favorite
    if payload.tags is not None:
        set_photo_tags(session, photo, payload.tags)

    session.add(photo)
    session.commit()
    session.refresh(photo)
    return _photo_to_read(photo)


# ---------- Bulk delete ----------


@router.post("/bulk-delete", response_model=BulkDeleteResponse, dependencies=[Depends(require_api_key)])
def bulk_delete(
    payload: BulkDeleteRequest, session: Session = Depends(get_session)
) -> BulkDeleteResponse:
    deleted = 0
    requested = list(dict.fromkeys(payload.ids))  # de-dup, preserve order
    for photo_id in requested:
        photo = session.get(Photo, photo_id)
        if photo is None:
            continue
        _delete_photo_assets(photo)
        session.delete(photo)
        deleted += 1
    session.commit()
    return BulkDeleteResponse(deleted=deleted, not_found=len(requested) - deleted)
