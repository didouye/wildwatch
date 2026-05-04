"""/api/photos/* endpoints: upload, list, detail, download, delete, stats."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import (
    Photo,
    PhotoListResponse,
    PhotoRead,
)
from wildwatch_server.storage import photos_dir

router = APIRouter(prefix="/api/photos", dependencies=[Depends(require_api_key)])


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


@router.post("")
async def upload_photo(
    file: UploadFile = File(...),
    captured_at: str | None = Form(default=None),
    metadata: str | None = Form(default=None),
    session: Session = Depends(get_session),
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
    session.add(photo)
    session.commit()
    session.refresh(photo)

    return {
        "id": photo.id,
        "stored_path": f"data/photos/{relative}",
        "size_bytes": str(len(contents)),
        "received_at": now.isoformat(),
        "captured_at": captured_at_dt.isoformat(),
    }


def _parse_date_param(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{name} must be YYYY-MM-DD"
        ) from exc


@router.get("", response_model=PhotoListResponse)
def list_photos(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    hostname: str | None = Query(default=None),
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
    if hostname is not None:
        query = query.where(Photo.hostname == hostname)
        count_query = count_query.where(Photo.hostname == hostname)

    if order == "captured_at_asc":
        query = query.order_by(Photo.captured_at.asc())
    else:
        query = query.order_by(Photo.captured_at.desc())

    query = query.limit(limit).offset(offset)

    items = session.exec(query).all()
    total = session.exec(count_query).one()

    return PhotoListResponse(
        items=[PhotoRead.model_validate(item, from_attributes=True) for item in items],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{photo_id}", response_model=PhotoRead)
def get_photo(photo_id: int, session: Session = Depends(get_session)) -> PhotoRead:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return PhotoRead.model_validate(photo, from_attributes=True)


@router.get("/{photo_id}/file")
def download_photo(photo_id: int, session: Session = Depends(get_session)) -> FileResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    full_path = photos_dir() / photo.file_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(full_path, media_type="image/jpeg", filename=full_path.name)


@router.delete("/{photo_id}")
def delete_photo(photo_id: int, session: Session = Depends(get_session)) -> dict:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    full_path = photos_dir() / photo.file_path
    full_path.unlink(missing_ok=True)
    session.delete(photo)
    session.commit()
    return {"deleted": photo_id}


