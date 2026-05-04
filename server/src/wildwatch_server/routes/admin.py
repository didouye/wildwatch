"""Admin endpoints (reindex, etc.)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import ReindexResponse
from wildwatch_server.reindex import reindex

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_api_key)])


@router.post("/reindex", response_model=ReindexResponse)
def admin_reindex(session: Session = Depends(get_session)) -> ReindexResponse:
    result = reindex(session)
    return ReindexResponse(**result)
