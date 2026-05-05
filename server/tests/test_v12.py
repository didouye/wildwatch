"""Tests for V1.2: schema migration + heartbeat infrastructure."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import SQLModel

from wildwatch_server import db as db_module
from wildwatch_server.models import Camera


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "admin-key")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    for var in ("ENROLL", "UPLOAD", "LOGIN", "DEFAULT", "HEARTBEAT"):
        monkeypatch.setenv(f"WILDWATCH_RATE_{var}", "1000/minute")
    db_module.reset_engine_cache()
    import wildwatch_server.rate_limit as rl
    importlib.reload(rl)
    rl.limiter.reset()
    import wildwatch_server.routes.cameras as cameras_routes
    import wildwatch_server.routes.photos as photos_routes
    importlib.reload(photos_routes)
    importlib.reload(cameras_routes)
    import wildwatch_server.main as main_module
    importlib.reload(main_module)
    SQLModel.metadata.create_all(db_module.get_engine())
    return main_module.app, db_module.get_engine()


def test_migration_v11_to_v12_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine = _reload_app(tmp_path, monkeypatch)

    # Drop the new columns so we simulate a pre-v12 schema.
    with engine.begin() as conn:
        for col in (
            "desired_config",
            "last_heartbeat",
            "agent_last_seen_at",
            "pending_reorient_delta",
        ):
            try:
                conn.execute(text(f"ALTER TABLE cameras DROP COLUMN {col}"))
            except Exception:
                pass

    from wildwatch_server.migrations import upgrade_to_v12

    result1 = upgrade_to_v12(engine)
    result2 = upgrade_to_v12(engine)

    assert result1["columns_added"] == 4
    assert result2["columns_added"] == 0  # second call is a no-op

    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(cameras)")).fetchall()}
    assert {"desired_config", "last_heartbeat", "agent_last_seen_at", "pending_reorient_delta"} <= cols


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app, _ = _reload_app(tmp_path, monkeypatch)
    return TestClient(app)


def _enroll(client: TestClient, hostname: str = "rpi") -> dict:
    return client.post("/api/cameras/enroll", json={"hostname": hostname}).json()


def _approve(client: TestClient, camera_id: int) -> None:
    res = client.patch(
        f"/api/cameras/{camera_id}",
        headers={"Authorization": "Bearer admin-key"},
        json={"status": "approved"},
    )
    assert res.status_code == 200


def _heartbeat(client: TestClient, token: str, status: dict, preview: bytes | None = None):
    files: dict = {"status": (None, json.dumps(status))}
    if preview is not None:
        files["preview"] = ("preview.jpg", preview, "image/jpeg")
    return client.post(
        "/api/cameras/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )


def test_heartbeat_requires_camera_token(client: TestClient) -> None:
    res = client.post("/api/cameras/agent/heartbeat", files={"status": (None, "{}")})
    assert res.status_code == 401


def test_heartbeat_returns_403_when_pending(client: TestClient) -> None:
    cam = _enroll(client)
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 403


def test_heartbeat_stores_blob_and_timestamp(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 42},
        "system": {"cpu_temp_c": 45.0, "memory_avail_mb": 200, "memory_total_mb": 512,
                    "load_avg_1min": 0.3, "disk_avail_mb": 1024, "queue_size": 0},
        "capture": {"service_active": True, "status_age_s": 1, "preview_age_s": 2,
                     "last_capture_at": None, "last_detection_at": None,
                     "error_count": 0, "apply_error_observed": False},
        "reported_config": {"rotation": 0, "capture_width": 2304},
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200, res.text

    # Sanity: the admin list endpoint still works after the heartbeat write.
    assert client.get(
        "/api/cameras",
        headers={"Authorization": "Bearer admin-key"},
    ).status_code == 200
    # Re-fetch via direct DB session for the new fields (they aren't in CameraRead yet).
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.agent_last_seen_at is not None
        stored = json.loads(row.last_heartbeat)
        assert stored["agent"]["version"] == "1.2.0"
        assert stored["reported_config"]["rotation"] == 0


def test_heartbeat_stores_preview_to_disk(client: TestClient, tmp_path: Path) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}}, preview=fake_jpeg)
    assert res.status_code == 200, res.text

    from wildwatch_server.storage import previews_dir
    p = previews_dir() / f"{cam['id']}.jpg"
    assert p.exists() and p.read_bytes() == fake_jpeg


def test_heartbeat_without_preview_does_not_create_file(client: TestClient, tmp_path: Path) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 200
    from wildwatch_server.storage import previews_dir
    assert not (previews_dir() / f"{cam['id']}.jpg").exists()


def test_get_preview_returns_jpeg(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    blob = b"\xff\xd8\xff\xe0jpegdata"
    _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}}, preview=blob)

    res = client.get(f"/preview/{cam['id']}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == blob


def test_get_preview_returns_404_when_missing(client: TestClient) -> None:
    res = client.get("/preview/9999")
    assert res.status_code == 404


def test_camera_card_renders_status(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 60},
        "system": {"cpu_temp_c": 47.3, "memory_avail_mb": 234, "memory_total_mb": 512,
                    "load_avg_1min": 0.5, "disk_avail_mb": 1024, "queue_size": 0},
        "capture": {"service_active": True, "status_age_s": 1, "preview_age_s": 2,
                     "last_capture_at": None, "last_detection_at": None,
                     "error_count": 0, "apply_error_observed": False},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    body = res.text
    # The fragment carries the running config + agent badge.
    assert "47.3" in body
    assert "Agent" in body
    assert "Capture" in body
    assert "rotation" in body and "0" in body  # current value


def test_heartbeat_rejects_non_dict_status(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Send a list instead of a dict.
    files = {"status": (None, json.dumps([1, 2, 3]))}
    res = client.post(
        "/api/cameras/agent/heartbeat",
        headers={"Authorization": f"Bearer {cam['token']}"},
        files=files,
    )
    assert res.status_code == 400
