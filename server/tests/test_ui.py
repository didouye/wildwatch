"""Tests for the server-rendered UI routes (gallery, detail, download) and
the thumbnail HTTP endpoints (/thumb, /api/admin/regen_thumbnails)."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, SQLModel

from wildwatch_server import db as db_module


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "secret-key-123")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    db_module.reset_engine_cache()
    import wildwatch_server.main as main_module

    importlib.reload(main_module)
    SQLModel.metadata.create_all(db_module.get_engine())
    return main_module.app, db_module.get_engine()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app, _ = _reload_app(tmp_path, monkeypatch)
    return TestClient(app)


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, engine = _reload_app(tmp_path, monkeypatch)
    with Session(engine) as s:
        yield s


def _seed_photo(
    session: Session,
    tmp_path: Path,
    relative: str = "2026/05/04/photo.jpg",
    captured_at: datetime | None = None,
    hostname: str = "DietPi",
):
    from wildwatch_server.models import Photo

    src = tmp_path / "photos" / relative
    src.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1200, 800), color=(40, 80, 200))
    img.save(src, "JPEG", quality=70)

    photo = Photo(
        captured_at=captured_at or datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        file_path=relative,
        file_size=src.stat().st_size,
        hostname=hostname,
        motion_score=0.14,
    )
    session.add(photo)
    session.commit()
    session.refresh(photo)
    return photo


# ---------- /thumb endpoint ----------


def test_thumb_returns_image(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    res = client.get(f"/thumb/150/{photo.id}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    # Cache header set
    assert "max-age" in res.headers.get("cache-control", "")


def test_thumb_invalid_size_returns_400(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    res = client.get(f"/thumb/999/{photo.id}")
    assert res.status_code == 400


def test_thumb_unknown_id_404(client: TestClient) -> None:
    res = client.get("/thumb/150/99999")
    assert res.status_code == 404


def test_thumb_lazy_generates_when_missing(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    photo = _seed_photo(session, tmp_path)
    # Force first call to generate the cache file
    res1 = client.get(f"/thumb/400/{photo.id}")
    assert res1.status_code == 200

    # Manually delete the cache and confirm the next request regenerates
    from wildwatch_server.thumbnails import thumbnail_path

    cache = thumbnail_path(photo.file_path, 400)
    assert cache.exists()
    cache.unlink()

    res2 = client.get(f"/thumb/400/{photo.id}")
    assert res2.status_code == 200
    assert cache.exists()


# ---------- /gallery and /photos/{id} ----------


def test_gallery_full_page_renders(client: TestClient, session: Session, tmp_path: Path) -> None:
    _seed_photo(session, tmp_path)
    res = client.get("/gallery")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "WildWatch" in res.text
    assert "Gallery" in res.text


def test_gallery_htmx_returns_partial(client: TestClient, session: Session, tmp_path: Path) -> None:
    _seed_photo(session, tmp_path)
    res = client.get("/gallery", headers={"HX-Request": "true"})
    assert res.status_code == 200
    # Partial does NOT include the full <html> page chrome.
    assert "<html" not in res.text.lower()
    # Partial DOES include the grid wrapper id.
    assert "gallery-grid-wrapper" in res.text


def test_gallery_filters_by_hostname(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    _seed_photo(session, tmp_path, relative="a.jpg", hostname="DietPi")
    _seed_photo(session, tmp_path, relative="b.jpg", hostname="OtherPi")

    res = client.get("/gallery?hostname=OtherPi")
    assert res.status_code == 200
    assert "OtherPi" in res.text or "1 photo" in res.text


def test_gallery_filters_by_date_range(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    base = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    _seed_photo(session, tmp_path, relative="a.jpg", captured_at=base)
    _seed_photo(session, tmp_path, relative="b.jpg", captured_at=base + timedelta(days=10))

    # Only the second photo
    res = client.get("/gallery?from=2026-05-05")
    assert "1 photo" in res.text


def test_root_redirects_to_gallery(client: TestClient) -> None:
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/gallery"


def test_photo_detail_renders(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    res = client.get(f"/photos/{photo.id}")
    assert res.status_code == 200
    assert f"Photo #{photo.id}" in res.text
    # Detail uses the 800px thumbnail
    assert f"/thumb/800/{photo.id}" in res.text


def test_photo_detail_404(client: TestClient) -> None:
    res = client.get("/photos/99999")
    assert res.status_code == 404


def test_photo_download_serves_original_without_auth(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    photo = _seed_photo(session, tmp_path)
    res = client.get(f"/photos/{photo.id}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


# ---------- /api/admin/regen_thumbnails ----------


def test_regen_thumbnails_creates_missing(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    _seed_photo(session, tmp_path)

    res = client.post(
        "/api/admin/regen_thumbnails",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    body = res.json()
    # 3 thumbnails generated for the seeded photo
    assert body["generated"] == 3
    assert body["skipped"] == 0


def test_regen_thumbnails_idempotent(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    _seed_photo(session, tmp_path)
    headers = {"Authorization": "Bearer secret-key-123"}
    client.post("/api/admin/regen_thumbnails", headers=headers)

    second = client.post("/api/admin/regen_thumbnails", headers=headers).json()
    assert second["generated"] == 0
    assert second["skipped"] == 3


# ---------- Upload schedules background thumbnails ----------


def test_upload_creates_thumbnails_eventually(
    client: TestClient, tmp_path: Path
) -> None:
    """After POST /api/photos, BackgroundTasks should have produced the thumbs.

    TestClient runs background tasks synchronously after the response, so they
    are guaranteed to have executed once the call returns.
    """
    img_buf = tmp_path / "src.jpg"
    Image.new("RGB", (1200, 800), color=(80, 200, 120)).save(img_buf, "JPEG")
    res = client.post(
        "/api/photos",
        files={"file": ("src.jpg", img_buf.read_bytes(), "image/jpeg")},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    photo_id = res.json()["id"]

    # Each thumb size should already exist on disk.
    detail = client.get(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    from wildwatch_server.thumbnails import thumbnail_path

    for size in (150, 400, 800):
        assert thumbnail_path(detail["file_path"], size).exists(), (
            f"thumbnail size={size} missing"
        )
