"""/api/stats endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import Photo, StatsResponse

router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


@router.get("/stats", response_model=StatsResponse)
def stats(session: Session = Depends(get_session)) -> StatsResponse:
    total = int(session.exec(select(func.count()).select_from(Photo)).one())

    by_day_rows = session.exec(
        select(
            func.strftime("%Y-%m-%d", Photo.captured_at).label("day"),
            func.count(),
        ).group_by("day")
    ).all()
    by_day = {row[0]: int(row[1]) for row in by_day_rows if row[0] is not None}

    by_host_rows = session.exec(
        select(Photo.hostname, func.count()).group_by(Photo.hostname)
    ).all()
    by_hostname = {(row[0] or "unknown"): int(row[1]) for row in by_host_rows}

    return StatsResponse(total=total, by_day=by_day, by_hostname=by_hostname)
