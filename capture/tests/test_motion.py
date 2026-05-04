"""Tests pour le détecteur de mouvement."""

from __future__ import annotations

import numpy as np
import pytest

from birdy_capture.config import MotionConfig
from birdy_capture.motion import MotionDetector


def make_frame(value: int, shape: tuple[int, int] = (60, 80)) -> np.ndarray:
    """Crée une frame en niveaux de gris uniforme."""
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
    # Pendant les 10 premières frames, on alterne 0 et 255 (changements massifs).
    # Aucune ne doit déclencher car on est en warmup.
    triggered_count = sum(
        1
        for i in range(10)
        if detector.process(make_frame(0 if i % 2 == 0 else 255), now=float(i))
    )
    assert triggered_count == 0


def test_no_motion_on_identical_frames(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    # Établir le background avec 5 frames identiques
    for i in range(5):
        detector.process(make_frame(100), now=float(i))
    # Une nouvelle frame identique → pas de mouvement
    triggered = detector.process(make_frame(100), now=10.0)
    assert triggered is False


def test_motion_detected_on_significant_change(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    # Background gris stable
    for i in range(5):
        detector.process(make_frame(100), now=float(i))
    # Frame très différente → mouvement
    triggered = detector.process(make_frame(200), now=10.0)
    assert triggered is True


def test_small_change_does_not_trigger(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))
    # Changement sous le pixel_threshold (diff de 10 < 25) → ignoré
    triggered = detector.process(make_frame(110), now=10.0)
    assert triggered is False


def test_partial_motion_below_area_threshold(config: MotionConfig) -> None:
    """Un changement très localisé (< 2% de la surface) ne déclenche pas."""
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # 60x80 = 4800 pixels. 2% = 96 pixels. On en change 50 → pas de déclenchement.
    frame = make_frame(100)
    frame[0:5, 0:10] = 250  # 50 pixels
    triggered = detector.process(frame, now=10.0)
    assert triggered is False


def test_partial_motion_above_area_threshold(config: MotionConfig) -> None:
    """Un changement sur >2% de la surface déclenche."""
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # 60x80 = 4800 pixels. On change 10x20 = 200 pixels (4.2%) → déclenche.
    frame = make_frame(100)
    frame[0:10, 0:20] = 250
    triggered = detector.process(frame, now=10.0)
    assert triggered is True


def test_cooldown_blocks_consecutive_triggers(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # Premier déclenchement
    assert detector.process(make_frame(200), now=10.0) is True

    # Pendant le cooldown (5s), un nouveau changement ne déclenche pas
    assert detector.process(make_frame(50), now=12.0) is False
    assert detector.process(make_frame(50), now=14.0) is False


def test_cooldown_expires(config: MotionConfig) -> None:
    detector = MotionDetector(config)
    for i in range(10):
        detector.process(make_frame(100), now=float(i))

    assert detector.process(make_frame(200), now=10.0) is True
    # Avec alpha=0.05, le background n'a presque pas évolué après une seule
    # frame à 200. Une nouvelle frame à 200 reste très différente du background
    # et, le cooldown étant écoulé (15s > 10s + 5s), doit redéclencher.
    assert detector.process(make_frame(200), now=15.5) is True


def test_background_adapts_to_lighting(config: MotionConfig) -> None:
    """Un changement progressif de luminosité ne doit pas déclencher en boucle."""
    detector = MotionDetector(config)
    for i in range(5):
        detector.process(make_frame(100), now=float(i))

    # Changement lent : +1 par frame pendant 50 frames → pas de motion
    triggers = 0
    for i in range(50):
        if detector.process(make_frame(100 + i), now=10.0 + i * 0.1):
            triggers += 1
    assert triggers == 0
