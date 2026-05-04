"""WildWatch capture client V0.2 — motion detection + burst capture + upload.

Reads the config at ~/wildwatch/config.toml (or --config), and runs a loop:
  1. Read a low-resolution frame
  2. Feed it to the motion detector (adaptive background subtraction)
  3. On motion: capture a high-resolution burst
  4. Move photos into the local queue, try to upload them to the server
  5. Resume monitoring

Clean shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import logging
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from wildwatch_capture import system_info
from wildwatch_capture.camera import Camera
from wildwatch_capture.config import Config, load
from wildwatch_capture.motion import MotionDetector
from wildwatch_capture.uploader import Uploader

log = logging.getLogger("wildwatch_capture")

DEFAULT_CONFIG_PATH = Path("~/wildwatch/config.toml").expanduser()


class StopRequested(Exception):
    pass


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


CLEANUP_INTERVAL_SECONDS = 3600.0  # once per hour


def run(config: Config) -> None:
    detector = MotionDetector(config.motion)
    uploader = Uploader(config.upload)
    last_cleanup_at = 0.0

    log.info("Starting camera")
    with Camera(config.camera) as camera:
        log.info("Monitoring loop started")
        while True:
            frame = camera.read_detection_frame()
            triggered = detector.process(frame, now=time.monotonic())
            if triggered:
                score = detector.last_motion_score
                log.info("Motion detected (score=%.3f), capturing burst", score)
                captured = _capture_burst(camera, uploader, config, score)
                log.info("Burst done: %d photo(s) enqueued", captured)
            sent = uploader.flush()
            if sent:
                log.info("%d photo(s) uploaded to the server", sent)

            now_mono = time.monotonic()
            if now_mono - last_cleanup_at > CLEANUP_INTERVAL_SECONDS:
                deleted = uploader.cleanup_old_sent()
                if deleted:
                    log.info("Cleanup: removed %d old photo(s) from sent/", deleted)
                last_cleanup_at = now_mono


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
