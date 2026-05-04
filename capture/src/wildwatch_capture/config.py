from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CameraConfig:
    capture_width: int = 4608
    capture_height: int = 2592
    detection_width: int = 640
    detection_height: int = 480
    rotation: int = 0  # 0, 90, 180, 270


@dataclass(frozen=True)
class MotionConfig:
    pixel_threshold: int = 25  # per-pixel diff (0-255) above which a pixel counts as "changed"
    area_threshold: float = 0.02  # fraction of changed pixels required to trigger (2% default)
    background_alpha: float = 0.05  # background adaptation speed (0-1)
    warmup_frames: int = 30  # frames ignored at startup so the background can stabilize
    cooldown_seconds: float = 5.0  # minimum delay between two triggers


@dataclass(frozen=True)
class CaptureConfig:
    burst_count: int = 3  # number of photos per trigger
    burst_interval_seconds: float = 0.5  # delay between photos in a burst


@dataclass(frozen=True)
class UploadConfig:
    server_url: str = "http://localhost:8000"
    api_key: str = ""  # empty = auth disabled (V0.2 dev)
    queue_dir: str = "~/wildwatch/queue"
    sent_dir: str = "~/wildwatch/sent"
    retry_interval_seconds: float = 30.0
    sent_retention_days: int = 7
    request_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)


def load(path: Path) -> Config:
    """Load a TOML file and return a validated Config.

    If the file does not exist, returns the default configuration.
    Fields missing from the TOML fall back to their defaults.
    """
    if not path.exists():
        return Config()

    with path.open("rb") as fp:
        raw = tomllib.load(fp)

    return Config(
        camera=CameraConfig(**raw.get("camera", {})),
        motion=MotionConfig(**raw.get("motion", {})),
        capture=CaptureConfig(**raw.get("capture", {})),
        upload=UploadConfig(**raw.get("upload", {})),
    )
