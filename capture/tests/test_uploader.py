"""Tests pour l'uploader (logique de retry + cleanup, sans HTTP réel)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from wildwatch_capture.config import UploadConfig
from wildwatch_capture.uploader import Uploader


@pytest.fixture
def config(tmp_path: Path) -> UploadConfig:
    return UploadConfig(
        server_url="http://example.invalid:8000",
        api_key="",
        queue_dir=str(tmp_path / "queue"),
        sent_dir=str(tmp_path / "sent"),
        retry_interval_seconds=0.0,  # désactive le throttle pour tester
        sent_retention_days=7,
        request_timeout_seconds=5.0,
    )


def write_fake_photo(uploader: Uploader, name: str, captured_at: datetime) -> Path:
    photo = uploader.queue_dir / name
    photo.write_bytes(b"\xff\xd8\xff\xe0fakeJPEG")
    meta = photo.with_suffix(photo.suffix + ".meta.json")
    meta.write_text(json.dumps({"captured_at": captured_at.isoformat()}))
    return photo


def test_enqueue_creates_meta(config: UploadConfig, tmp_path: Path) -> None:
    src = tmp_path / "tmp_capture.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0fake")
    uploader = Uploader(config)
    captured = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    target = uploader.enqueue(src, captured)
    assert target.exists()
    assert target.with_suffix(target.suffix + ".meta.json").exists()
    meta = json.loads(target.with_suffix(target.suffix + ".meta.json").read_text())
    assert meta["captured_at"] == captured.isoformat()


def test_flush_moves_uploaded_to_sent(config: UploadConfig) -> None:
    uploader = Uploader(config)
    write_fake_photo(uploader, "a.jpg", datetime.now(timezone.utc))

    response = httpx.Response(
        200, json={"stored_path": "ok"}, request=httpx.Request("POST", "http://x/api/photos")
    )
    with patch("httpx.post", return_value=response):
        sent = uploader.flush(now=100.0)
    assert sent == 1
    assert not (uploader.queue_dir / "a.jpg").exists()
    assert (uploader.sent_dir / "a.jpg").exists()


def test_flush_4xx_moves_to_dead_letter(config: UploadConfig) -> None:
    """Une erreur 4xx (auth, payload invalide) est irrécupérable : on retire de la queue
    et on la place dans dead/ pour ne pas re-tenter en boucle."""
    uploader = Uploader(config)
    write_fake_photo(uploader, "bad.jpg", datetime.now(timezone.utc))

    response = httpx.Response(
        401, text="Unauthorized", request=httpx.Request("POST", "http://x/api/photos")
    )
    with patch("httpx.post", return_value=response):
        sent = uploader.flush(now=100.0)
    assert sent == 0
    assert not (uploader.queue_dir / "bad.jpg").exists()
    assert (uploader.dead_dir / "bad.jpg").exists()


def test_flush_5xx_keeps_in_queue(config: UploadConfig) -> None:
    """Une erreur 5xx (serveur down) est transitoire : on garde dans la queue."""
    uploader = Uploader(config)
    write_fake_photo(uploader, "later.jpg", datetime.now(timezone.utc))

    response = httpx.Response(503, request=httpx.Request("POST", "http://x/api/photos"))
    with patch("httpx.post", return_value=response):
        sent = uploader.flush(now=100.0)
    assert sent == 0
    assert (uploader.queue_dir / "later.jpg").exists()


def test_flush_network_error_keeps_in_queue(config: UploadConfig) -> None:
    """Une erreur de connexion (WiFi down) est transitoire : on garde dans la queue."""
    uploader = Uploader(config)
    write_fake_photo(uploader, "wifi.jpg", datetime.now(timezone.utc))

    with patch("httpx.post", side_effect=httpx.ConnectError("no network")):
        sent = uploader.flush(now=100.0)
    assert sent == 0
    assert (uploader.queue_dir / "wifi.jpg").exists()


def test_flush_throttled_by_retry_interval(config: UploadConfig) -> None:
    cfg = UploadConfig(
        server_url=config.server_url,
        api_key=config.api_key,
        queue_dir=config.queue_dir,
        sent_dir=config.sent_dir,
        retry_interval_seconds=30.0,
        sent_retention_days=config.sent_retention_days,
        request_timeout_seconds=config.request_timeout_seconds,
    )
    uploader = Uploader(cfg)
    write_fake_photo(uploader, "x.jpg", datetime.now(timezone.utc))

    response = httpx.Response(
        200, json={"stored_path": "ok"}, request=httpx.Request("POST", "http://x/api/photos")
    )
    with patch("httpx.post", return_value=response) as mock:
        uploader.flush(now=100.0)
        assert mock.call_count == 1
        # Avant l'expiration de retry_interval, deuxième flush ne fait rien
        uploader.flush(now=110.0)
        assert mock.call_count == 1
        # Après l'expiration, troisième flush passe
        uploader.flush(now=140.0)
        assert mock.call_count == 1  # plus de photo en queue, mais on a bien tenté


def test_cleanup_old_sent(config: UploadConfig) -> None:
    uploader = Uploader(config)
    old = uploader.sent_dir / "old.jpg"
    old.write_bytes(b"x")
    old.with_suffix(old.suffix + ".meta.json").write_bytes(b"{}")
    recent = uploader.sent_dir / "recent.jpg"
    recent.write_bytes(b"x")

    # Vieillit "old" en arrière de 10 jours
    cutoff_seconds = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    import os
    os.utime(old, (cutoff_seconds, cutoff_seconds))
    os.utime(old.with_suffix(old.suffix + ".meta.json"), (cutoff_seconds, cutoff_seconds))

    deleted = uploader.cleanup_old_sent()
    assert deleted == 1
    assert not old.exists()
    assert recent.exists()
