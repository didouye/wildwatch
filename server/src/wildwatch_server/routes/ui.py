"""Server-rendered UI routes (no auth)."""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server import reorient as reorient_module
from wildwatch_server.auth_web import require_web_session
from wildwatch_server.db import get_session
from wildwatch_server.models import Camera, Photo, PhotoTagLink, Tag
from wildwatch_server.routes.photos import _delete_photo_assets, set_photo_tags
from wildwatch_server.storage import photos_dir, previews_dir
from wildwatch_server.versions import LATEST_AGENT_VERSION

router = APIRouter(dependencies=[Depends(require_web_session)])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PAGE_SIZE = 24


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/gallery", status_code=302)


@router.get("/preview/{camera_id}", include_in_schema=False)
def preview(camera_id: int) -> FileResponse:
    p = previews_dir() / f"{camera_id}.jpg"
    if not p.exists():
        raise HTTPException(status_code=404, detail="No preview yet")
    return FileResponse(
        p,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


def _parse_date(value: str | None) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc


def _bool_param(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


@router.get("/gallery", response_class=HTMLResponse)
def gallery(
    request: Request,
    page: int = Query(default=1, ge=1),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    hostname: str | None = Query(default=None),
    camera_id: int | None = Query(default=None),
    favorite: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    from_date = _parse_date(from_)
    to_date = _parse_date(to)
    favorite_only = _bool_param(favorite)

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
    if favorite_only:
        query = query.where(Photo.is_favorite.is_(True))
        count_query = count_query.where(Photo.is_favorite.is_(True))
    if tag:
        tag_q = (
            select(PhotoTagLink.photo_id)
            .join(Tag, Tag.id == PhotoTagLink.tag_id)
            .where(func.lower(Tag.name) == tag.lower())
        )
        query = query.where(Photo.id.in_(tag_q))
        count_query = count_query.where(Photo.id.in_(tag_q))

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
    all_tags = sorted(
        row[0] for row in session.exec(select(Tag.name).distinct()).all()
    )
    cameras_for_filter = session.exec(
        select(Camera).where(Camera.status != "revoked").order_by(Camera.hostname)
    ).all()

    base_qs = {}
    if from_:
        base_qs["from"] = from_
    if to:
        base_qs["to"] = to
    if hostname:
        base_qs["hostname"] = hostname
    if favorite_only:
        base_qs["favorite"] = "true"
    if tag:
        base_qs["tag"] = tag
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
        "all_tags": all_tags,
        "cameras_for_filter": cameras_for_filter,
        "filters": {
            "from_": from_,
            "to": to,
            "hostname": hostname,
            "camera_id": camera_id,
            "favorite": favorite_only,
            "tag": tag,
        },
    }

    template = (
        "_gallery_grid.html" if request.headers.get("HX-Request") else "gallery.html"
    )
    return templates.TemplateResponse(request=request, name=template, context=context)


# ---------- Photo detail ----------


@router.get("/photos/{photo_id}", response_class=HTMLResponse)
def photo_detail(
    photo_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

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
            "tag_names": ", ".join(sorted(t.name for t in photo.tags)),
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
    return FileResponse(full_path, media_type="image/jpeg", filename=full_path.name)


# ---------- Favorites ----------


@router.post("/photos/{photo_id}/favorite", response_class=HTMLResponse)
def toggle_favorite(
    photo_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    photo.is_favorite = not photo.is_favorite
    session.add(photo)
    session.commit()
    session.refresh(photo)
    # htmx returns just the updated star fragment; full-page POST falls back to detail.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_favorite_button.html",
            context={"photo": photo},
        )
    return HTMLResponse(
        f"<span data-favorite='{photo.is_favorite}'>{'star' if photo.is_favorite else ''}</span>"
    )


# ---------- Tags ----------


@router.post("/photos/{photo_id}/tags", response_class=HTMLResponse)
def update_tags(
    photo_id: int,
    request: Request,
    names: str = Form(default=""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    parsed = [n.strip() for n in names.split(",") if n.strip()]
    set_photo_tags(session, photo, parsed)
    session.add(photo)
    session.commit()
    session.refresh(photo)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request,
            name="_tag_chips.html",
            context={"photo": photo, "tag_names": ", ".join(sorted(t.name for t in photo.tags))},
        )
    return HTMLResponse(",".join(sorted(t.name for t in photo.tags)))


# ---------- Share ----------


@router.post("/photos/{photo_id}/share")
def create_share(
    photo_id: int, request: Request, session: Session = Depends(get_session)
):
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    photo.share_token = secrets.token_urlsafe(24)
    session.add(photo)
    session.commit()
    session.refresh(photo)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request, name="_share_panel.html", context={"photo": photo}
        )
    return {"token": photo.share_token, "url": f"/share/{photo.share_token}"}


@router.post("/photos/{photo_id}/unshare")
def revoke_share(
    photo_id: int, request: Request, session: Session = Depends(get_session)
):
    photo = session.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    photo.share_token = None
    session.add(photo)
    session.commit()
    session.refresh(photo)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request=request, name="_share_panel.html", context={"photo": photo}
        )
    return {"unshared": photo_id}


# ---------- Bulk actions ----------


@router.post("/photos/bulk")
def bulk_action(
    action: str = Form(...),
    ids: list[int] = Form(default=[]),
    session: Session = Depends(get_session),
) -> dict:
    if action not in {"delete", "favorite", "unfavorite"}:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    affected = 0
    for photo_id in ids:
        photo = session.get(Photo, photo_id)
        if photo is None:
            continue
        if action == "delete":
            _delete_photo_assets(photo)
            session.delete(photo)
        elif action == "favorite":
            photo.is_favorite = True
            session.add(photo)
        elif action == "unfavorite":
            photo.is_favorite = False
            session.add(photo)
        affected += 1
    session.commit()
    return {"action": action, "affected": affected}


# ---------- Stats page ----------


# ---------- Cameras admin page ----------


def _photo_count(session: Session, cam: Camera) -> int:
    return int(
        session.exec(
            select(func.count()).select_from(Photo).where(Photo.camera_id == cam.id)
        ).one()
    )


def _camera_card_context(cam: Camera, session: Session) -> dict:
    """Compute fields the card template needs (parses last_heartbeat)."""
    hb = json.loads(cam.last_heartbeat) if cam.last_heartbeat else {}
    now = datetime.now(timezone.utc)
    agent_seen = cam.agent_last_seen_at
    if agent_seen is not None and agent_seen.tzinfo is None:
        agent_seen = agent_seen.replace(tzinfo=timezone.utc)
    agent_age_s = (now - agent_seen).total_seconds() if agent_seen else None
    agent_online = agent_age_s is not None and agent_age_s < 60

    capture_block = hb.get("capture") or {}
    capture_online = (
        agent_online
        and bool(capture_block.get("service_active"))
        and (capture_block.get("status_age_s") or 999) < 30
    )

    reported_version = (hb.get("agent") or {}).get("version")
    if reported_version is None:
        agent_status = "none"
    elif reported_version != LATEST_AGENT_VERSION:
        agent_status = "outdated"
    else:
        agent_status = "current"

    desired = json.loads(cam.desired_config) if cam.desired_config else None
    apply_error = bool(capture_block.get("apply_error_observed"))
    config_diff = None
    if desired:
        reported = hb.get("reported_config") or {}
        config_diff = [
            {"field": k, "from": reported.get(k, "?"), "to": v}
            for k, v in desired.items()
        ]

    reorient_progress = reorient_module.reorient_progress.get(cam.id)

    return {
        "cam": cam,
        "hb": hb,
        "agent_online": agent_online,
        "agent_age_s": agent_age_s,
        "capture_online": capture_online,
        "photo_count": _photo_count(session, cam),
        "agent_status": agent_status,
        "reported_agent_version": reported_version,
        "latest_agent_version": LATEST_AGENT_VERSION,
        "desired_config": desired,
        "config_diff": config_diff,
        "apply_error_observed": apply_error,
        "reorient_pending_delta": cam.pending_reorient_delta,
        "reorient_progress": reorient_progress,  # tuple (done, total) or None
    }


@router.get("/cameras/{camera_id}/card", response_class=HTMLResponse)
def camera_card(
    camera_id: int, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="_camera_card.html",
        context=_camera_card_context(cam, session),
    )


# Allowed fields and their casts (form values arrive as strings).
_CONFIG_FIELDS = {
    "rotation": int,
    "capture_width": int,
    "capture_height": int,
    "detection_width": int,
    "detection_height": int,
    "pixel_threshold": int,
    "area_threshold": float,
    "background_alpha": float,
    "warmup_frames": int,
    "cooldown_seconds": float,
    "burst_count": int,
    "burst_interval_seconds": float,
}


def _validate_config_form(form: dict) -> dict:
    """Cast form values to the right types and validate ranges."""
    parsed: dict = {}
    for name, caster in _CONFIG_FIELDS.items():
        raw = form.get(name)
        if raw is None or raw == "":
            raise HTTPException(status_code=400, detail=f"Missing field: {name}")
        try:
            parsed[name] = caster(raw)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid value for {name}: {raw}"
            ) from exc

    # Range checks
    if parsed["rotation"] not in {0, 90, 180, 270}:
        raise HTTPException(status_code=400, detail="rotation must be 0, 90, 180, or 270")
    if parsed["capture_width"] <= 0 or parsed["capture_height"] <= 0:
        raise HTTPException(status_code=400, detail="capture dimensions must be positive")
    if parsed["detection_width"] <= 0 or parsed["detection_height"] <= 0:
        raise HTTPException(status_code=400, detail="detection dimensions must be positive")
    for dim in ("capture_width", "capture_height", "detection_width", "detection_height"):
        if parsed[dim] > 8192:
            raise HTTPException(status_code=400, detail=f"{dim} must be <= 8192")
    if parsed["pixel_threshold"] < 0 or parsed["pixel_threshold"] > 255:
        raise HTTPException(status_code=400, detail="pixel_threshold must be in [0, 255]")
    if not (0 < parsed["area_threshold"] <= 1):
        raise HTTPException(status_code=400, detail="area_threshold must be in (0, 1]")
    if not (0 < parsed["background_alpha"] <= 1):
        raise HTTPException(status_code=400, detail="background_alpha must be in (0, 1]")
    if parsed["warmup_frames"] < 0:
        raise HTTPException(status_code=400, detail="warmup_frames must be >= 0")
    if parsed["cooldown_seconds"] < 0:
        raise HTTPException(status_code=400, detail="cooldown_seconds must be >= 0")
    if parsed["burst_count"] < 1:
        raise HTTPException(status_code=400, detail="burst_count must be >= 1")
    if parsed["burst_interval_seconds"] < 0:
        raise HTTPException(status_code=400, detail="burst_interval_seconds must be >= 0")
    return parsed


def _diff_against_reported(submitted: dict, reported: dict | None) -> dict:
    """Return only the fields where submitted differs from reported.

    Empty or missing `reported` -> all submitted fields are considered
    changed (returned as-is). This matters for fresh cameras that haven't
    sent a heartbeat yet: we want the operator's first config submission
    to populate every field into desired_config, not silently drop them.

    For non-empty `reported`, fields not present in `reported` are skipped
    (we have nothing to diff against)."""
    if not reported:
        return submitted
    return {k: v for k, v in submitted.items() if k in reported and reported[k] != v}


@router.post("/cameras/{camera_id}/config", response_class=HTMLResponse)
async def post_camera_config(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    form = await request.form()
    submitted = _validate_config_form(form)

    hb = json.loads(cam.last_heartbeat) if cam.last_heartbeat else {}
    reported = (hb.get("reported_config") or {}) if hb else {}
    diff = _diff_against_reported(submitted, reported)

    # Reorient flag: only set pending_reorient_delta if rotation actually changes.
    reorient_existing = form.get("reorient_existing") in ("on", "1", "true", "yes")
    pending_delta: int | None = None
    if reorient_existing and "rotation" in diff:
        new_rot = diff["rotation"]
        old_rot = reported.get("rotation") or 0
        delta = (new_rot - old_rot) % 360
        if delta in (90, 180, 270):
            pending_delta = delta

    cam.desired_config = json.dumps(diff) if diff else None
    cam.pending_reorient_delta = pending_delta
    session.add(cam)
    session.commit()
    session.refresh(cam)

    # Return the freshly-rendered card fragment for htmx swap.
    return templates.TemplateResponse(
        request=request,
        name="_camera_card.html",
        context=_camera_card_context(cam, session),
    )


@router.post("/cameras/{camera_id}/config/cancel", response_class=HTMLResponse)
def cancel_camera_config(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    cam.desired_config = None
    session.add(cam)
    session.commit()
    session.refresh(cam)
    return templates.TemplateResponse(
        request=request,
        name="_camera_card.html",
        context=_camera_card_context(cam, session),
    )


@router.get("/cameras", response_class=HTMLResponse)
def cameras_page(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    rows = session.exec(select(Camera).order_by(Camera.enrolled_at.desc())).all()
    pending = [
        {"cam": c, "photo_count": _photo_count(session, c)}
        for c in rows
        if c.status == "pending"
    ]
    revoked = [
        {"cam": c, "photo_count": _photo_count(session, c)}
        for c in rows
        if c.status == "revoked"
    ]
    approved = [
        _camera_card_context(c, session)
        for c in rows
        if c.status == "approved"
    ]
    server_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request=request,
        name="cameras.html",
        context={
            "pending": pending,
            "approved": approved,
            "revoked": revoked,
            "total": len(rows),
            "server_url": server_url,
        },
    )


@router.post("/cameras/{camera_id}/approve", response_class=HTMLResponse)
def approve_camera_ui(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    from datetime import datetime, timezone

    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404)
    cam.status = "approved"
    cam.approved_at = datetime.now(timezone.utc)
    session.add(cam)
    session.commit()
    return RedirectResponse(url="/cameras", status_code=302)


@router.post("/cameras/{camera_id}/revoke", response_class=HTMLResponse)
def revoke_camera_ui(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404)
    cam.status = "revoked"
    session.add(cam)
    session.commit()
    return RedirectResponse(url="/cameras", status_code=302)


@router.post("/cameras/{camera_id}/rename", response_class=HTMLResponse)
def rename_camera_ui(
    camera_id: int,
    request: Request,
    display_name: str = Form(default=""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404)
    cam.display_name = display_name.strip() or None
    session.add(cam)
    session.commit()
    return RedirectResponse(url="/cameras", status_code=302)


@router.post("/cameras/{camera_id}/delete", response_class=HTMLResponse)
def delete_camera_ui(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404)
    photos = session.exec(select(Photo).where(Photo.camera_id == camera_id)).all()
    for p in photos:
        p.camera_id = None
        session.add(p)
    session.delete(cam)
    session.commit()
    return RedirectResponse(url="/cameras", status_code=302)


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    total = int(session.exec(select(func.count()).select_from(Photo)).one())

    by_day_rows = session.exec(
        select(
            func.strftime("%Y-%m-%d", Photo.captured_at).label("day"),
            func.count(),
        ).group_by("day").order_by("day")
    ).all()
    by_day = [{"day": r[0], "count": int(r[1])} for r in by_day_rows if r[0]]

    by_hour_rows = session.exec(
        select(
            func.strftime("%H", Photo.captured_at).label("hour"),
            func.count(),
        ).group_by("hour")
    ).all()
    by_hour = {f"{h:02d}": 0 for h in range(24)}
    for r in by_hour_rows:
        if r[0]:
            by_hour[r[0]] = int(r[1])

    by_host_rows = session.exec(
        select(Photo.hostname, func.count()).group_by(Photo.hostname)
    ).all()
    by_hostname = [
        {"hostname": (r[0] or "unknown"), "count": int(r[1])} for r in by_host_rows
    ]

    by_tag_rows = session.exec(
        select(Tag.name, func.count(PhotoTagLink.photo_id))
        .join(PhotoTagLink, PhotoTagLink.tag_id == Tag.id)
        .group_by(Tag.name)
        .order_by(func.count(PhotoTagLink.photo_id).desc())
    ).all()
    by_tag = [{"name": r[0], "count": int(r[1])} for r in by_tag_rows]

    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={
            "total": total,
            "by_day": by_day,
            "by_hour": by_hour,
            "by_hostname": by_hostname,
            "by_tag": by_tag,
        },
    )
