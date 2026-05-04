"""Tests for the motion detector."""

from __future__ import annotations

import numpy as np
import pytest

from wildwatch_capture.config import MotionConfig
from wildwatch_capture.motion import MotionDetector


def make_frame(value: int, shape: tuple[int, int] = (60, 80)) -> np.ndarray:
    """Build a uniform grayscale frame."""
    return np.full(shape, value, dtype=np.uint8)


@pytest.fixture
def config() -> MotionConfig:
    return MotionConfig(
        pixel_threshold=25,
        area_threshold=0.02,
        background_alpha=0.05,
        warmup_frames=3,
        cooldown_seconds=5.0,
    )


def test_warmup_never_triggers() -> None:
    cfg = MotionConfig(warmup_frames=10, pixel_threshold=25, area_threshold=0.02)
    detector = MotionDetector(cfg)
    # During the first 10 frames we alternate 0 and 255 (massive changes).
    # None of them should trigger because warmup is in progress.
    triggered_count = sum(
        1
        for i in range(10)
        if detector.process(make_frame(0 if i % 2 == 0 else 255), now=float(i))
    )
    assert triggered_count == 0


def test_no_motion_on_identical_frames(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    # Seed the background with 5 identical frames
    for i in range(5):
        detector.process(make_frame(100), now=float(i))
    # A new identical frame → no motion
    triggered = detector.process(make_frame(100), now=10.0)
    assert triggered is False


def test_motion_detected_on_significant_change(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    # Stable gray background
    for i in range(5):
        detector.process(make_frame(100), now=float(i))
    # Very different frame → motion
    triggered = detector.process(make_frame(200), now=10.0)
    assert triggered is True


def test_small_change_does_not_trigger(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))
    # Change below pixel_threshold (diff of 10 < 25) → ignored
    triggered = detector.process(make_frame(110), now=10.0)
    assert triggered is False


def test_partial_motion_below_area_threshold(config: MotionConfig) -> None:
    """A very localized change (< 2% of the area) does not trigger."""
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # 60x80 = 4800 pixels. 2% = 96 pixels. We change 50 → no trigger.
    frame = make_frame(100)
    frame[0:5, 0:10] = 250  # 50 pixels
    triggered = detector.process(frame, now=10.0)
    assert triggered is False


def test_partial_motion_above_area_threshold(config: MotionConfig) -> None:
    """A change covering more than 2% of the area triggers."""
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # 60x80 = 4800 pixels. We change 10x20 = 200 pixels (4.2%) → trigger.
    frame = make_frame(100)
    frame[0:10, 0:20] = 250
    triggered = detector.process(frame, now=10.0)
    assert triggered is True


def test_cooldown_blocks_consecutive_triggers(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # First trigger
    assert detector.process(make_frame(200), now=10.0) is True

    # During cooldown (5s) a new change does not trigger
    assert detector.process(make_frame(50), now=12.0) is False
    assert detector.process(make_frame(50), now=14.0) is False


def test_cooldown_expires(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    for i in range(10):
        detector.process(make_frame(100), now=float(i))

    assert detector.process(make_frame(200), now=10.0) is True
    # With alpha=0.05 the background has barely moved after a single frame
    # at 200. A new frame at 200 is still far from the background, and the
    # cooldown has elapsed (15s > 10s + 5s), so it must trigger again.
    assert detector.process(make_frame(200), now=15.5) is True


def test_background_adapts_to_lighting(config: MotionConfig) -> None:
    """A gradual lighting change must not trigger repeatedly."""
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # Slow change: +1 per frame for 50 frames → no motion
    triggers = 0
    for i in range(50):
        if detector.process(make_frame(100 + i), now=10.0 + i * 0.1):
            triggers += 1
    assert triggers == 0
