"""Tests for the WildWatch server REST API (V0.4a)."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from wildwatch_server import db as db_module


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_auth: bool):
    if with_auth:
        monkeypatch.setenv("WILDWATCH_API_KEY", "secret-key-123")
    else:
        monkeypatch.delenv("WILDWATCH_API_KEY", raising=False)
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    db_module.reset_engine_cache()
    # Wipe module-level state from previous tests (engine, app already mounted, ...)
    import wildwatch_server.main as main_module

    importlib.reload(main_module)
    SQLModel.metadata.create_all(db_module.get_engine())
    return main_module.app, db_module.get_engine()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app, _ = _reload_app(tmp_path, monkeypatch, with_auth=True)
    return TestClient(app)


@pytest.fixture
def client_no_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app, _ = _reload_app(tmp_path, monkeypatch, with_auth=False)
    return TestClient(app)


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    """Direct DB session fixture for seeding rows in tests."""
    _, engine = _reload_app(tmp_path, monkeypatch, with_auth=True)
    with Session(engine) as s:
        yield s


# ---------- Health and auth ----------


def test_health_no_auth_required(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_without_auth_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
    )
    assert response.status_code == 401


def test_upload_with_correct_key(client: TestClient) -> None:
    response = client.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert response.status_code == 200
    assert "id" in response.json()


# ---------- Upload ----------


def test_upload_inserts_into_db(client: TestClient, tmp_path: Path) -> None:
    metadata = {
        "motion_score": 0.14,
        "frame_index": 1,
        "burst_size": 3,
        "system": {"hostname": "DietPi", "cpu_temp_celsius": 42.1},
        "camera": {"width": 2304, "height": 1296, "rotation": 0},
        "sensor": {"model": "imx708_noir", "exposure_time_us": 12000},
    }
    response = client.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        data={
            "captured_at": "2026-05-04T12:00:00+00:00",
            "metadata": json.dumps(metadata),
        },
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert response.status_code == 200
    body = response.json()
    photo_id = body["id"]
    assert isinstance(photo_id, int)

    detail = client.get(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert detail.status_code == 200
    saved = detail.json()
    assert saved["motion_score"] == 0.14
    assert saved["hostname"] == "DietPi"
    assert saved["cpu_temp"] == 42.1
    assert saved["camera_width"] == 2304
    assert saved["sensor_model"] == "imx708_noir"


def test_upload_rejects_invalid_content_type(client_no_auth: TestClient) -> None:
    response = client_no_auth.post(
        "/api/photos",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415


# ---------- List, filters, pagination ----------


def _seed_photos(session: Session, count: int = 5, hostname: str = "DietPi") -> None:
    from wildwatch_server.models import Photo

    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(count):
        session.add(
            Photo(
                captured_at=base + timedelta(days=i),
                file_path=f"2026/05/0{(i % 9) + 1}/photo_{i}.jpg",
                file_size=100 + i,
                hostname=hostname,
                motion_score=0.1 + 0.01 * i,
            )
        )
    session.commit()


def test_list_returns_paginated_results(client: TestClient, session: Session) -> None:
    _seed_photos(session, count=12)
    res = client.get(
        "/api/photos?limit=5&offset=0",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 12
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert len(body["items"]) == 5


def test_list_default_order_desc(client: TestClient, session: Session) -> None:
    _seed_photos(session, count=3)
    res = client.get(
        "/api/photos",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    items = res.json()["items"]
    captured = [item["captured_at"] for item in items]
    assert captured == sorted(captured, reverse=True)


def test_list_filter_by_date_range(client: TestClient, session: Session) -> None:
    _seed_photos(session, count=10)
    res = client.get(
        "/api/photos?from=2026-05-03&to=2026-05-05",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    body = res.json()
    # Days 2026-05-03, 2026-05-04, 2026-05-05 → 3 photos
    assert body["total"] == 3


def test_list_filter_by_hostname(client: TestClient, session: Session) -> None:
    from wildwatch_server.models import Photo

    _seed_photos(session, count=3, hostname="DietPi")
    session.add(
        Photo(
            captured_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            file_path="other/x.jpg",
            file_size=10,
            hostname="OtherPi",
        )
    )
    session.commit()

    res = client.get(
        "/api/photos?hostname=OtherPi",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.json()["total"] == 1


def test_list_invalid_date_returns_400(client: TestClient) -> None:
    res = client.get(
        "/api/photos?from=not-a-date",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 400


# ---------- Detail / download / delete ----------


def test_get_photo_404(client: TestClient) -> None:
    res = client.get(
        "/api/photos/999",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 404


def test_download_photo(client: TestClient) -> None:
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0image-bytes", "image/jpeg")},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    photo_id = res.json()["id"]
    download = client.get(
        f"/api/photos/{photo_id}/file",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert download.status_code == 200
    assert download.content == b"\xff\xd8\xff\xe0image-bytes"


def test_delete_removes_db_row_and_file(client: TestClient, tmp_path: Path) -> None:
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    photo_id = res.json()["id"]
    detail = client.get(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    full_path = tmp_path / "photos" / detail["file_path"]

    assert full_path.exists()

    delete = client.delete(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert delete.status_code == 200
    assert not full_path.exists()

    after = client.get(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert after.status_code == 404


# ---------- Stats ----------


def test_stats_groups_by_day_and_hostname(client: TestClient, session: Session) -> None:
    from wildwatch_server.models import Photo

    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    session.add(Photo(captured_at=base, file_path="a.jpg", file_size=1, hostname="DietPi"))
    session.add(
        Photo(captured_at=base + timedelta(hours=2), file_path="b.jpg", file_size=2, hostname="DietPi")
    )
    session.add(
        Photo(captured_at=base + timedelta(days=1), file_path="c.jpg", file_size=3, hostname="OtherPi")
    )
    session.commit()

    res = client.get(
        "/api/stats", headers={"Authorization": "Bearer secret-key-123"}
    )
    body = res.json()
    assert body["total"] == 3
    assert body["by_day"]["2026-05-01"] == 2
    assert body["by_day"]["2026-05-02"] == 1
    assert body["by_hostname"]["DietPi"] == 2
    assert body["by_hostname"]["OtherPi"] == 1


# ---------- Reindex ----------


def test_reindex_ingests_existing_sidecars(
    client: TestClient, tmp_path: Path
) -> None:
    photos_dir = tmp_path / "photos"
    day_dir = photos_dir / "2026/05/04"
    day_dir.mkdir(parents=True)

    photo = day_dir / "20260504T120000.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data")

    sidecar = photo.with_suffix(photo.suffix + ".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "captured_at": "2026-05-04T12:00:00+00:00",
                "motion_score": 0.42,
                "system": {"hostname": "DietPi"},
            }
        )
    )

    res = client.post(
        "/api/admin/reindex",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["scanned"] == 1
    assert body["inserted"] == 1
    assert body["skipped"] == 0

    listed = client.get(
        "/api/photos",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    assert listed["total"] == 1
    assert listed["items"][0]["motion_score"] == 0.42


def test_reindex_is_idempotent(client: TestClient, tmp_path: Path) -> None:
    day_dir = tmp_path / "photos" / "2026/05/04"
    day_dir.mkdir(parents=True)
    (day_dir / "x.jpg").write_bytes(b"\xff\xd8\xff\xe0a")

    first = client.post(
        "/api/admin/reindex",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()
    second = client.post(
        "/api/admin/reindex",
        headers={"Authorization": "Bearer secret-key-123"},
    ).json()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["skipped"] == 1


def test_reindex_without_sidecar_uses_mtime(
    client: TestClient, tmp_path: Path
) -> None:
    day_dir = tmp_path / "photos" / "2026/05/04"
    day_dir.mkdir(parents=True)
    photo = day_dir / "no_meta.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake")

    res = client.post(
        "/api/admin/reindex",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert res.status_code == 200
    assert res.json()["inserted"] == 1
