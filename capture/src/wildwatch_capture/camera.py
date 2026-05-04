"""picamera2 wrapper.

Always runs in a lightweight preview config (low resolution, small CMA
footprint). When a high-resolution capture is requested, briefly switches to a
still config via `switch_mode_and_capture_request` and returns to preview.

This strategy fits within the ~64 MB of default CMA on a RPi 2 v1.1, where a
dual-stream config (full-res main + lores) exhausts DMA memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from wildwatch_capture.config import CameraConfig

if TYPE_CHECKING:
    from picamera2 import Picamera2  # pragma: no cover


class Camera:
    def __init__(self, config: CameraConfig) -> None:
        self._cfg = config
        self._picam: Picamera2 | None = None
        self._still_config: dict | None = None
        self._last_capture_metadata: dict[str, object] = {}

    def start(self) -> None:
        from libcamera import Transform
        from picamera2 import Picamera2

        self._picam = Picamera2()
        rotation_to_transform = {
            0: Transform(),
            90: Transform(transpose=True, vflip=True),
            180: Transform(hflip=True, vflip=True),
            270: Transform(transpose=True, hflip=True),
        }
        transform = rotation_to_transform.get(self._cfg.rotation, Transform())

        preview_config = self._picam.create_preview_configuration(
            main={"size": (self._cfg.detection_width, self._cfg.detection_height), "format": "YUV420"},
            raw=None,
            transform=transform,
            buffer_count=4,
        )
        self._still_config = self._picam.create_still_configuration(
            main={"size": (self._cfg.capture_width, self._cfg.capture_height), "format": "BGR888"},
            raw=None,
            transform=transform,
            buffer_count=1,
        )

        self._picam.configure(preview_config)
        self._picam.start()

    def stop(self) -> None:
        if self._picam is not None:
            self._picam.stop()
            self._picam.close()
            self._picam = None
            self._still_config = None

    def read_detection_frame(self) -> np.ndarray:
        """Return the latest preview frame as 2D grayscale (uint8).

        YUV420 stores the Y (luminance) plane in the first `height` rows, which
        is already a grayscale image. We slice it directly without any costly
        color conversion.
        """
        if self._picam is None:
            raise RuntimeError("Camera not started")
        yuv = self._picam.capture_array("main")
        h = self._cfg.detection_height
        return yuv[:h, : self._cfg.detection_width]

    def capture_to_file(self, path: Path) -> None:
        """Switch to the still config, capture at high resolution, return to preview.

        Stores picamera2 metadata (exposure, gain, etc.) in
        `self.last_capture_metadata` so the uploader can enrich the JSON sidecar.
        """
        if self._picam is None or self._still_config is None:
            raise RuntimeError("Camera not started")
        request = self._picam.switch_mode_and_capture_request(self._still_config)
        try:
            request.save("main", str(path))
            raw_meta = request.get_metadata() or {}
        finally:
            request.release()

        camera_props = self._picam.camera_properties or {}
        self._last_capture_metadata = {
            "camera": {
                "width": self._cfg.capture_width,
                "height": self._cfg.capture_height,
                "rotation": self._cfg.rotation,
            },
            "sensor": {
                "model": camera_props.get("Model"),
                "exposure_time_us": raw_meta.get("ExposureTime"),
                "analogue_gain": raw_meta.get("AnalogueGain"),
                "lux": raw_meta.get("Lux"),
            },
        }

    @property
    def last_capture_metadata(self) -> dict[str, object]:
        return dict(self._last_capture_metadata)

    def __enter__(self) -> Camera:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
