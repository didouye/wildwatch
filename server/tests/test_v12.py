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
