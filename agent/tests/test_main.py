"""Smoke test for agent main loop -- ensures imports + signatures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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
        apply_state=main_module.ApplyState(),
        config_path=Path("/tmp/whatever"),
    )
    client.send.assert_called_once()


def test_tick_applies_desired_when_received_first_time() -> None:
    state_reader = MagicMock()
    state_reader.read_status.return_value = (
        {"service_active": True, "current_config": {"rotation": 0}}, 1.0
    )
    state_reader.read_preview.return_value = (None, None)

    client = MagicMock()
    client.send.return_value = {"desired_config": {"rotation": 180}, "commands": []}

    apply_state = main_module.ApplyState()
    with patch("wildwatch_agent.apply.apply_desired_config") as mock_apply:
        main_module._tick(
            agent_version="1.2.0",
            agent_started_at_mono=0.0,
            state_reader=state_reader,
            heartbeat_client=client,
            queue_dir=None,
            apply_state=apply_state,
            config_path=Path("/tmp/whatever"),
        )

    mock_apply.assert_called_once()
    assert apply_state.last_apply_attempt_iso is not None
    assert apply_state.applied_desired_hash is not None


def test_tick_skips_apply_when_same_desired_again() -> None:
    state_reader = MagicMock()
    state_reader.read_status.return_value = ({"current_config": {"rotation": 0}}, 1.0)
    state_reader.read_preview.return_value = (None, None)
    client = MagicMock()
    client.send.return_value = {"desired_config": {"rotation": 180}, "commands": []}

    apply_state = main_module.ApplyState()
    with patch("wildwatch_agent.apply.apply_desired_config") as mock_apply:
        main_module._tick(  # 1st tick: applies
            agent_version="1.2.0", agent_started_at_mono=0.0,
            state_reader=state_reader, heartbeat_client=client,
            queue_dir=None, apply_state=apply_state, config_path=Path("/tmp/x"),
        )
        main_module._tick(  # 2nd tick: same desired, no re-apply
            agent_version="1.2.0", agent_started_at_mono=0.0,
            state_reader=state_reader, heartbeat_client=client,
            queue_dir=None, apply_state=apply_state, config_path=Path("/tmp/x"),
        )

    assert mock_apply.call_count == 1


def test_tick_clears_state_when_response_has_no_desired() -> None:
    apply_state = main_module.ApplyState(
        applied_desired_hash="abc",
        last_apply_attempt_iso="2026-05-06T00:00:00+00:00",
    )
    state_reader = MagicMock()
    state_reader.read_status.return_value = (None, None)
    state_reader.read_preview.return_value = (None, None)
    client = MagicMock()
    client.send.return_value = {"desired_config": None, "commands": []}

    main_module._tick(
        agent_version="1.2.0", agent_started_at_mono=0.0,
        state_reader=state_reader, heartbeat_client=client,
        queue_dir=None, apply_state=apply_state, config_path=Path("/tmp/x"),
    )

    assert apply_state.applied_desired_hash is None
    assert apply_state.last_apply_attempt_iso is None


def test_tick_passes_applied_at_in_payload() -> None:
    apply_state = main_module.ApplyState(
        applied_desired_hash="abc",
        last_apply_attempt_iso="2026-05-06T00:00:00+00:00",
    )
    state_reader = MagicMock()
    state_reader.read_status.return_value = ({"current_config": {"rotation": 180}}, 1.0)
    state_reader.read_preview.return_value = (None, None)
    client = MagicMock()
    client.send.return_value = {"desired_config": {"rotation": 180}, "commands": []}

    main_module._tick(
        agent_version="1.2.0", agent_started_at_mono=0.0,
        state_reader=state_reader, heartbeat_client=client,
        queue_dir=None, apply_state=apply_state, config_path=Path("/tmp/x"),
    )

    sent_payload = client.send.call_args.kwargs["payload"]
    assert sent_payload["applied_at"] == "2026-05-06T00:00:00+00:00"
