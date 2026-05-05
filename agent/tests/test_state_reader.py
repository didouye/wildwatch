from __future__ import annotations

import json
import time
from pathlib import Path

from wildwatch_agent.state_reader import StateReader


def test_returns_none_when_dir_missing(tmp_path: Path) -> None:
    reader = StateReader(state_dir=tmp_path / "missing")
    assert reader.read_status() == (None, None)
    assert reader.read_preview(max_age_s=10) == (None, None)


def test_reads_status(tmp_path: Path) -> None:
    snap = {"service_active": True, "current_config": {"rotation": 0}}
    (tmp_path / "status.json").write_text(json.dumps(snap))
    reader = StateReader(state_dir=tmp_path)
    parsed, age = reader.read_status()
    assert parsed == snap
    assert 0 <= age < 5


def test_skips_stale_preview(tmp_path: Path) -> None:
    p = tmp_path / "preview.jpg"
    p.write_bytes(b"\xff\xd8data")
    old = time.time() - 60
    import os

    os.utime(p, (old, old))
    reader = StateReader(state_dir=tmp_path)
    blob, age = reader.read_preview(max_age_s=10)
    assert blob is None
    assert age is not None and age >= 60
