"""Tests for the uploader (retry + cleanup logic, no real HTTP)."""

from __future__ import annotations

import json
import os
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
        retry_interval_seconds=0.0,  # disable throttling for the tests
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
    """A 4xx error (auth, invalid payload) is permanent: drop it from the
    queue and store it in dead/ so we do not retry forever."""
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
    """A 5xx error (server down) is transient: keep the photo in the queue."""
    uploader = Uploader(config)
    write_fake_photo(uploader, "later.jpg", datetime.now(timezone.utc))

    response = httpx.Response(503, request=httpx.Request("POST", "http://x/api/photos"))
    with patch("httpx.post", return_value=response):
        sent = uploader.flush(now=100.0)
    assert sent == 0
    assert (uploader.queue_dir / "later.jpg").exists()


def test_flush_network_error_keeps_in_queue(config: UploadConfig) -> None:
    """A connection error (WiFi down) is transient: keep the photo queued."""
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
        # Before retry_interval expires, the second flush is a no-op
        uploader.flush(now=110.0)
        assert mock.call_count == 1
        # After expiry, the third flush would attempt again (queue empty here)
        uploader.flush(now=140.0)
        assert mock.call_count == 1  # no photo left to send, but we did try


def test_flush_empty_meta_moves_to_dead_letter(config: UploadConfig) -> None:
    """A truncated meta.json (e.g. enqueue interrupted by reboot before fsync)
    must not crash the loop -- move the item to dead/ and keep going."""
    uploader = Uploader(config)
    photo = uploader.queue_dir / "corrupt.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake")
    photo.with_suffix(photo.suffix + ".meta.json").write_text("")  # empty, invalid JSON

    sent = uploader.flush(now=100.0)
    assert sent == 0
    assert not (uploader.queue_dir / "corrupt.jpg").exists()
    assert (uploader.dead_dir / "corrupt.jpg").exists()
    assert (uploader.dead_dir / "corrupt.jpg.meta.json").exists()


def test_flush_invalid_json_meta_moves_to_dead_letter(config: UploadConfig) -> None:
    """Half-written meta.json with invalid JSON should also go to dead/."""
    uploader = Uploader(config)
    photo = uploader.queue_dir / "halfwritten.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake")
    photo.with_suffix(photo.suffix + ".meta.json").write_text('{"captured_at": "20')

    sent = uploader.flush(now=100.0)
    assert sent == 0
    assert not (uploader.queue_dir / "halfwritten.jpg").exists()
    assert (uploader.dead_dir / "halfwritten.jpg").exists()


def test_flush_empty_jpg_moves_to_dead_letter(config: UploadConfig) -> None:
    """A 0-byte JPG (write interrupted) is unusable: drop to dead/, don't upload."""
    uploader = Uploader(config)
    photo = uploader.queue_dir / "empty.jpg"
    photo.write_bytes(b"")
    meta = photo.with_suffix(photo.suffix + ".meta.json")
    meta.write_text(json.dumps({"captured_at": "2026-05-06T12:00:00+00:00"}))

    with patch("httpx.post") as mock_post:
        sent = uploader.flush(now=100.0)
    assert sent == 0
    assert mock_post.call_count == 0  # never even attempted
    assert not (uploader.queue_dir / "empty.jpg").exists()
    assert (uploader.dead_dir / "empty.jpg").exists()


def test_flush_continues_after_corrupt_item(config: UploadConfig) -> None:
    """One corrupt item must not block subsequent valid items in the same flush."""
    uploader = Uploader(config)
    # First (alphabetically): corrupt
    bad = uploader.queue_dir / "00bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0fake")
    bad.with_suffix(bad.suffix + ".meta.json").write_text("")
    # Second: valid
    write_fake_photo(uploader, "01good.jpg", datetime.now(timezone.utc))

    response = httpx.Response(
        200, json={"stored_path": "ok"}, request=httpx.Request("POST", "http://x/api/photos")
    )
    with patch("httpx.post", return_value=response):
        sent = uploader.flush(now=100.0)
    assert sent == 1
    assert (uploader.dead_dir / "00bad.jpg").exists()
    assert (uploader.sent_dir / "01good.jpg").exists()


def test_enqueue_fsyncs_jpg_meta_and_dir(
    config: UploadConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enqueue() must fsync the JPG, the meta, and the queue dir so an abrupt
    reboot cannot leave 0-byte files behind."""
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    src = tmp_path / "tmp_capture.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0fakeJPEG")
    uploader = Uploader(config)
    target = uploader.enqueue(src, datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc))

    # JPG, meta, and parent dir each need at least one fsync.
    assert len(fsync_calls) >= 3, f"expected >=3 fsync calls, got {len(fsync_calls)}"
    assert target.exists()
    assert target.with_suffix(target.suffix + ".meta.json").exists()


def test_cleanup_old_sent(config: UploadConfig) -> None:
    uploader = Uploader(config)
    old = uploader.sent_dir / "old.jpg"
    old.write_bytes(b"x")
    old.with_suffix(old.suffix + ".meta.json").write_bytes(b"{}")
    recent = uploader.sent_dir / "recent.jpg"
    recent.write_bytes(b"x")

    # Backdate "old" by 10 days
    cutoff_seconds = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    import os
    os.utime(old, (cutoff_seconds, cutoff_seconds))
    os.utime(old.with_suffix(old.suffix + ".meta.json"), (cutoff_seconds, cutoff_seconds))

    deleted = uploader.cleanup_old_sent()
    assert deleted == 1
    assert not old.exists()
    assert recent.exists()
