"""WildWatch agent -- heartbeat client + config applier.

PR2 wires `apply.py`: when the heartbeat response carries a `desired_config`,
hash it, apply it once, and report `applied_at` on subsequent heartbeats
until the server clears the desired (signaling success).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import signal
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wildwatch_agent import apply as apply_module
from wildwatch_agent import system_info
from wildwatch_agent.heartbeat import HeartbeatClient, build_payload
from wildwatch_agent.state_reader import StateReader

log = logging.getLogger("wildwatch_agent")

DEFAULT_CONFIG_PATH = Path("~/wildwatch/config.toml").expanduser()
AGENT_VERSION = "1.2.0"


class StopRequested(Exception):
    pass


@dataclass
class ApplyState:
    applied_desired_hash: str | None = None
    last_apply_attempt_iso: str | None = None


def _hash_desired(desired: dict) -> str:
    canonical = json.dumps(desired, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        log.info("Signal %s received, shutting down", signum)
        raise StopRequested

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _load_config(path: Path) -> tuple[str, str, Path | None, float]:
    """Return (server_url, token, queue_dir, heartbeat_interval_s)."""
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    with path.open("rb") as fp:
        raw = tomllib.load(fp)
    upload = raw.get("upload", {})
    agent = raw.get("agent", {})
    server_url = upload.get("server_url") or ""
    token = upload.get("api_key") or ""
    if not server_url or not token:
        raise SystemExit("upload.server_url and upload.api_key are required")
    queue_dir_str = upload.get("queue_dir") or ""
    queue_dir = Path(queue_dir_str).expanduser() if queue_dir_str else None
    interval = float(agent.get("heartbeat_interval_s", 30.0))
    return server_url, token, queue_dir, interval


def _tick(
    *,
    agent_version: str,
    agent_started_at_mono: float,
    state_reader: StateReader,
    heartbeat_client: HeartbeatClient,
    queue_dir: Path | None,
    apply_state: ApplyState,
    config_path: Path,
) -> None:
    sys_info = system_info.snapshot(queue_dir=queue_dir)
    capture_status, status_age_s = state_reader.read_status()
    preview_blob, preview_age_s = state_reader.read_preview(max_age_s=30.0)
    payload = build_payload(
        agent_version=agent_version,
        agent_uptime_s=int(time.monotonic() - agent_started_at_mono),
        system=sys_info,
        capture_status=capture_status,
        status_age_s=status_age_s,
        preview_age_s=preview_age_s,
        last_apply_attempt_iso=apply_state.last_apply_attempt_iso,
    )
    response = heartbeat_client.send(payload=payload, preview=preview_blob)
    if response is None:
        return  # network error, retry next tick

    desired = response.get("desired_config")
    if desired is None:
        # Server cleared (or never had) a desired. Reset state.
        apply_state.applied_desired_hash = None
        apply_state.last_apply_attempt_iso = None
        return

    # Server has a desired. Compare to what we last applied.
    new_hash = _hash_desired(desired)
    if new_hash == apply_state.applied_desired_hash:
        # Already applied this exact desired. Wait for server to ack.
        return

    # New (or different) desired. Attempt apply.
    log.info("Applying new desired_config from server")
    try:
        apply_module.apply_desired_config(desired, config_path=config_path)
        apply_state.applied_desired_hash = new_hash
        apply_state.last_apply_attempt_iso = datetime.now(timezone.utc).isoformat()
    except Exception:
        log.exception("apply_desired_config failed; will retry next tick")
        # Don't update state -- retry on next heartbeat.


def run(config_path: Path) -> None:
    server_url, token, queue_dir, interval = _load_config(config_path)
    log.info("Agent v%s -- server=%s, interval=%.1fs", AGENT_VERSION, server_url, interval)
    started = time.monotonic()
    reader = StateReader()
    client = HeartbeatClient(server_url=server_url, token=token, timeout=10.0)
    apply_state = ApplyState()
    while True:
        try:
            _tick(
                agent_version=AGENT_VERSION,
                agent_started_at_mono=started,
                state_reader=reader,
                heartbeat_client=client,
                queue_dir=queue_dir,
                apply_state=apply_state,
                config_path=config_path,
            )
        except Exception:
            log.exception("Heartbeat tick failed")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="WildWatch agent V1.2")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _install_signal_handlers()
    try:
        run(args.config)
    except StopRequested:
        log.info("Shutdown requested, exiting cleanly")


if __name__ == "__main__":
    main()
