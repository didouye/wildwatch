"""WildWatch agent -- heartbeat client + (PR2+) config applier.

PR1 scope: heartbeat only. The `desired_config` from the server response
is logged but not applied. PR2 wires `apply.py`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
import tomllib
from pathlib import Path

from wildwatch_agent import system_info
from wildwatch_agent.heartbeat import HeartbeatClient, build_payload
from wildwatch_agent.state_reader import StateReader

log = logging.getLogger("wildwatch_agent")

DEFAULT_CONFIG_PATH = Path("~/wildwatch/config.toml").expanduser()
AGENT_VERSION = "1.2.0"


class StopRequested(Exception):
    pass


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
    )
    response = heartbeat_client.send(payload=payload, preview=preview_blob)
    if response is not None:
        desired = response.get("desired_config")
        cmds = response.get("commands") or []
        if desired or cmds:
            # PR1: log only; PR2 will dispatch to apply.py.
            log.info("desired_config or commands received (ignored in PR1)")


def run(config_path: Path) -> None:
    server_url, token, queue_dir, interval = _load_config(config_path)
    log.info("Agent v%s -- server=%s, interval=%.1fs", AGENT_VERSION, server_url, interval)
    started = time.monotonic()
    reader = StateReader()
    client = HeartbeatClient(server_url=server_url, token=token, timeout=10.0)
    while True:
        try:
            _tick(
                agent_version=AGENT_VERSION,
                agent_started_at_mono=started,
                state_reader=reader,
                heartbeat_client=client,
                queue_dir=queue_dir,
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
