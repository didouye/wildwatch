"""Envoi des photos au serveur avec file d'attente locale et retry."""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from wildwatch_capture.config import UploadConfig

log = logging.getLogger(__name__)


class Uploader:
    """Statuses HTTP considérés transitoires : on garde la photo en queue pour réessayer."""

    TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, config: UploadConfig) -> None:
        self._cfg = config
        self.queue_dir = Path(config.queue_dir).expanduser()
        self.sent_dir = Path(config.sent_dir).expanduser()
        self.dead_dir = self.queue_dir.parent / "dead"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        self.dead_dir.mkdir(parents=True, exist_ok=True)
        self._last_flush_at = 0.0

    def enqueue(
        self,
        photo_path: Path,
        captured_at: datetime,
        extra_metadata: dict[str, object] | None = None,
    ) -> Path:
        """Déplace la photo dans la queue d'envoi et écrit ses métadonnées.

        `extra_metadata` est mergé dans le JSON et peut contenir : motion_score,
        frame_index, burst_size, camera (résolution, format), sensor (model,
        exposure_time, gain), system (cpu_temp, memory, hostname), etc.
        """
        suffix = photo_path.suffix or ".jpg"
        stem = captured_at.strftime("%Y%m%dT%H%M%S%f")
        target = self.queue_dir / f"{stem}{suffix}"
        shutil.move(str(photo_path), target)
        metadata: dict[str, object] = {
            "captured_at": captured_at.isoformat(),
            "filename": target.name,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        meta_path = target.with_suffix(target.suffix + ".meta.json")
        meta_path.write_text(json.dumps(metadata, sort_keys=True))
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
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in self.TRANSIENT_STATUS:
                    log.warning(
                        "Upload temporairement échoué (%s) pour %s, réessai plus tard",
                        exc.response.status_code,
                        photo.name,
                    )
                    break  # on garde l'ordre, on réessaiera
                log.error(
                    "Upload échoué de manière irrécupérable (%s) pour %s, déplacement en dead-letter",
                    exc.response.status_code,
                    photo.name,
                )
                self._move_to_dead(photo)
            except httpx.HTTPError as exc:
                log.warning("Upload échoué (réseau) pour %s: %s", photo.name, exc)
                break  # erreur réseau = transitoire, on garde l'ordre
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
        metadata_str: str | None = None
        if meta_path.exists():
            metadata_str = meta_path.read_text()
            captured_at = json.loads(metadata_str).get("captured_at", "")

        url = f"{self._cfg.server_url.rstrip('/')}/api/photos"
        headers: dict[str, str] = {}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"

        with photo.open("rb") as fp:
            files = {"file": (photo.name, fp, "image/jpeg")}
            data: dict[str, str] = {"captured_at": captured_at}
            if metadata_str:
                data["metadata"] = metadata_str
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
        self._move_with_meta(photo, self.sent_dir)

    def _move_to_dead(self, photo: Path) -> None:
        self._move_with_meta(photo, self.dead_dir)

    @staticmethod
    def _move_with_meta(photo: Path, target_dir: Path) -> None:
        shutil.move(str(photo), target_dir / photo.name)
        meta = photo.with_suffix(photo.suffix + ".meta.json")
        if meta.exists():
            shutil.move(str(meta), target_dir / meta.name)
