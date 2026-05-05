"""/api/tags endpoints (list + delete)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from wildwatch_server.auth import require_api_key
from wildwatch_server.db import get_session
from wildwatch_server.models import PhotoTagLink, Tag, TagRead

router = APIRouter(prefix="/api/tags", dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[TagRead])
def list_tags(session: Session = Depends(get_session)) -> list[TagRead]:
    rows = session.exec(
        select(Tag, func.count(PhotoTagLink.photo_id))
        .outerjoin(PhotoTagLink, PhotoTagLink.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(Tag.name)
    ).all()
    out: list[TagRead] = []
    for tag, photo_count in rows:
        out.append(
            TagRead(
                id=tag.id,
                name=tag.name,
                color=tag.color,
                photo_count=int(photo_count),
            )
        )
    return out


@router.delete("/{tag_id}")
def delete_tag(tag_id: int, session: Session = Depends(get_session)) -> dict:
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    session.delete(tag)  # cascades to photo_tags via ON DELETE CASCADE
    session.commit()
    return {"deleted": tag_id}
