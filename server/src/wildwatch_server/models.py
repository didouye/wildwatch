"""SQLModel definitions for the WildWatch catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
