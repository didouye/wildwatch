"""WildWatch capture client V0.2 — détection de mouvement + capture rafale + upload.

Lit la config dans ~/wildwatch/config.toml (ou --config), tourne en boucle :
  1. Lit une frame basse résolution
  2. La passe au détecteur de mouvement (background subtraction adaptatif)
  3. Si mouvement détecté → capture une rafale haute résolution
  4. Met les photos en queue locale, tente de les envoyer au serveur
  5. Reprend la surveillance

Arrêt propre via SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import logging
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

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
        log.info("Signal %s reçu, arrêt en cours", signum)
        raise StopRequested

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _capture_burst(camera: Camera, uploader: Uploader, config: Config) -> int:
    """Capture une rafale et l'enqueue. Retourne le nombre de photos."""
    captured = 0
    for i in range(config.capture.burst_count):
        captured_at = datetime.now(timezone.utc)
        with tempfile.NamedTemporaryFile(prefix="wildwatch_", suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            camera.capture_to_file(tmp_path)
            uploader.enqueue(tmp_path, captured_at)
            captured += 1
        except Exception as exc:
            log.exception("Échec capture %d/%d: %s", i + 1, config.capture.burst_count, exc)
            tmp_path.unlink(missing_ok=True)
        if i < config.capture.burst_count - 1:
            time.sleep(config.capture.burst_interval_seconds)
    return captured


CLEANUP_INTERVAL_SECONDS = 3600.0  # une fois par heure


def run(config: Config) -> None:
    detector = MotionDetector(config.motion)
    uploader = Uploader(config.upload)
    last_cleanup_at = 0.0

    log.info("Démarrage de la caméra")
    with Camera(config.camera) as camera:
        log.info("Boucle de surveillance démarrée")
        while True:
            frame = camera.read_detection_frame()
            triggered = detector.process(frame, now=time.monotonic())
            if triggered:
                log.info("Mouvement détecté (score=%.3f), capture rafale", detector.last_motion_score)
                captured = _capture_burst(camera, uploader, config)
                log.info("Rafale terminée : %d photo(s) enqueue(s)", captured)
            sent = uploader.flush()
            if sent:
                log.info("%d photo(s) envoyée(s) au serveur", sent)

            now_mono = time.monotonic()
            if now_mono - last_cleanup_at > CLEANUP_INTERVAL_SECONDS:
                deleted = uploader.cleanup_old_sent()
                if deleted:
                    log.info("Cleanup : %d photo(s) ancienne(s) supprimée(s) de sent/", deleted)
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
    log.info("Config chargée depuis %s", args.config if args.config.exists() else "(défaut)")
    log.info("Serveur cible : %s", config.upload.server_url)

    _install_signal_handlers()

    try:
        run(config)
    except StopRequested:
        log.info("Arrêt demandé, sortie propre")


if __name__ == "__main__":
    main()
