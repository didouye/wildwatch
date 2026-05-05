"""Camera enrollment, listing, approval, revocation."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import (
    Camera,
    CameraEnrollRequest,
    CameraEnrollResponse,
    CameraRead,
    CameraSelfRead,
    CameraUpdate,
    Photo,
)
from wildwatch_server.rate_limit import ENROLL_LIMIT, limiter

# Public router: enrollment is open by design (admin approves afterwards).
public_router = APIRouter(prefix="/api/cameras")


@public_router.post(
    "/enroll", response_model=CameraEnrollResponse, status_code=201
)
@limiter.limit(ENROLL_LIMIT)
def enroll(
    request: Request,
    response: Response,
    payload: CameraEnrollRequest,
    session: Session = Depends(get_session),
) -> CameraEnrollResponse:
    """Open enrollment endpoint. Creates a pending camera and returns its
    token so the RPi can authenticate future requests."""
    token = secrets.token_urlsafe(32)
    camera = Camera(
        token=token,
        hostname=payload.hostname,
        status="pending",
        notes=None,  # system info dropped on the floor for now
    )
    session.add(camera)
    session.commit()
    session.refresh(camera)
    return CameraEnrollResponse(
        id=camera.id,
        token=camera.token,
        status=camera.status,
        hostname=camera.hostname,
        display_name=camera.display_name,
        enrolled_at=camera.enrolled_at,
    )


@public_router.get("/me", response_model=CameraSelfRead)
def get_self(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> CameraSelfRead:
    """Used by the RPi to detect approval after a 403."""
    camera = _camera_from_authorization(session, authorization)
    if camera is None:
        raise HTTPException(status_code=401, detail="Unknown camera token")
    return CameraSelfRead(
        id=camera.id,
        hostname=camera.hostname,
        display_name=camera.display_name,
        status=camera.status,
    )


def _camera_from_authorization(
    session: Session, authorization: str | None
) -> Camera | None:
    """Return the Camera matching a Bearer token, or None.

    The legacy `WILDWATCH_API_KEY` is intentionally not accepted here --
    only camera tokens are valid for /api/cameras/me.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    return session.exec(select(Camera).where(Camera.token == token)).first()


# Admin router: list / patch / delete. Protected by the existing API key gate.
admin_router = APIRouter(
    prefix="/api/cameras", dependencies=[Depends(require_api_key)]
)


@admin_router.get("", response_model=list[CameraRead])
def list_cameras(
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[CameraRead]:
    query = select(
        Camera,
        func.count(Photo.id).label("photo_count"),
    ).outerjoin(Photo, Photo.camera_id == Camera.id).group_by(Camera.id)
    if status:
        query = query.where(Camera.status == status)
    query = query.order_by(Camera.enrolled_at.desc())
    rows = session.exec(query).all()
    return [
        CameraRead(
            id=cam.id,
            hostname=cam.hostname,
            display_name=cam.display_name,
            status=cam.status,
            enrolled_at=cam.enrolled_at,
            approved_at=cam.approved_at,
            last_seen_at=cam.last_seen_at,
            notes=cam.notes,
            photo_count=int(count),
        )
        for cam, count in rows
    ]


@admin_router.patch("/{camera_id}", response_model=CameraRead)
def patch_camera(
    camera_id: int,
    payload: CameraUpdate,
    session: Session = Depends(get_session),
) -> CameraRead:
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    if payload.status is not None:
        if payload.status not in {"pending", "approved", "revoked"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        if payload.status == "approved" and camera.status != "approved":
            camera.approved_at = datetime.now(timezone.utc)
        camera.status = payload.status
    if payload.display_name is not None:
        camera.display_name = payload.display_name or None
    if payload.notes is not None:
        camera.notes = payload.notes or None

    session.add(camera)
    session.commit()
    session.refresh(camera)

    photo_count = int(
        session.exec(
            select(func.count()).select_from(Photo).where(Photo.camera_id == camera.id)
        ).one()
    )
    return CameraRead(
        id=camera.id,
        hostname=camera.hostname,
        display_name=camera.display_name,
        status=camera.status,
        enrolled_at=camera.enrolled_at,
        approved_at=camera.approved_at,
        last_seen_at=camera.last_seen_at,
        notes=camera.notes,
        photo_count=photo_count,
    )


@admin_router.delete("/{camera_id}")
def delete_camera(
    camera_id: int, session: Session = Depends(get_session)
) -> dict:
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    # Photos.camera_id FK has ON DELETE SET NULL -- but SQLite doesn't enforce
    # FKs by default, so we null them out explicitly first.
    photos = session.exec(select(Photo).where(Photo.camera_id == camera_id)).all()
    for p in photos:
        p.camera_id = None
        session.add(p)
    session.delete(camera)
    session.commit()
    return {"deleted": camera_id, "photos_orphaned": len(photos)}
