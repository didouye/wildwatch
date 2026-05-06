"""Camera agent heartbeat endpoint (V1.2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from sqlmodel import Session, select

from wildwatch_server.db import get_session
from wildwatch_server.models import Camera
from wildwatch_server.rate_limit import HEARTBEAT_LIMIT, limiter
from wildwatch_server.storage import previews_dir

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
async def heartbeat(
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
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="status must be a JSON object")

    # --- Reconcile desired_config vs reported_config ---
    desired = (
        json.loads(cam.desired_config) if cam.desired_config else None
    )
    applied_at = parsed.get("applied_at")
    reported = parsed.get("reported_config") or {}

    apply_error_observed = False
    # Use truthiness check (not `is not None`) so an empty dict doesn't trigger
    # vacuous-truth success (`all(... for _ in {}) == True`).
    if desired and applied_at:
        # Agent claims it applied. Did it actually take effect?
        if all(reported.get(k) == v for k, v in desired.items()):
            # Success: clear desired
            cam.desired_config = None
            desired = None
        else:
            apply_error_observed = True

    # Inject the apply error flag into the stored heartbeat so the UI can render it.
    parsed.setdefault("capture", {})
    parsed["capture"]["apply_error_observed"] = apply_error_observed

    cam.last_heartbeat = json.dumps(parsed, separators=(",", ":"))
    cam.agent_last_seen_at = datetime.now(timezone.utc)
    session.add(cam)
    session.commit()

    if preview is not None:
        contents = await preview.read()
        if contents:
            target = previews_dir() / f"{cam.id}.jpg"
            tmp = target.with_suffix(".jpg.tmp")
            tmp.write_bytes(contents)
            tmp.replace(target)
    return {"desired_config": desired, "commands": []}
