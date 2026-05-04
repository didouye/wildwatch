"""Tests for the upload API (auth + file persistence + metadata sidecar)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a fresh app with a temporary PHOTOS_DIR and auth enabled."""
    monkeypatch.setenv("WILDWATCH_API_KEY", "secret-key-123")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    # Force reloading the module so it picks up the new env vars
    import importlib

    import wildwatch_server.main as main_module
    importlib.reload(main_module)
    return TestClient(main_module.app)


@pytest.fixture
def client_no_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Dev mode without auth (env var unset)."""
    monkeypatch.delenv("WILDWATCH_API_KEY", raising=False)
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    import importlib

    import wildwatch_server.main as main_module
    importlib.reload(main_module)
    return TestClient(main_module.app)


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


def test_upload_with_wrong_key_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_upload_with_correct_key(client: TestClient) -> None:
    response = client.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["size_bytes"] == "8"
    assert body["stored_path"].endswith(".jpg")


def test_upload_persists_metadata_sidecar(client: TestClient, tmp_path: Path) -> None:
    metadata = {"motion_score": 0.14, "frame_index": 1, "burst_size": 3}
    response = client.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        data={"metadata": json.dumps(metadata)},
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert response.status_code == 200
    stored_path = Path(response.json()["stored_path"])
    photo_full = tmp_path / "photos" / stored_path.relative_to("data/photos")
    meta_full = photo_full.with_suffix(photo_full.suffix + ".meta.json")
    assert photo_full.exists()
    assert meta_full.exists()
    saved = json.loads(meta_full.read_text())
    assert saved["motion_score"] == 0.14
    assert saved["frame_index"] == 1


def test_upload_no_auth_when_api_key_unset(client_no_auth: TestClient) -> None:
    """Dev mode: no env var set → every request goes through."""
    response = client_no_auth.post(
        "/api/photos",
        files={"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
    )
    assert response.status_code == 200


def test_upload_rejects_invalid_content_type(client_no_auth: TestClient) -> None:
    response = client_no_auth.post(
        "/api/photos",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415
