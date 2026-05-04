"""Wrapper picamera2.

Tourne en permanence en config preview légère (basse résolution, faible empreinte
CMA). Quand une capture haute résolution est demandée, bascule temporairement vers
une config still avec `switch_mode_and_capture_file` puis revient à la preview.

Cette stratégie permet de tenir dans les ~64 Mo de CMA par défaut sur le RPi 2 v1.1
là où une config dual-stream haute résolution + lores en simultané dépassait la
mémoire DMA.
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
        """Retourne la dernière frame de la preview en niveaux de gris (uint8 2D).

        Le format YUV420 stocke la luminance Y dans les premiers `height` lignes,
        ce qui équivaut à une image en niveaux de gris. On l'extrait directement
        sans conversion coûteuse.
        """
        if self._picam is None:
            raise RuntimeError("Camera not started")
        yuv = self._picam.capture_array("main")
        h = self._cfg.detection_height
        return yuv[:h, : self._cfg.detection_width]

    def capture_to_file(self, path: Path) -> None:
        """Bascule en config still, capture en haute résolution, revient à la preview."""
        if self._picam is None or self._still_config is None:
            raise RuntimeError("Camera not started")
        self._picam.switch_mode_and_capture_file(self._still_config, str(path))

    def __enter__(self) -> Camera:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
