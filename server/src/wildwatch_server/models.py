"""SQLModel definitions for the WildWatch catalog.

Note: this module intentionally avoids `from __future__ import annotations`
because SQLModel's `Relationship(...)` resolves the annotated type at class
construction time. With deferred annotations, `list["Tag"]` becomes a string
forward-ref that SQLAlchemy refuses, expecting `Mapped[...]` instead.
"""

from datetime import datetime, timezone

from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PhotoTagLink(SQLModel, table=True):
    """Many-to-many link table between photos and tags."""

    __tablename__ = "photo_tags"

    photo_id: int = Field(foreign_key="photos.id", primary_key=True, ondelete="CASCADE")
    tag_id: int = Field(foreign_key="tags.id", primary_key=True, ondelete="CASCADE")


class Photo(SQLModel, table=True):
    """One row per ingested photo.

    The metadata columns mirror the JSON sidecar produced by the capture
    client. They are all nullable so a photo can be ingested even if a
    field is missing (older clients, partial reindex, etc.).
    """

    __tablename__ = "photos"

    id: int | None = Field(default=None, primary_key=True)

    captured_at: datetime = Field(index=True)
    received_at: datetime = Field(default_factory=utcnow)

    file_path: str = Field(unique=True, index=True)
    file_size: int

    hostname: str | None = Field(default=None, index=True)

    # Motion detector context
    motion_score: float | None = None
    frame_index: int | None = None
    burst_size: int | None = None

    # Camera config
    camera_width: int | None = None
    camera_height: int | None = None

    # Sensor metadata from picamera2
    sensor_model: str | None = None
    exposure_time_us: int | None = None
    analogue_gain: float | None = None
    lux: float | None = None

    # System probes from the RPi
    cpu_temp: float | None = None
    memory_avail_mb: float | None = None
    load_avg_1min: float | None = None

    # V0.5 -- curation
    is_favorite: bool = Field(default=False, index=True)
    share_token: str | None = Field(default=None, unique=True, index=True)

    tags: list["Tag"] = Relationship(back_populates="photos", link_model=PhotoTagLink)


class Tag(SQLModel, table=True):
    """Free-form label attached to one or more photos."""

    __tablename__ = "tags"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)  # COLLATE NOCASE applied at SQL level
    color: str | None = None

    photos: list[Photo] = Relationship(back_populates="tags", link_model=PhotoTagLink)


class PhotoRead(SQLModel):
    """API response model for a single photo."""

    id: int
    captured_at: datetime
    received_at: datetime
    file_path: str
    file_size: int
    hostname: str | None = None
    motion_score: float | None = None
    frame_index: int | None = None
    burst_size: int | None = None
    camera_width: int | None = None
    camera_height: int | None = None
    sensor_model: str | None = None
    exposure_time_us: int | None = None
    analogue_gain: float | None = None
    lux: float | None = None
    cpu_temp: float | None = None
    memory_avail_mb: float | None = None
    load_avg_1min: float | None = None
    is_favorite: bool = False
    share_token: str | None = None
    tags: list[str] = []


class PhotoUpdate(SQLModel):
    """Body of PATCH /api/photos/{id}."""

    is_favorite: bool | None = None
    tags: list[str] | None = None


class PhotoListResponse(SQLModel):
    items: list[PhotoRead]
    total: int
    limit: int
    offset: int


class StatsResponse(SQLModel):
    total: int
    by_day: dict[str, int]
    by_hostname: dict[str, int]


class ReindexResponse(SQLModel):
    scanned: int
    inserted: int
    skipped: int


class TagRead(SQLModel):
    id: int
    name: str
    color: str | None = None
    photo_count: int = 0


class BulkDeleteRequest(SQLModel):
    ids: list[int]


class BulkDeleteResponse(SQLModel):
    deleted: int
    not_found: int
