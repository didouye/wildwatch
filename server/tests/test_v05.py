"""Tests for V0.5 features: favorites, tags, share, bulk delete, stats."""

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
    Image.new("RGB", (1200, 800), color=(40, 80, 200)).save(src, "JPEG", quality=70)

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


# =====================================================
# Favorites
# =====================================================


def test_favorite_toggle_endpoint(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    assert photo.is_favorite is False

    res = client.post(f"/photos/{photo.id}/favorite")
    assert res.status_code == 200

    session.refresh(photo)
    assert photo.is_favorite is True

    res = client.post(f"/photos/{photo.id}/favorite")
    assert res.status_code == 200
    session.refresh(photo)
    assert photo.is_favorite is False


def test_favorite_toggle_unknown_photo_returns_404(client: TestClient) -> None:
    res = client.post("/photos/9999/favorite")
    assert res.status_code == 404


def test_filter_gallery_by_favorite(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    p1 = _seed_photo(session, tmp_path, relative="a.jpg")
    _seed_photo(session, tmp_path, relative="b.jpg")
    p1.is_favorite = True
    session.add(p1)
    session.commit()

    res = client.get("/gallery?favorite=true")
    assert res.status_code == 200
    assert "1 photo" in res.text


def test_patch_photo_sets_favorite(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    res = client.patch(
        f"/api/photos/{photo.id}",
        json={"is_favorite": True},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    assert res.json()["is_favorite"] is True


# =====================================================
# Tags
# =====================================================


def test_set_tags_creates_new_tag(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    res = client.post(f"/photos/{photo.id}/tags", data={"names": "bird,sunset"})
    assert res.status_code == 200

    detail = client.get(
        f"/api/photos/{photo.id}",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    assert sorted(detail["tags"]) == ["bird", "sunset"]


def test_set_tags_reuses_existing_case_insensitive(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    photo1 = _seed_photo(session, tmp_path, relative="a.jpg")
    photo2 = _seed_photo(session, tmp_path, relative="b.jpg")

    client.post(f"/photos/{photo1.id}/tags", data={"names": "Bird"})
    client.post(f"/photos/{photo2.id}/tags", data={"names": "bird"})

    tags = client.get("/api/tags", headers={"Authorization": "Bearer secret-key-123"}).json()
    bird_rows = [t for t in tags if t["name"].lower() == "bird"]
    # Single tag row, even though case differs
    assert len(bird_rows) == 1
    assert bird_rows[0]["photo_count"] == 2


def test_set_tags_replaces_full_set(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    client.post(f"/photos/{photo.id}/tags", data={"names": "bird,fox"})
    client.post(f"/photos/{photo.id}/tags", data={"names": "fox"})  # bird removed

    detail = client.get(
        f"/api/photos/{photo.id}",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    assert detail["tags"] == ["fox"]


def test_filter_gallery_by_tag(client: TestClient, session: Session, tmp_path: Path) -> None:
    p1 = _seed_photo(session, tmp_path, relative="a.jpg")
    _seed_photo(session, tmp_path, relative="b.jpg")
    client.post(f"/photos/{p1.id}/tags", data={"names": "fox"})

    res = client.get("/gallery?tag=fox")
    assert res.status_code == 200
    assert "1 photo" in res.text


def test_delete_tag_unlinks_photos_but_keeps_them(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    photo = _seed_photo(session, tmp_path)
    client.post(f"/photos/{photo.id}/tags", data={"names": "bird"})

    tags = client.get("/api/tags", headers={"Authorization": "Bearer secret-key-123"}).json()
    bird_id = next(t["id"] for t in tags if t["name"] == "bird")

    res = client.delete(
        f"/api/tags/{bird_id}", headers={"Authorization": "Bearer secret-key-123"}
    )
    assert res.status_code == 200

    # Photo still exists, just with no tags now
    detail = client.get(
        f"/api/photos/{photo.id}",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    assert detail["tags"] == []


# =====================================================
# Public share
# =====================================================


def test_share_creates_token_and_view_works(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    photo = _seed_photo(session, tmp_path)

    # Initially no token
    res = client.post(f"/photos/{photo.id}/share")
    assert res.status_code == 200
    body = res.json()
    token = body["token"]
    assert isinstance(token, str) and len(token) > 16

    # Public view returns the photo
    public = client.get(f"/share/{token}")
    assert public.status_code == 200
    assert "image" in public.text.lower() or f"/share/{token}/thumb/800" in public.text


def test_share_view_strips_private_metadata(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    photo = _seed_photo(session, tmp_path, hostname="MyPrivateRPi")
    photo_id = photo.id
    token = client.post(f"/photos/{photo_id}/share").json()["token"]

    public = client.get(f"/share/{token}")
    assert "MyPrivateRPi" not in public.text


def test_unshare_invalidates_token(client: TestClient, session: Session, tmp_path: Path) -> None:
    photo = _seed_photo(session, tmp_path)
    token = client.post(f"/photos/{photo.id}/share").json()["token"]

    # Token works initially
    assert client.get(f"/share/{token}").status_code == 200

    res = client.post(f"/photos/{photo.id}/unshare")
    assert res.status_code == 200

    # Token now returns 404
    assert client.get(f"/share/{token}").status_code == 404


def test_share_unknown_token_returns_404(client: TestClient) -> None:
    res = client.get("/share/nonexistent-token-xxxx")
    assert res.status_code == 404


# =====================================================
# Bulk delete
# =====================================================


def test_bulk_delete_removes_selected(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    p1 = _seed_photo(session, tmp_path, relative="a.jpg")
    p2 = _seed_photo(session, tmp_path, relative="b.jpg")
    p3 = _seed_photo(session, tmp_path, relative="c.jpg")

    res = client.post(
        "/api/photos/bulk-delete",
        json={"ids": [p1.id, p3.id]},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["deleted"] == 2

    # Verify only p2 remains
    listed = client.get(
        "/api/photos", headers={"Authorization": "Bearer secret-key-123"}
    ).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == p2.id


def test_bulk_delete_ignores_unknown_ids(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    p1 = _seed_photo(session, tmp_path)
    res = client.post(
        "/api/photos/bulk-delete",
        json={"ids": [p1.id, 9999]},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    body = res.json()
    assert body["deleted"] == 1
    assert body["not_found"] == 1


# =====================================================
# Stats page
# =====================================================


def test_stats_page_renders(client: TestClient, session: Session, tmp_path: Path) -> None:
    base = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    _seed_photo(session, tmp_path, relative="a.jpg", captured_at=base)
    _seed_photo(session, tmp_path, relative="b.jpg", captured_at=base + timedelta(days=1))

    res = client.get("/stats")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    # Chart.js loaded via CDN
    assert "chart.js" in res.text.lower() or "chart.umd" in res.text.lower()
    # Some canvas elements should be there
    assert "<canvas" in res.text


# =====================================================
# Migration
# =====================================================


def test_migration_adds_columns_on_v04_database(tmp_path: Path) -> None:
    """Build a V0.4 schema (no is_favorite, no share_token, no tags/photo_tags)
    and verify upgrade_to_v05 brings it up to spec."""
    from sqlalchemy import text
    from sqlmodel import create_engine

    db_path = tmp_path / "v04.db"
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TIMESTAMP NOT NULL,
                    received_at TIMESTAMP NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    file_size INTEGER NOT NULL,
                    hostname TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO photos (captured_at, received_at, file_path, file_size, hostname) "
                "VALUES ('2026-05-04 12:00:00', '2026-05-04 12:00:01', 'a.jpg', 100, 'DietPi')"
            )
        )

    from wildwatch_server.migrations import upgrade_to_v05

    actions = upgrade_to_v05(engine)
    assert actions["columns_added"] >= 2
    assert actions["tables_created"] >= 2

    # Pre-existing row is intact
    with engine.connect() as conn:
        cols = [
            row[1] for row in conn.execute(text("PRAGMA table_info(photos)")).fetchall()
        ]
    assert "is_favorite" in cols
    assert "share_token" in cols

    # Idempotent re-run does nothing
    second = upgrade_to_v05(engine)
    assert second["columns_added"] == 0
    assert second["tables_created"] == 0
