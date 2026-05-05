"""Heartbeat payload builder + multipart POST client.

The agent posts to `/api/cameras/agent/heartbeat` with a multipart body:
  - `status`  : compact JSON form field describing agent + system + capture state
  - `preview` : optional JPEG file part (most recent frame)

The server replies with `{"desired_config": ..., "commands": [...]}` (PR2+ uses
`desired_config` to drive remote reconfiguration). On 4xx/5xx or network errors
`send()` returns `None` so the caller can back off without crashing the loop.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger("wildwatch_agent.heartbeat")


def build_payload(
    *,
    agent_version: str,
    agent_uptime_s: int,
    system: dict[str, Any],
    capture_status: dict | None,
    status_age_s: float | None,
    preview_age_s: float | None,
    last_apply_attempt_iso: str | None = None,
) -> dict:
    """Assemble the JSON payload posted to /api/cameras/agent/heartbeat."""
    capture: dict = {
        "service_active": bool(capture_status.get("service_active")) if capture_status else False,
        "status_age_s": status_age_s,
        "preview_age_s": preview_age_s,
        "last_capture_at": (capture_status or {}).get("last_capture_at"),
        "last_detection_at": (capture_status or {}).get("last_detection_at"),
        "error_count": (capture_status or {}).get("error_count", 0),
        "apply_error_observed": False,  # PR1 stub; PR2 sets this if apply fails
    }
    return {
        "agent": {"version": agent_version, "uptime_s": agent_uptime_s},
        "system": system,
        "capture": capture,
        "reported_config": (capture_status or {}).get("current_config", {}) or None,
        "applied_at": last_apply_attempt_iso,
    }


class HeartbeatClient:
    def __init__(self, server_url: str, token: str, timeout: float = 10.0) -> None:
        self._url = server_url.rstrip("/") + "/api/cameras/agent/heartbeat"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout

    def send(self, payload: dict, preview: bytes | None) -> dict | None:
        files: dict = {"status": (None, json.dumps(payload, separators=(",", ":")))}
        if preview is not None:
            files["preview"] = ("preview.jpg", preview, "image/jpeg")
        try:
            r = httpx.post(
                self._url,
                files=files,
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.warning("Heartbeat POST failed: %s", exc)
            return None
        if r.status_code == 200:
            return r.json()
        if r.status_code in (401, 403):
            log.warning("Heartbeat rejected (%d): %s", r.status_code, r.text[:200])
            return None
        log.warning("Heartbeat returned %d: %s", r.status_code, r.text[:200])
        return None
