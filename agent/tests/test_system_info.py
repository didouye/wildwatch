"""Tests for the agent system_info probes."""

from __future__ import annotations

from wildwatch_agent import system_info


def test_snapshot_keys() -> None:
    snap = system_info.snapshot(queue_dir=None)
    expected = {"cpu_temp_c", "cpu_usage_percent", "memory_avail_mb", "memory_total_mb",
                "load_avg_1min", "disk_avail_mb", "queue_size"}
    assert expected <= set(snap.keys())


def test_queue_size_counts_files(tmp_path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"y")
    (tmp_path / "c.json").write_bytes(b"{}")  # not a jpg
    snap = system_info.snapshot(queue_dir=tmp_path)
    assert snap["queue_size"] == 2


def test_cpu_usage_first_call_returns_none() -> None:
    # Reset module state so we test the "no baseline" path.
    system_info._last_cpu_snapshot = None
    assert system_info.cpu_usage_percent() is None
    # Second call: on Linux it returns a float (or 0.0), on macOS it stays None.
    second = system_info.cpu_usage_percent()
    assert second is None or isinstance(second, float)
