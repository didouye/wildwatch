from __future__ import annotations

import numpy as np

from wildwatch_capture.config import MotionConfig


class MotionDetector:
    """Adaptive background-subtraction motion detector.

    Keeps a "background" frame computed as a weighted moving average of recent
    frames. A frame triggers if enough pixels differ significantly from the
    background and the cooldown since the last trigger has elapsed.
    """

    def __init__(self, config: MotionConfig) -> None:
        self._cfg = config
        self._background: np.ndarray | None = None
        self._frames_seen = 0
        self._last_trigger_at: float | None = None
        self.last_motion_score: float = 0.0

    @property
    def _warm(self) -> bool:
        return self._frames_seen > self._cfg.warmup_frames

    def process(self, frame: np.ndarray, now: float) -> bool:
        """Feed a new frame (uint8, 2D grayscale) and return True if motion was
        just detected.

        Always updates the background, even when motion is detected, so it
        adapts to gradual light changes and a static subject does not stay
        "detected" forever.
        """
        if frame.dtype != np.uint8:
            raise ValueError(f"frame must be uint8, got {frame.dtype}")
        if frame.ndim != 2:
            raise ValueError(f"frame must be 2D grayscale, got shape {frame.shape}")

        frame_f = frame.astype(np.float32)

        if self._background is None:
            self._background = frame_f.copy()
            self._frames_seen = 1
            return False

        diff = np.abs(frame_f - self._background)
        changed_pixels = int(np.sum(diff > self._cfg.pixel_threshold))
        total_pixels = frame.size
        score = changed_pixels / total_pixels
        self.last_motion_score = score

        alpha = self._cfg.background_alpha
        self._background = self._background * (1.0 - alpha) + frame_f * alpha
        self._frames_seen += 1

        if not self._warm:
            return False

        if score < self._cfg.area_threshold:
            return False

        if self._last_trigger_at is not None:
            since_last = now - self._last_trigger_at
            if since_last < self._cfg.cooldown_seconds:
                return False

        self._last_trigger_at = now
        return True
