"""WildWatch capture client V0.2 — motion detection + burst capture + upload.

Reads the config at ~/wildwatch/config.toml (or --config), and runs a loop:
  1. Read a low-resolution frame
  2. Feed it to the motion detector (adaptive background subtraction)
  3. On motion: capture a high-resolution burst
  4. Move photos into the local queue, try to upload them to the server
  5. Publish current state to /run/wildwatch/ for the agent to pick up
  6. Resume monitoring

Clean shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import logging
import signal
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from wildwatch_capture import system_info
from wildwatch_capture.camera import Camera
from wildwatch_capture.config import Config, load
from wildwatch_capture.motion import MotionDetector
from wildwatch_capture.state_publisher import StatePublisher
from wildwatch_capture.uploader import Uploader

log = logging.getLogger("wildwatch_capture")

DEFAULT_CONFIG_PATH = Path("~/wildwatch/config.toml").expanduser()

CLEANUP_INTERVAL_SECONDS = 3600.0  # once per hour
PREVIEW_INTERVAL_S = 5.0  # throttle preview JPEG writes


class StopRequested(Exception):
    pass


@dataclass
class LoopState:
    started_at_mono: float = field(default_factory=time.monotonic)
    last_cleanup_at: float = 0.0
    last_capture_at: datetime | None = None
    last_detection_at: datetime | None = None
    error_count: int = 0
    last_preview_at_mono: float = 0.0


def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: object) -> None:
        log.info("Signal %s received, shutting down", signum)
        raise StopRequested

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _capture_burst(
    camera: Camera, uploader: Uploader, config: Config, motion_score: float
) -> int:
    """Capture a burst and enqueue each photo with enriched metadata."""
    captured = 0
    burst_size = config.capture.burst_count
    for i in range(burst_size):
        captured_at = datetime.now(timezone.utc)
        with tempfile.NamedTemporaryFile(prefix="wildwatch_", suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            camera.capture_to_file(tmp_path)
            extra = {
                "motion_score": round(motion_score, 4),
                "frame_index": i + 1,
                "burst_size": burst_size,
                "system": system_info.snapshot(),
                **camera.last_capture_metadata,
            }
            uploader.enqueue(tmp_path, captured_at, extra_metadata=extra)
            captured += 1
        except Exception as exc:
            log.exception("Capture %d/%d failed: %s", i + 1, burst_size, exc)
            tmp_path.unlink(missing_ok=True)
        if i < burst_size - 1:
            time.sleep(config.capture.burst_interval_seconds)
    return captured


def _build_status(state: LoopState, config: Config) -> dict:
    return {
        "service_active": True,
        "uptime_s": int(time.monotonic() - state.started_at_mono),
        "last_capture_at": state.last_capture_at.isoformat() if state.last_capture_at else None,
        "last_detection_at": (
            state.last_detection_at.isoformat() if state.last_detection_at else None
        ),
        "error_count": state.error_count,
        "current_config": {
            "rotation": config.camera.rotation,
            "capture_width": config.camera.capture_width,
            "capture_height": config.camera.capture_height,
            "detection_width": config.camera.detection_width,
            "detection_height": config.camera.detection_height,
            "pixel_threshold": config.motion.pixel_threshold,
            "area_threshold": config.motion.area_threshold,
            "background_alpha": config.motion.background_alpha,
            "warmup_frames": config.motion.warmup_frames,
            "cooldown_seconds": config.motion.cooldown_seconds,
            "burst_count": config.capture.burst_count,
            "burst_interval_seconds": config.capture.burst_interval_seconds,
        },
    }


def _loop_once(
    camera: Camera,
    detector: MotionDetector,
    uploader: Uploader,
    publisher: StatePublisher,
    state: LoopState,
    config: Config,
) -> None:
    """Run a single iteration of the monitoring loop."""
    frame = camera.read_detection_frame()
    triggered = detector.process(frame, now=time.monotonic())
    if triggered:
        state.last_detection_at = datetime.now(timezone.utc)
        score = detector.last_motion_score
        log.info("Motion detected (score=%.3f), capturing burst", score)
        captured = _capture_burst(camera, uploader, config, score)
        if captured:
            state.last_capture_at = datetime.now(timezone.utc)
        log.info("Burst done: %d photo(s) enqueued", captured)
    sent = uploader.flush()
    if sent:
        log.info("%d photo(s) uploaded to the server", sent)

    # State publishing — always status, preview throttled.
    publisher.write_status(_build_status(state, config))
    now_mono = time.monotonic()
    if now_mono - state.last_preview_at_mono > PREVIEW_INTERVAL_S:
        try:
            publisher.write_preview(frame)
            state.last_preview_at_mono = now_mono
        except Exception:
            log.exception("Failed to write preview")

    # Cleanup unchanged.
    if now_mono - state.last_cleanup_at > CLEANUP_INTERVAL_SECONDS:
        deleted = uploader.cleanup_old_sent()
        if deleted:
            log.info("Cleanup: removed %d old photo(s) from sent/", deleted)
        state.last_cleanup_at = now_mono


def run(config: Config) -> None:
    detector = MotionDetector(config.motion)
    uploader = Uploader(config.upload)
    publisher = StatePublisher()
    state = LoopState()

    log.info("Starting camera")
    with Camera(config.camera) as camera:
        log.info("Monitoring loop started")
        while True:
            _loop_once(camera, detector, uploader, publisher, state, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="WildWatch capture V0.2")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load(args.config)
    log.info("Config loaded from %s", args.config if args.config.exists() else "(defaults)")
    log.info("Target server: %s", config.upload.server_url)

    _install_signal_handlers()

    try:
        run(config)
    except StopRequested:
        log.info("Shutdown requested, exiting cleanly")


if __name__ == "__main__":
    main()
