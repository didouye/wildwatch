"""Tests for the state publisher (writes /run/wildwatch/{status.json,preview.jpg})."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from wildwatch_capture.state_publisher import StatePublisher


def test_status_json_atomic_write(tmp_path: Path) -> None:
    pub = StatePublisher(state_dir=tmp_path)
    snap = {
        "service_active": True,
        "current_config": {"rotation": 0, "capture_width": 2304},
        "last_capture_at": None,
        "last_detection_at": None,
        "error_count": 0,
        "uptime_s": 12,
    }
    pub.write_status(snap)
    out = tmp_path / "status.json"
    assert out.exists()
    assert json.loads(out.read_text()) == snap


def test_preview_writes_320x240_jpeg(tmp_path: Path) -> None:
    pub = StatePublisher(state_dir=tmp_path)
    # Simulate a 640x480 grayscale frame coming from camera.read_detection_frame().
    frame = (np.random.rand(480, 640) * 255).astype("uint8")
    pub.write_preview(frame)
    out = tmp_path / "preview.jpg"
    assert out.exists()
    # Soft assert: file is a JPEG (starts with SOI marker).
    assert out.read_bytes()[:2] == b"\xff\xd8"


def test_status_json_no_partial_file_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pub = StatePublisher(state_dir=tmp_path)
    pub.write_status({"a": 1})  # seed
    original = (tmp_path / "status.json").read_text()

    # Force a crash mid-write by patching os.replace.
    import os

    monkeypatch.setattr(
        os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated"))
    )
    with pytest.raises(OSError):
        pub.write_status({"a": 2})

    # The original file should still be intact, no half-written content.
    assert (tmp_path / "status.json").read_text() == original
