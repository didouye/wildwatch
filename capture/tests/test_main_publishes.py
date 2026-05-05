"""Tests for main._loop_once: ensures status/preview are published each cycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from wildwatch_capture import main as main_module
from wildwatch_capture.config import Config
from wildwatch_capture.main import LoopState, _loop_once


def _make_mocks(tmp_path: Path):
    publisher = MagicMock()
    publisher._dir = tmp_path
    camera = MagicMock()
    camera.read_detection_frame.return_value = np.zeros((480, 640), dtype="uint8")
    detector = MagicMock()
    detector.process.return_value = False  # no motion
    uploader = MagicMock()
    uploader.flush.return_value = 0
    uploader.cleanup_old_sent.return_value = 0
    return camera, detector, uploader, publisher


def test_loop_once_publishes_status_and_preview(tmp_path: Path) -> None:
    config = Config()
    camera, detector, uploader, publisher = _make_mocks(tmp_path)
    state = main_module.LoopState()

    _loop_once(camera, detector, uploader, publisher, state, config)

    publisher.write_status.assert_called_once()
    publisher.write_preview.assert_called_once()


def test_loop_once_throttles_preview(tmp_path: Path) -> None:
    config = Config()
    camera, detector, uploader, publisher = _make_mocks(tmp_path)
    state = LoopState()

    _loop_once(camera, detector, uploader, publisher, state, config)
    _loop_once(camera, detector, uploader, publisher, state, config)
    _loop_once(camera, detector, uploader, publisher, state, config)

    # status written each iteration, but preview only once (throttled).
    assert publisher.write_status.call_count == 3
    assert publisher.write_preview.call_count == 1
