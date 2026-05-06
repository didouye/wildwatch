"""Tests for the reorient.py module (rotate photos in place + swap dims)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from wildwatch_server import reorient
from wildwatch_server.models import Camera, Photo


@pytest.fixture
def setup_db_and_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "admin-key")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    from wildwatch_server import db as db_module
    db_module.reset_engine_cache()
    db_module.init_db()
    return db_module.get_engine()


def _seed_camera(engine, hostname: str = "rpi") -> Camera:
    with Session(engine) as s:
        cam = Camera(token="t", hostname=hostname, status="approved")
        s.add(cam)
        s.commit()
        s.refresh(cam)
        return cam


def _seed_photo(engine, camera_id: int, captured_at: datetime, w: int, h: int,
                 base: Path, name: str) -> Photo:
    """Create a JPEG on disk + a Photo row pointing at it."""
    rel = f"2026/05/06/{name}.jpg"
    full = base / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color=(255, 0, 0)).save(full, "JPEG", quality=95)
    with Session(engine) as s:
        photo = Photo(
            captured_at=captured_at,
            file_path=rel,
            file_size=full.stat().st_size,
            hostname="rpi",
            camera_id=camera_id,
            camera_width=w,
            camera_height=h,
        )
        s.add(photo)
        s.commit()
        s.refresh(photo)
        return photo


def test_pil_rotation_for_delta_inverts_sign() -> None:
    assert reorient._pil_rotation_for_delta(90) == 270  # 90 CW = 270 CCW
    assert reorient._pil_rotation_for_delta(180) == 180
    assert reorient._pil_rotation_for_delta(270) == 90  # 270 CW = 90 CCW


def test_reorient_rotates_photos_and_swaps_dims_for_90(
    setup_db_and_storage, tmp_path: Path
) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    photo = _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "p1")

    result = reorient.reorient_camera_photos(cam.id, delta=90, ack_time=now)

    assert result == {"rotated": 1, "skipped": 0, "total": 1}
    # File on disk: dimensions swapped.
    with Image.open(base / photo.file_path) as img:
        assert img.size == (100, 200)
    # DB: dims swapped.
    with Session(engine) as s:
        row = s.get(Photo, photo.id)
        assert row.camera_width == 100
        assert row.camera_height == 200


def test_reorient_180_keeps_dims(setup_db_and_storage, tmp_path: Path) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "p1")

    reorient.reorient_camera_photos(cam.id, delta=180, ack_time=now)

    with Session(engine) as s:
        row = s.exec(select(Photo)).first()
        assert (row.camera_width, row.camera_height) == (200, 100)


def test_reorient_skips_photos_after_ack_time(
    setup_db_and_storage, tmp_path: Path
) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    # One BEFORE ack, one AFTER ack.
    _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "before")
    _seed_photo(engine, cam.id, now + timedelta(hours=1), 200, 100, base, "after")

    result = reorient.reorient_camera_photos(cam.id, delta=180, ack_time=now)
    assert result["total"] == 1
    assert result["rotated"] == 1


def test_reorient_clears_pending_delta_when_done(
    setup_db_and_storage,
) -> None:
    engine = setup_db_and_storage
    with Session(engine) as s:
        cam = Camera(token="t", hostname="rpi", status="approved",
                     pending_reorient_delta=180)
        s.add(cam)
        s.commit()
        s.refresh(cam)
    now = datetime.now(timezone.utc)

    reorient.reorient_camera_photos(cam.id, delta=180, ack_time=now)

    with Session(engine) as s:
        assert s.get(Camera, cam.id).pending_reorient_delta is None


def test_reorient_deletes_cached_thumbnails(
    setup_db_and_storage, tmp_path: Path
) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    photo = _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "p1")
    # Pre-cache a thumb.
    from wildwatch_server.thumbnails import thumbnail_path
    thumb = thumbnail_path(photo.file_path, 150)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"fake-thumb")
    assert thumb.exists()

    reorient.reorient_camera_photos(cam.id, delta=90, ack_time=now)

    assert not thumb.exists()  # purged


def test_reorient_invalid_delta_raises(setup_db_and_storage) -> None:
    with pytest.raises(ValueError, match="delta must be"):
        reorient.reorient_camera_photos(camera_id=1, delta=45,
                                          ack_time=datetime.now(timezone.utc))
