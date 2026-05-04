"""Envoi des photos au serveur avec file d'attente locale et retry."""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from birdy_capture.config import UploadConfig

log = logging.getLogger(__name__)


class Uploader:
    def __init__(self, config: UploadConfig) -> None:
        self._cfg = config
        self.queue_dir = Path(config.queue_dir).expanduser()
        self.sent_dir = Path(config.sent_dir).expanduser()
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        self._last_flush_at = 0.0

    def enqueue(self, photo_path: Path, captured_at: datetime) -> Path:
        """Déplace la photo dans la queue d'envoi et écrit ses métadonnées."""
        suffix = photo_path.suffix or ".jpg"
        stem = captured_at.strftime("%Y%m%dT%H%M%S%f")
        target = self.queue_dir / f"{stem}{suffix}"
        shutil.move(str(photo_path), target)
        meta_path = target.with_suffix(target.suffix + ".meta.json")
        meta_path.write_text(
            json.dumps({"captured_at": captured_at.isoformat(), "filename": target.name})
        )
        log.info("Queued %s", target)
        return target

    def flush(self, now: float | None = None) -> int:
        """Tente d'envoyer toutes les photos en queue. Retourne le nombre envoyé.

        Respecte `retry_interval_seconds` entre les tentatives globales.
        """
        now = now if now is not None else time.monotonic()
        if now - self._last_flush_at < self._cfg.retry_interval_seconds and self._last_flush_at > 0:
            return 0
        self._last_flush_at = now

        sent = 0
        for photo in sorted(self.queue_dir.glob("*.jpg")):
            try:
                self._upload_one(photo)
                self._move_to_sent(photo)
                sent += 1
            except httpx.HTTPError as exc:
                log.warning("Upload failed for %s: %s", photo.name, exc)
                break  # on garde l'ordre, on réessaiera la prochaine
        return sent

    def cleanup_old_sent(self, now: datetime | None = None) -> int:
        """Supprime les photos envoyées plus vieilles que `sent_retention_days`."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=self._cfg.sent_retention_days)
        deleted = 0
        for photo in self.sent_dir.glob("*.jpg"):
            mtime = datetime.fromtimestamp(photo.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                meta = photo.with_suffix(photo.suffix + ".meta.json")
                photo.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                deleted += 1
        return deleted

    def _upload_one(self, photo: Path) -> None:
        meta_path = photo.with_suffix(photo.suffix + ".meta.json")
        captured_at = ""
        if meta_path.exists():
            captured_at = json.loads(meta_path.read_text()).get("captured_at", "")

        url = f"{self._cfg.server_url.rstrip('/')}/api/photos"
        headers: dict[str, str] = {}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        with photo.open("rb") as fp:
            files = {"file": (photo.name, fp, "image/jpeg")}
            data = {"captured_at": captured_at}
            response = httpx.post(
                url,
                files=files,
                data=data,
                headers=headers,
                timeout=self._cfg.request_timeout_seconds,
            )
        response.raise_for_status()
        log.info("Uploaded %s -> %s", photo.name, response.json().get("stored_path"))

    def _move_to_sent(self, photo: Path) -> None:
        target = self.sent_dir / photo.name
        shutil.move(str(photo), target)
        meta = photo.with_suffix(photo.suffix + ".meta.json")
        if meta.exists():
            shutil.move(str(meta), self.sent_dir / meta.name)
