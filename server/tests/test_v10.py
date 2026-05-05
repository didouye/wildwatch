"""Tests for V1.0: web auth (login form + session cookie) and rate limiting."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel

from wildwatch_server import db as db_module
from wildwatch_server.auth_web import hash_password


def _reload_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    web_user: str | None = "admin",
    web_password: str | None = "secret-pw-1",
    rate_login: str | None = None,
    rate_upload: str | None = None,
):
    monkeypatch.setenv("WILDWATCH_API_KEY", "secret-key-123")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")

    if web_user and web_password:
        monkeypatch.setenv("WILDWATCH_WEB_USER", web_user)
        monkeypatch.setenv("WILDWATCH_WEB_PASSWORD_HASH", hash_password(web_password))
        monkeypatch.setenv("WILDWATCH_SESSION_SECRET", "test-secret-32-chars-aaaaaaaaaaaa")
    else:
        monkeypatch.delenv("WILDWATCH_WEB_USER", raising=False)
        monkeypatch.delenv("WILDWATCH_WEB_PASSWORD_HASH", raising=False)
        monkeypatch.delenv("WILDWATCH_SESSION_SECRET", raising=False)

    if rate_login:
        monkeypatch.setenv("WILDWATCH_RATE_LOGIN", rate_login)
    if rate_upload:
        monkeypatch.setenv("WILDWATCH_RATE_UPLOAD", rate_upload)

    db_module.reset_engine_cache()
    import wildwatch_server.rate_limit as rl

    importlib.reload(rl)
    rl.limiter.reset()
    import wildwatch_server.routes.auth as auth_routes
    import wildwatch_server.routes.photos as photos_routes

    importlib.reload(auth_routes)
    importlib.reload(photos_routes)

    import wildwatch_server.main as main_module

    importlib.reload(main_module)
    SQLModel.metadata.create_all(db_module.get_engine())
    return main_module.app


@pytest.fixture
def client_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _reload_app(tmp_path, monkeypatch)
    return TestClient(app)


@pytest.fixture
def client_no_web_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _reload_app(tmp_path, monkeypatch, web_user=None, web_password=None)
    return TestClient(app)


# ---------- Web auth ----------


def test_login_form_renders(client_auth: TestClient) -> None:
    res = client_auth.get("/login")
    assert res.status_code == 200
    assert "WildWatch" in res.text
    assert "Sign in" in res.text


def test_protected_route_redirects_when_no_cookie(client_auth: TestClient) -> None:
    res = client_auth.get("/gallery", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["location"]


def test_login_with_wrong_password_returns_401(client_auth: TestClient) -> None:
    res = client_auth.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert res.status_code == 401
    assert "Invalid" in res.text


def test_login_with_valid_credentials_sets_cookie_and_redirects(
    client_auth: TestClient,
) -> None:
    res = client_auth.post(
        "/login",
        data={"username": "admin", "password": "secret-pw-1"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["location"] == "/gallery"
    assert "wildwatch_session=" in res.headers.get("set-cookie", "")


def test_authenticated_request_can_access_gallery(client_auth: TestClient) -> None:
    client_auth.post(
        "/login",
        data={"username": "admin", "password": "secret-pw-1"},
        follow_redirects=False,
    )
    # cookie kept across requests by TestClient
    res = client_auth.get("/gallery")
    assert res.status_code == 200


def test_logout_clears_cookie_and_redirects(client_auth: TestClient) -> None:
    client_auth.post(
        "/login",
        data={"username": "admin", "password": "secret-pw-1"},
        follow_redirects=False,
    )
    res = client_auth.post("/logout", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["location"]
    # Subsequent gallery hit goes back to /login
    res2 = client_auth.get("/gallery", follow_redirects=False)
    assert res2.status_code == 302


def test_share_view_remains_public(client_auth: TestClient, tmp_path: Path) -> None:
    # Seed a photo + share token directly through DB to avoid login dance.
    from datetime import datetime, timezone

    from PIL import Image
    from sqlmodel import Session

    from wildwatch_server.models import Photo

    src = tmp_path / "photos" / "share-me.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 200), color=(80, 200, 120)).save(src, "JPEG")

    with Session(db_module.get_engine()) as session:
        photo = Photo(
            captured_at=datetime(2026, 5, 4, 12, tzinfo=timezone.utc),
            file_path="share-me.jpg",
            file_size=src.stat().st_size,
            share_token="public-token-xyz",
        )
        session.add(photo)
        session.commit()

    res = client_auth.get("/share/public-token-xyz", follow_redirects=False)
    assert res.status_code == 200, "Public share view must skip auth"


def test_no_web_auth_when_password_unset(client_no_web_auth: TestClient) -> None:
    """Backward compatibility with V0.5: gallery is open if no password is set."""
    res = client_no_web_auth.get("/gallery", follow_redirects=False)
    assert res.status_code == 200


# ---------- Rate limiting ----------


def test_login_rate_limited_after_3_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configure a tighter login limit and prove it kicks in."""
    app = _reload_app(tmp_path, monkeypatch, rate_login="3/minute")
    client = TestClient(app)
    for _ in range(3):
        res = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert res.status_code == 401
    blocked = client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429


def test_upload_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a tight upload limit and verify the 4th request gets 429."""
    app = _reload_app(tmp_path, monkeypatch, rate_upload="3/minute")
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-key-123"}
    for _ in range(3):
        res = client.post(
            "/api/photos",
            files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
            headers=headers,
        )
        assert res.status_code == 200
    blocked = client.post(
        "/api/photos",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        headers=headers,
    )
    assert blocked.status_code == 429
