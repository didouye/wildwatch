"""Smoke test for agent main loop -- ensures imports + signatures."""

from __future__ import annotations

from unittest.mock import MagicMock

from wildwatch_agent import main as main_module


def test_loop_once_callable() -> None:
    state_reader = MagicMock()
    state_reader.read_status.return_value = ({"service_active": True, "current_config": {}}, 1.0)
    state_reader.read_preview.return_value = (None, None)

    client = MagicMock()
    client.send.return_value = {"desired_config": None, "commands": []}

    started_at = 0.0
    main_module._tick(
        agent_version="1.2.0",
        agent_started_at_mono=started_at,
        state_reader=state_reader,
        heartbeat_client=client,
        queue_dir=None,
    )
    client.send.assert_called_once()
