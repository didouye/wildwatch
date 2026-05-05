"""Camera agent heartbeat endpoint (V1.2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from sqlmodel import Session, select

from wildwatch_server.db import get_session
from wildwatch_server.models import Camera
from wildwatch_server.rate_limit import HEARTBEAT_LIMIT, limiter

router = APIRouter(prefix="/api/cameras/agent")


def _camera_from_authz(session: Session, authorization: str | None) -> Camera:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    cam = session.exec(select(Camera).where(Camera.token == token)).first()
    if cam is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if cam.status != "approved":
        raise HTTPException(status_code=403, detail=f"Camera is {cam.status}")
    return cam


@router.post("/heartbeat")
@limiter.limit(HEARTBEAT_LIMIT)
def heartbeat(
    request: Request,
    response: Response,
    status: str = Form(...),
    preview: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict:
    cam = _camera_from_authz(session, authorization)
    try:
        parsed = json.loads(status)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="status must be valid JSON") from exc

    cam.last_heartbeat = json.dumps(parsed, separators=(",", ":"))
    cam.agent_last_seen_at = datetime.now(timezone.utc)
    session.add(cam)
    session.commit()
    return {"desired_config": None, "commands": []}
