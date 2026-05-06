"""Tests for apply.py -- write config.toml + restart wildwatch-capture."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from wildwatch_agent.apply import apply_desired_config, _merge_into_config_toml, FIELD_TO_SECTION


def _seed_config(path: Path) -> None:
    path.write_text(textwrap.dedent("""\
        [camera]
        capture_width = 2304
        capture_height = 1296
        rotation = 0

        [motion]
        pixel_threshold = 25
        area_threshold = 0.02

        [capture]
        burst_count = 3

        [upload]
        server_url = "https://wildwatch.example.com"
        api_key = "secret-token"
    """))


def test_field_to_section_mapping() -> None:
    # Just verify the mapping is complete for the 12 expected fields.
    expected = {
        "rotation", "capture_width", "capture_height", "detection_width", "detection_height",
        "pixel_threshold", "area_threshold", "background_alpha", "warmup_frames", "cooldown_seconds",
        "burst_count", "burst_interval_seconds",
    }
    assert set(FIELD_TO_SECTION.keys()) == expected


def test_merge_writes_only_changed_fields(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _seed_config(cfg)
    _merge_into_config_toml(cfg, {"rotation": 180, "burst_count": 5})

    text = cfg.read_text()
    # Updated values
    assert "rotation = 180" in text
    assert "burst_count = 5" in text
    # Untouched
    assert "pixel_threshold = 25" in text
    assert 'api_key = "secret-token"' in text
    assert 'server_url = "https://wildwatch.example.com"' in text


def test_merge_never_touches_upload_section(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _seed_config(cfg)
    # Even if a hostile desired carries an upload field (it shouldn't), drop it.
    _merge_into_config_toml(cfg, {"rotation": 90, "server_url": "https://evil"})
    assert 'server_url = "https://wildwatch.example.com"' in cfg.read_text()


def test_merge_atomic_write(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _seed_config(cfg)
    original = cfg.read_text()
    with patch("os.replace", side_effect=OSError("simulated")):
        with pytest.raises(OSError):
            _merge_into_config_toml(cfg, {"rotation": 180})
    assert cfg.read_text() == original  # original intact


def test_apply_desired_config_calls_systemctl(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _seed_config(cfg)
    with patch("subprocess.run") as mock_run:
        apply_desired_config({"rotation": 180}, config_path=cfg)
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "sudo" in args[0]
    assert "wildwatch-capture" in args[-1]
    assert "restart" in args
