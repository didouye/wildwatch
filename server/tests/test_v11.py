"""Tests for V1.1: camera enrollment, approval, multi-camera upload."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from wildwatch_server import db as db_module


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "admin-key")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    # Loosen the enrollment throttle so a single test can spin up multiple
    # cameras without burning the whole hourly bucket.
    monkeypatch.setenv("WILDWATCH_RATE_ENROLL", "1000/minute")
    monkeypatch.setenv("WILDWATCH_RATE_UPLOAD", "1000/minute")
    monkeypatch.setenv("WILDWATCH_RATE_LOGIN", "1000/minute")
    monkeypatch.setenv("WILDWATCH_RATE_DEFAULT", "1000/minute")
    db_module.reset_engine_cache()
    import wildwatch_server.rate_limit as rl

    importlib.reload(rl)
    rl.limiter.reset()
    # Reload every module that captures rate-limit values at import time so
    # the new limiter and limit strings are picked up.
    import wildwatch_server.routes.auth as auth_routes
    import wildwatch_server.routes.cameras as cameras_routes
    import wildwatch_server.routes.photos as photos_routes

    importlib.reload(photos_routes)
    importlib.reload(auth_routes)
    importlib.reload(cameras_routes)

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


# -------------------- Enrollment --------------------


def test_enroll_creates_pending_camera(client: TestClient) -> None:
    res = client.post(
        "/api/cameras/enroll",
        json={"hostname": "RPi-Test", "system": {"cpu_temp_celsius": 41.0}},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "pending"
    assert body["hostname"] == "RPi-Test"
    assert isinstance(body["token"], str) and len(body["token"]) > 16


def test_enroll_returns_unique_tokens(client: TestClient) -> None:
    a = client.post("/api/cameras/enroll", json={"hostname": "A"}).json()
    b = client.post("/api/cameras/enroll", json={"hostname": "B"}).json()
    assert a["token"] != b["token"]


# -------------------- /me --------------------


def test_get_me_returns_status(client: TestClient) -> None:
    enrolled = client.post("/api/cameras/enroll", json={"hostname": "X"}).json()
    res = client.get(
        "/api/cameras/me",
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "pending"


def test_get_me_requires_known_token(client: TestClient) -> None:
    res = client.get(
        "/api/cameras/me", headers={"Authorization": "Bearer not-a-token"}
    )
    assert res.status_code == 401


# -------------------- Upload auth modes --------------------


def test_upload_with_pending_camera_token_returns_403(client: TestClient) -> None:
    enrolled = client.post("/api/cameras/enroll", json={"hostname": "X"}).json()
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    assert res.status_code == 403
    assert "approval" in res.json()["detail"].lower()


def test_upload_with_approved_camera_token_works(
    client: TestClient, session: Session
) -> None:
    enrolled = client.post("/api/cameras/enroll", json={"hostname": "X"}).json()
    # Approve via admin PATCH
    patch = client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "approved"},
        headers={"Authorization": "Bearer admin-key"},
    )
    assert patch.status_code == 200
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    assert res.status_code == 200


def test_upload_with_revoked_camera_token_returns_403(client: TestClient) -> None:
    enrolled = client.post("/api/cameras/enroll", json={"hostname": "X"}).json()
    client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "revoked"},
        headers={"Authorization": "Bearer admin-key"},
    )
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    assert res.status_code == 403


def test_upload_with_admin_key_still_works(client: TestClient) -> None:
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": "Bearer admin-key"},
    )
    assert res.status_code == 200
    # No camera_id since the admin uploaded
    photo_id = res.json()["id"]
    detail = client.get(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer admin-key"},
    ).json()
    # camera_id is not exposed in PhotoRead -- but hostname stays None
    # because the admin did not set one in the metadata.
    assert detail["hostname"] is None


def test_upload_with_camera_token_links_to_camera(
    client: TestClient, session: Session
) -> None:
    from wildwatch_server.models import Camera, Photo
    from sqlmodel import select

    enrolled = client.post(
        "/api/cameras/enroll", json={"hostname": "FieldRPi"}
    ).json()
    client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "approved"},
        headers={"Authorization": "Bearer admin-key"},
    )
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    photo_id = res.json()["id"]

    photo = session.get(Photo, photo_id)
    assert photo.camera_id == enrolled["id"]
    assert photo.hostname == "FieldRPi"

    cam = session.exec(select(Camera).where(Camera.id == enrolled["id"])).first()
    assert cam.last_seen_at is not None


def test_upload_without_any_token_returns_401(client: TestClient) -> None:
    res = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
    )
    assert res.status_code == 401


# -------------------- Admin: list / patch / delete --------------------


def test_list_cameras_excludes_token(client: TestClient) -> None:
    client.post("/api/cameras/enroll", json={"hostname": "X"})
    res = client.get(
        "/api/cameras", headers={"Authorization": "Bearer admin-key"}
    )
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert "token" not in items[0]
    assert items[0]["status"] == "pending"


def test_list_cameras_with_status_filter(client: TestClient) -> None:
    a = client.post("/api/cameras/enroll", json={"hostname": "A"}).json()
    client.post("/api/cameras/enroll", json={"hostname": "B"})
    client.patch(
        f"/api/cameras/{a['id']}",
        json={"status": "approved"},
        headers={"Authorization": "Bearer admin-key"},
    )
    pending = client.get(
        "/api/cameras?status=pending",
        headers={"Authorization": "Bearer admin-key"},
    ).json()
    approved = client.get(
        "/api/cameras?status=approved",
        headers={"Authorization": "Bearer admin-key"},
    ).json()
    assert len(pending) == 1
    assert len(approved) == 1


def test_patch_approve_sets_approved_at(
    client: TestClient, session: Session
) -> None:
    enrolled = client.post("/api/cameras/enroll", json={"hostname": "X"}).json()
    client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "approved", "display_name": "Backyard"},
        headers={"Authorization": "Bearer admin-key"},
    )
    from wildwatch_server.models import Camera

    cam = session.get(Camera, enrolled["id"])
    assert cam.approved_at is not None
    assert cam.display_name == "Backyard"


def test_revoke_keeps_photos(client: TestClient, session: Session) -> None:
    enrolled = client.post(
        "/api/cameras/enroll", json={"hostname": "X"}
    ).json()
    client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "approved"},
        headers={"Authorization": "Bearer admin-key"},
    )
    client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    # Revoke
    client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "revoked"},
        headers={"Authorization": "Bearer admin-key"},
    )
    # Photo still listed
    listed = client.get(
        "/api/photos", headers={"Authorization": "Bearer admin-key"}
    ).json()
    assert listed["total"] == 1


def test_delete_camera_orphans_photos(
    client: TestClient, session: Session
) -> None:
    from wildwatch_server.models import Photo

    enrolled = client.post(
        "/api/cameras/enroll", json={"hostname": "X"}
    ).json()
    client.patch(
        f"/api/cameras/{enrolled['id']}",
        json={"status": "approved"},
        headers={"Authorization": "Bearer admin-key"},
    )
    upload = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {enrolled['token']}"},
    )
    photo_id = upload.json()["id"]

    res = client.delete(
        f"/api/cameras/{enrolled['id']}",
        headers={"Authorization": "Bearer admin-key"},
    )
    assert res.status_code == 200
    assert res.json()["photos_orphaned"] == 1

    # Photo row still exists, camera_id is now NULL
    p = session.get(Photo, photo_id)
    assert p is not None
    assert p.camera_id is None


# -------------------- camera_id filter --------------------


def test_list_photos_filtered_by_camera_id(client: TestClient) -> None:
    cam_a = client.post("/api/cameras/enroll", json={"hostname": "A"}).json()
    cam_b = client.post("/api/cameras/enroll", json={"hostname": "B"}).json()
    for cam in (cam_a, cam_b):
        client.patch(
            f"/api/cameras/{cam['id']}",
            json={"status": "approved"},
            headers={"Authorization": "Bearer admin-key"},
        )
    client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {cam_a['token']}"},
    )
    client.post(
        "/api/photos",
        files={"file": ("b.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": f"Bearer {cam_b['token']}"},
    )

    listed_a = client.get(
        f"/api/photos?camera_id={cam_a['id']}",
        headers={"Authorization": "Bearer admin-key"},
    ).json()
    assert listed_a["total"] == 1


# -------------------- Migration --------------------


def test_migration_v10_to_v11_adds_cameras_and_camera_id(tmp_path: Path) -> None:
    """Build a V1.0 schema by hand and verify upgrade_to_v11 brings it to V1.1."""
    from sqlalchemy import text
    from sqlmodel import create_engine

    db_path = tmp_path / "v10.db"
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
                    hostname TEXT,
                    is_favorite INTEGER NOT NULL DEFAULT 0,
                    share_token TEXT
                )
                """
            )
        )

    from wildwatch_server.migrations import upgrade_to_v11

    actions = upgrade_to_v11(engine)
    assert actions["tables_created"] == 1
    assert actions["columns_added"] == 1

    with engine.connect() as conn:
        photo_cols = [
            row[1] for row in conn.execute(text("PRAGMA table_info(photos)")).fetchall()
        ]
        assert "camera_id" in photo_cols
        cam_table = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='cameras'")
        ).fetchone()
        assert cam_table is not None

    # Idempotent
    again = upgrade_to_v11(engine)
    assert again["tables_created"] == 0
    assert again["columns_added"] == 0
