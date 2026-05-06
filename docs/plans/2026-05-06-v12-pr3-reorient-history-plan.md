# V1.2 PR 3 -- Reorient historical photos on rotation change

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** When the operator changes a camera's `rotation` from the edit modal, offer (default-on) to also rotate every photo previously uploaded by that camera by the same delta. Once the agent acks the config change, a BackgroundTask iterates the photos, rotates them in place, swaps cached thumbnails, and updates `camera_width/height` for delta ∈ {90, 270}.

**Architecture:**
- Server computes the delta from `(submitted.rotation - reported.rotation) mod 360`. Stores in `Camera.pending_reorient_delta` (column already added in PR1's `upgrade_to_v12`).
- The heartbeat reconciliation that clears `desired_config` (Task 1 of PR2) ALSO schedules a `BackgroundTask` if `pending_reorient_delta IS NOT NULL`, then clears the column at the end of the job.
- The job (`reorient.py`) opens its own DB session, walks `Photo` rows for the camera with `captured_at <= ack_time`, calls `PIL.Image.rotate(-delta, expand=True)`, saves at q=95, deletes cached thumbnails, swaps `camera_width/height` if delta ∈ {90, 270}.
- In-memory `reorient_progress[camera_id] = (done, total)` lets the card show a `↻ Reorienting N/M photos...` sticker during the run. Lost on server restart (acceptable; only affects the UI counter, not the rotation work).

**Tech stack:** FastAPI BackgroundTasks (already used by photo uploads), Pillow (already used by thumbnails), SQLModel for DB iteration. No new dependencies.

**Companion design:** `docs/plans/2026-05-06-camera-control-plane-design.md` Section 6.

---

## Conventions

- TDD: failing test, run, confirm fail, implement, run, confirm pass, commit. Each task is one TDD cycle ending in a commit.
- Commit messages: `feat(v1.2): ...` or `fix(v1.2): ...`.
- Test commands run from `server/` with the existing `uv` venv.
- Baseline (post-PR2 merge): server tests = 116 passed, capture = 26, agent = 19. Goal: server ≈ 125 after PR3.
- The schema migration was already applied in PR1's `upgrade_to_v12` (`pending_reorient_delta` column exists). **No new migration in PR3.**

---

## Task 1: `reorient.py` module -- rotate photos + swap thumbs + progress

**Files:**
- Create: `server/src/wildwatch_server/reorient.py`
- Create: `server/tests/test_reorient.py`

**Goal:** a pure module that does the rotation work. Easy to unit-test in isolation; later wired into a FastAPI BackgroundTask.

**Module shape:**

```python
"""Rotate historical photos for a camera by a delta in {90, 180, 270}.

Walks every Photo row whose camera_id matches and whose captured_at is at
or before `ack_time`. For each, rotates the JPEG in place at quality 95,
deletes cached thumbnails (regenerated lazily next view), and (if delta is
a quarter-turn) swaps `camera_width`/`camera_height` in the DB row.

In-memory progress counter `reorient_progress[camera_id] = (done, total)`
is updated as the job runs. The counter disappears on server restart;
photos already rotated stay rotated, the rest don't.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from threading import Lock

from PIL import Image
from sqlmodel import Session, select

from wildwatch_server.db import get_engine
from wildwatch_server.models import Camera, Photo
from wildwatch_server.storage import photos_dir
from wildwatch_server.thumbnails import THUMBNAIL_SIZES, thumbnail_path

log = logging.getLogger(__name__)

_lock = Lock()
reorient_progress: dict[int, tuple[int, int]] = {}


def _pil_rotation_for_delta(delta: int) -> int:
    """Convert a clockwise delta to PIL's counter-clockwise rotation argument.

    PIL's Image.rotate(angle) rotates counter-clockwise. Operator wants
    clockwise. So for delta=90 (CW), we pass -90 to PIL (= 270 CCW).
    """
    return (-delta) % 360


def _rotate_one_photo(photo: Photo, delta: int, base_dir: Path) -> bool:
    """Rotate one photo file in place + delete its thumbs. Returns True on success."""
    src = base_dir / photo.file_path
    if not src.exists():
        log.warning("Source missing, skipping reorient: %s", src)
        return False
    try:
        with Image.open(src) as img:
            rotated = img.rotate(_pil_rotation_for_delta(delta), expand=True)
            if rotated.mode != "RGB":
                rotated = rotated.convert("RGB")
            tmp = src.with_suffix(src.suffix + ".tmp")
            rotated.save(tmp, "JPEG", quality=95, optimize=True)
        import os
        os.replace(tmp, src)
    except Exception:
        log.exception("Failed to rotate %s", src)
        return False

    for size in THUMBNAIL_SIZES:
        thumbnail_path(photo.file_path, size).unlink(missing_ok=True)
    return True


def reorient_camera_photos(
    camera_id: int, delta: int, ack_time: datetime
) -> dict:
    """Synchronously rotate every photo for a camera, captured at or before ack_time.

    Designed to be invoked from a FastAPI BackgroundTask. Updates the
    in-memory progress counter as it goes. Clears `pending_reorient_delta`
    on the Camera row when finished.
    """
    if delta not in (90, 180, 270):
        raise ValueError(f"delta must be one of 90/180/270, got {delta}")

    engine = get_engine()
    base = photos_dir()
    rotated_count = 0
    skipped_count = 0

    with Session(engine) as session:
        photos = session.exec(
            select(Photo)
            .where(Photo.camera_id == camera_id)
            .where(Photo.captured_at <= ack_time)
        ).all()
        total = len(photos)
        with _lock:
            reorient_progress[camera_id] = (0, total)

        for i, photo in enumerate(photos, 1):
            if _rotate_one_photo(photo, delta, base):
                rotated_count += 1
                if delta in (90, 270):
                    photo.camera_width, photo.camera_height = (
                        photo.camera_height,
                        photo.camera_width,
                    )
                    session.add(photo)
            else:
                skipped_count += 1
            with _lock:
                reorient_progress[camera_id] = (i, total)

        # Commit dim-swap updates in one batch.
        session.commit()

        # Clear the pending delta so the UI knows the job finished.
        cam = session.get(Camera, camera_id)
        if cam is not None:
            cam.pending_reorient_delta = None
            session.add(cam)
            session.commit()

    with _lock:
        reorient_progress.pop(camera_id, None)

    return {"rotated": rotated_count, "skipped": skipped_count, "total": total}
```

**Tests** (`server/tests/test_reorient.py`):

```python
"""Tests for the reorient.py module (rotate photos in place + swap dims)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session

from wildwatch_server import reorient
from wildwatch_server.models import Camera, Photo


@pytest.fixture
def setup_db_and_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "admin-key")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    from wildwatch_server import db as db_module
    db_module.reset_engine_cache()
    db_module.init_db()
    return db_module.get_engine()


def _seed_camera(engine, hostname: str = "rpi") -> Camera:
    with Session(engine) as s:
        cam = Camera(token="t", hostname=hostname, status="approved")
        s.add(cam)
        s.commit()
        s.refresh(cam)
        return cam


def _seed_photo(engine, camera_id: int, captured_at: datetime, w: int, h: int,
                 base: Path, name: str) -> Photo:
    """Create a JPEG on disk + a Photo row pointing at it."""
    rel = f"2026/05/06/{name}.jpg"
    full = base / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color=(255, 0, 0)).save(full, "JPEG", quality=95)
    with Session(engine) as s:
        photo = Photo(
            captured_at=captured_at,
            file_path=rel,
            file_size=full.stat().st_size,
            hostname="rpi",
            camera_id=camera_id,
            camera_width=w,
            camera_height=h,
        )
        s.add(photo)
        s.commit()
        s.refresh(photo)
        return photo


def test_pil_rotation_for_delta_inverts_sign() -> None:
    assert reorient._pil_rotation_for_delta(90) == 270  # 90 CW = 270 CCW
    assert reorient._pil_rotation_for_delta(180) == 180
    assert reorient._pil_rotation_for_delta(270) == 90  # 270 CW = 90 CCW


def test_reorient_rotates_photos_and_swaps_dims_for_90(
    setup_db_and_storage, tmp_path: Path
) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    photo = _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "p1")

    result = reorient.reorient_camera_photos(cam.id, delta=90, ack_time=now)

    assert result == {"rotated": 1, "skipped": 0, "total": 1}
    # File on disk: dimensions swapped.
    with Image.open(base / photo.file_path) as img:
        assert img.size == (100, 200)
    # DB: dims swapped.
    with Session(engine) as s:
        row = s.get(Photo, photo.id)
        assert row.camera_width == 100
        assert row.camera_height == 200


def test_reorient_180_keeps_dims(setup_db_and_storage, tmp_path: Path) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "p1")

    reorient.reorient_camera_photos(cam.id, delta=180, ack_time=now)

    with Session(engine) as s:
        row = s.exec(__import__("sqlmodel").select(Photo)).first()
        assert (row.camera_width, row.camera_height) == (200, 100)


def test_reorient_skips_photos_after_ack_time(
    setup_db_and_storage, tmp_path: Path
) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    # One BEFORE ack, one AFTER ack.
    _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "before")
    _seed_photo(engine, cam.id, now + timedelta(hours=1), 200, 100, base, "after")

    result = reorient.reorient_camera_photos(cam.id, delta=180, ack_time=now)
    assert result["total"] == 1
    assert result["rotated"] == 1


def test_reorient_clears_pending_delta_when_done(
    setup_db_and_storage,
) -> None:
    engine = setup_db_and_storage
    with Session(engine) as s:
        cam = Camera(token="t", hostname="rpi", status="approved",
                     pending_reorient_delta=180)
        s.add(cam); s.commit(); s.refresh(cam)
    now = datetime.now(timezone.utc)

    reorient.reorient_camera_photos(cam.id, delta=180, ack_time=now)

    with Session(engine) as s:
        assert s.get(Camera, cam.id).pending_reorient_delta is None


def test_reorient_deletes_cached_thumbnails(
    setup_db_and_storage, tmp_path: Path
) -> None:
    engine = setup_db_and_storage
    cam = _seed_camera(engine)
    base = tmp_path / "photos"
    now = datetime.now(timezone.utc)
    photo = _seed_photo(engine, cam.id, now - timedelta(hours=1), 200, 100, base, "p1")
    # Pre-cache a thumb.
    from wildwatch_server.thumbnails import thumbnail_path
    thumb = thumbnail_path(photo.file_path, 150)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"fake-thumb")
    assert thumb.exists()

    reorient.reorient_camera_photos(cam.id, delta=90, ack_time=now)

    assert not thumb.exists()  # purged


def test_reorient_invalid_delta_raises(setup_db_and_storage) -> None:
    with pytest.raises(ValueError, match="delta must be"):
        reorient.reorient_camera_photos(camera_id=1, delta=45,
                                          ack_time=datetime.now(timezone.utc))
```

**Steps:**

1. Add the 6 failing tests to `server/tests/test_reorient.py`.
2. Run targeted: `cd server && uv run pytest tests/test_reorient.py -v`. Confirm RED (`ImportError: cannot import name 'reorient' from 'wildwatch_server'`).
3. Create `server/src/wildwatch_server/reorient.py` per the module shape above.
4. Run targeted, confirm GREEN.
5. Run full suite: `uv run pytest -q` -> **122 passed** (116 + 6).
6. Run ruff: clean.
7. Commit: `feat(v1.2): reorient.py -- rotate historical photos + swap dims + thumb purge`.

**Don't push.**

---

## Task 2: `POST /cameras/{id}/config` accepts `reorient_existing` flag

**Files:**
- Modify: `server/src/wildwatch_server/routes/ui.py` (`post_camera_config`)
- Modify: `server/tests/test_v12.py`

**Goal:** when the form includes `reorient_existing=on` (checkbox checked) AND the rotation actually changes vs `reported_config`, server computes `delta = (new - old) mod 360` and stores it in `Camera.pending_reorient_delta` along with `desired_config`.

**Step 1: failing tests**

```python
def test_post_config_with_reorient_sets_pending_delta(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5",
              "reorient_existing": "on"},
    )
    assert res.status_code == 200
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        # rotation changed 0 -> 180, delta = 180
        assert row.pending_reorient_delta == 180
        # And desired_config holds the rotation change (minimal diff)
        assert json.loads(row.desired_config) == {"rotation": 180}


def test_post_config_with_reorient_no_rotation_change_stores_no_delta(
    client: TestClient,
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 90, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    # Submit same rotation but with reorient_existing checked. No delta.
    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "90", "capture_width": "1536", "capture_height": "864",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5",
              "reorient_existing": "on"},
    )
    assert res.status_code == 200
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        # capture_width/height changed but rotation stayed → no delta
        assert row.pending_reorient_delta is None


def test_post_config_without_reorient_flag_skips_delta(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    # Rotation changes but checkbox NOT checked.
    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
        # no reorient_existing field
    )
    assert res.status_code == 200
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.pending_reorient_delta is None
        # desired_config still set for the rotation change
        assert json.loads(row.desired_config) == {"rotation": 180}
```

**Step 2: confirm RED.**

**Step 3: implement.** Modify `post_camera_config` in `routes/ui.py`. After computing `diff`, before the commit:

```python
    # Reorient flag: only set pending_reorient_delta if rotation actually changes.
    reorient_existing = (form.get("reorient_existing") in ("on", "1", "true", "yes"))
    pending_delta: int | None = None
    if reorient_existing and "rotation" in diff:
        new_rot = diff["rotation"]
        old_rot = (reported.get("rotation") or 0)
        delta = (new_rot - old_rot) % 360
        if delta in (90, 180, 270):
            pending_delta = delta

    cam.desired_config = json.dumps(diff) if diff else None
    cam.pending_reorient_delta = pending_delta
    session.add(cam)
    session.commit()
```

Note: explicitly set `cam.pending_reorient_delta = pending_delta` (which is None when not requested) to ensure it's cleared if the operator cancels and re-submits without the checkbox.

**Step 4: GREEN. Run full server suite -> 119 passed (116 + 3).**

**Step 5: ruff clean. Commit:** `feat(v1.2): config endpoint stores pending_reorient_delta on rotation change`.

---

## Task 3: Heartbeat schedules BackgroundTask when desired clears + delta is set

**Files:**
- Modify: `server/src/wildwatch_server/routes/agent.py`
- Modify: `server/tests/test_v12.py`

**Goal:** when the heartbeat reconciliation clears `desired_config` (success path) AND `pending_reorient_delta` is non-null, schedule `reorient.reorient_camera_photos(...)` as a `BackgroundTask`. The reorient module itself clears `pending_reorient_delta` when the job is done.

**Step 1: failing test**

```python
def test_heartbeat_schedules_reorient_when_apply_succeeds_with_delta(
    client: TestClient, tmp_path: Path
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Pre-set desired + pending_reorient_delta as Task 2 would.
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"rotation": 180})
        row.pending_reorient_delta = 180
        s.commit()

    # Heartbeat with applied_at + reported matching desired -> server clears
    # desired AND should schedule the reorient BG task.
    from unittest.mock import patch
    with patch("wildwatch_server.routes.agent.reorient.reorient_camera_photos") as mock_job:
        payload = {
            "agent": {"version": "1.2.0"},
            "applied_at": "2026-05-06T00:00:00+00:00",
            "reported_config": {"rotation": 180},
        }
        res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200

    # The mock should have been queued as a BG task and executed.
    mock_job.assert_called_once()
    args, kwargs = mock_job.call_args
    # camera_id passed as kwarg or positional; assert delta + ack_time present
    call_camera_id = kwargs.get("camera_id", args[0] if args else None)
    call_delta = kwargs.get("delta", args[1] if len(args) > 1 else None)
    assert call_camera_id == cam["id"]
    assert call_delta == 180


def test_heartbeat_does_not_schedule_reorient_when_no_delta(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"capture_width": 1536})  # no rotation change
        # no pending_reorient_delta
        s.commit()

    from unittest.mock import patch
    with patch("wildwatch_server.routes.agent.reorient.reorient_camera_photos") as mock_job:
        payload = {
            "agent": {"version": "1.2.0"},
            "applied_at": "2026-05-06T00:00:00+00:00",
            "reported_config": {"capture_width": 1536},
        }
        _heartbeat(client, cam["token"], payload)

    mock_job.assert_not_called()


def test_heartbeat_does_not_schedule_reorient_on_apply_error(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"rotation": 180})
        row.pending_reorient_delta = 180
        s.commit()

    from unittest.mock import patch
    with patch("wildwatch_server.routes.agent.reorient.reorient_camera_photos") as mock_job:
        payload = {
            "agent": {"version": "1.2.0"},
            "applied_at": "2026-05-06T00:00:00+00:00",
            "reported_config": {"rotation": 0},  # MISMATCH -> apply_error
        }
        _heartbeat(client, cam["token"], payload)

    # Apply error -> desired stays, pending_reorient_delta stays, NO bg task.
    mock_job.assert_not_called()
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        assert row.pending_reorient_delta == 180
```

**Step 2: confirm RED.**

**Step 3: implement.** Modify `routes/agent.py`:

Imports:
```python
from datetime import datetime, timezone
from fastapi import BackgroundTasks
from wildwatch_server import reorient
```

Update the route signature to accept `BackgroundTasks`:
```python
async def heartbeat(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    status: str = Form(...),
    preview: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict:
```

Inside the success branch (where `cam.desired_config = None` is set), capture the delta and schedule:

```python
    apply_error_observed = False
    reorient_delta_to_run: int | None = None
    ack_time = datetime.now(timezone.utc)
    if desired and applied_at:
        if all(reported.get(k) == v for k, v in desired.items()):
            cam.desired_config = None
            desired = None
            # If a reorient was queued, capture the delta to schedule.
            if cam.pending_reorient_delta is not None:
                reorient_delta_to_run = cam.pending_reorient_delta
        else:
            apply_error_observed = True
```

After `session.commit()`:

```python
    if reorient_delta_to_run is not None:
        background_tasks.add_task(
            reorient.reorient_camera_photos,
            camera_id=cam.id,
            delta=reorient_delta_to_run,
            ack_time=ack_time,
        )
```

**Step 4: GREEN. Full suite -> 122 passed (119 + 3).**

**Step 5: ruff clean. Commit:** `feat(v1.2): heartbeat schedules reorient BG task on apply success with delta`.

---

## Task 4: Surface reorient progress on the camera card

**Files:**
- Modify: `server/src/wildwatch_server/routes/ui.py` (`_camera_card_context`)
- Modify: `server/src/wildwatch_server/templates/_camera_card.html`
- Modify: `server/tests/test_v12.py`

**Goal:** when `pending_reorient_delta IS NOT NULL` OR there's an entry in `reorient_progress`, show a sticker on the card: `↻ Reorienting 12/342 photos...` (or `↻ Reorient queued` if not yet started).

**Step 1: failing tests**

```python
def test_card_shows_reorient_in_progress(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Manually populate the in-memory progress dict.
    from wildwatch_server import reorient
    reorient.reorient_progress[cam["id"]] = (12, 50)
    try:
        res = client.get(f"/cameras/{cam['id']}/card")
        assert "Reorienting" in res.text
        assert "12" in res.text and "50" in res.text
    finally:
        reorient.reorient_progress.pop(cam["id"], None)


def test_card_shows_reorient_queued_when_pending_no_progress_yet(
    client: TestClient,
) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.pending_reorient_delta = 90
        s.commit()
    res = client.get(f"/cameras/{cam['id']}/card")
    # Either "queued" or "Reorienting" with 0/?
    assert "Reorient" in res.text
    assert "90" in res.text  # delta visible
```

**Step 2: RED.**

**Step 3: implement.**

In `_camera_card_context` (`routes/ui.py`), add to the return dict:

```python
from wildwatch_server import reorient as reorient_module

reorient_progress = reorient_module.reorient_progress.get(cam.id)
return {
    ...
    "reorient_pending_delta": cam.pending_reorient_delta,
    "reorient_progress": reorient_progress,  # tuple (done, total) or None
}
```

In `_camera_card.html`, between the apply-error banner and the action row, add:

```html
{% if reorient_pending_delta or reorient_progress %}
  <div class="rounded border border-sky-700 bg-sky-950/40 p-2 text-xs text-sky-200">
    {% if reorient_progress %}
      &#x21bb; Reorienting {{ reorient_progress[0] }}/{{ reorient_progress[1] }} photo(s)
      by {{ reorient_pending_delta or "?" }}&deg;...
    {% else %}
      &#x21bb; Reorient queued ({{ reorient_pending_delta }}&deg;) -- will start
      after the next heartbeat ack.
    {% endif %}
  </div>
{% endif %}
```

**Step 4: GREEN. Full suite -> 124 passed (122 + 2).**

**Step 5: ruff. Commit:** `feat(v1.2): card shows reorient queued / progress sticker`.

---

## Task 5: Edit modal -- "Also reorient" checkbox + JS delta display

**Files:**
- Modify: `server/src/wildwatch_server/templates/_edit_settings_modal.html`
- Modify: `server/tests/test_v12.py`

**Goal:** add a row in the modal (after the rotation select) that:
- Shows current photo count for that camera (already in `ctx.photo_count`).
- Shows the computed delta when rotation differs from `effective.rotation`.
- Has a checkbox `name="reorient_existing"` (default checked).
- Hidden by default; revealed by JS when rotation changes.

**Step 1: failing test**

```python
def test_edit_modal_includes_reorient_checkbox(client: TestClient) -> None:
    cam = _enroll(client, hostname="rpi-1")
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296,
                            "detection_width": 640, "detection_height": 480,
                            "pixel_threshold": 25, "area_threshold": 0.02,
                            "background_alpha": 0.05, "warmup_frames": 30,
                            "cooldown_seconds": 5.0, "burst_count": 3,
                            "burst_interval_seconds": 0.5},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get("/cameras")
    body = res.text
    assert 'name="reorient_existing"' in body
    # The checkbox row references the photo count (0 photos yet).
    assert "existing photo" in body.lower() or "photo(s)" in body
```

**Step 2: RED.**

**Step 3: implement.** In `_edit_settings_modal.html`, after the rotation `<select>` block, add:

```html
<div class="reorient-row hidden flex items-start gap-3 pl-32 text-xs text-amber-200" data-initial-rotation="{{ effective.get('rotation', 0) }}">
  <input type="checkbox" name="reorient_existing" id="reorient-existing-{{ ctx.cam.id }}" checked
         class="mt-0.5">
  <label for="reorient-existing-{{ ctx.cam.id }}">
    Also rotate the {{ ctx.photo_count }} existing photo(s) of this camera
    by <span class="reorient-delta">?</span>&deg; clockwise.
  </label>
</div>

<script>
(function() {
  const modal = document.getElementById('edit-settings-modal-{{ ctx.cam.id }}');
  if (!modal || modal.dataset.reorientWired === '1') return;
  modal.dataset.reorientWired = '1';
  const sel = modal.querySelector('select[name="rotation"]');
  const row = modal.querySelector('.reorient-row');
  const label = modal.querySelector('.reorient-delta');
  const initial = parseInt(row.dataset.initialRotation || '0', 10);
  function update() {
    const cur = parseInt(sel.value, 10);
    const delta = ((cur - initial) % 360 + 360) % 360;
    if (delta === 0) {
      row.classList.add('hidden');
    } else {
      row.classList.remove('hidden');
      label.textContent = String(delta);
    }
  }
  sel.addEventListener('change', update);
})();
</script>
```

The `dataset.reorientWired` guard makes the script idempotent in case the modal markup is re-included by htmx.

**Step 4: GREEN. Full suite -> 125 passed (124 + 1).**

**Step 5: ruff (no Python changes -> no impact). Commit:** `feat(v1.2): edit modal -- reorient checkbox + JS delta display`.

---

## Task 6: ROADMAP entry

**Files:**
- Modify: `ROADMAP.md`

Add a new block under the existing V1.2 section:

```markdown
### V1.2 PR 3: reorient historical photos on rotation change

- [x] `wildwatch_server.reorient` module: walks
      `Photo.camera_id == X AND captured_at <= ack_time`, rotates each
      JPEG in place at q=95 (PIL counter-clockwise = `-delta` for the
      operator's clockwise mental model), atomic save, deletes cached
      thumbnails (lazy regeneration), swaps `camera_width/height` for
      `delta in {90, 270}`, clears `pending_reorient_delta` at the end
- [x] In-memory `reorient_progress[camera_id] = (done, total)` updated
      every photo. Lost on server restart (only the counter; rotated
      photos stay rotated)
- [x] `POST /cameras/{id}/config` learns about the `reorient_existing`
      form field. Server computes `delta = (new_rotation - reported_rotation) mod 360`
      and stores it in `pending_reorient_delta` only when both checkbox is
      checked AND rotation actually changes
- [x] Heartbeat reconciliation: when apply succeeds (`reported == desired`,
      desired cleared), if `pending_reorient_delta IS NOT NULL` schedule
      `reorient_camera_photos(...)` as a `BackgroundTasks` job. Apply
      errors do NOT trigger reorient (delta stays for retry/cancel)
- [x] Camera card: amber sticker `↻ Reorienting N/M photos...` while the
      job runs, `↻ Reorient queued` while waiting for ack
- [x] Edit modal: row with the photo count, computed delta, and an
      `reorient_existing` checkbox (default checked). Visibility wired
      to the rotation `<select>` via inline JS; row stays hidden until
      rotation actually differs from `reported_config.rotation`
- [x] No new dependencies. Pillow already in capture's deps; thumbnails
      module already imported. No schema migration needed
      (`pending_reorient_delta` was added in PR1's `upgrade_to_v12`)
- [x] X new tests across reorient module (6) + endpoints + UI. Ruff clean.
```

Commit: `docs(v1.2): roadmap entry for PR3 reorient history`.

---

## Final integration verification

After all 6 tasks + ROADMAP:

1. **All test suites green:**
   ```bash
   cd /Users/didouye/Workspace/birdyphotobooth/server && uv run pytest -q  # ≥ 125
   cd /Users/didouye/Workspace/birdyphotobooth/agent && uv run pytest -q   # 19 (untouched)
   cd /Users/didouye/Workspace/birdyphotobooth/capture && uv run pytest -q # 26 (untouched)
   ```

2. **Ruff clean** on server (only package modified):
   ```bash
   cd /Users/didouye/Workspace/birdyphotobooth/server && uv run ruff check .
   ```

3. **Manual end-to-end smoke test on the actual RPi (and the prod server):**
   - `git push origin main` (after merge)
   - Server auto-rebuilds (Docker workflow)
   - Check `/cameras` -- previously-rotated photos visible in `/gallery`
   - Click "Edit settings" on the camera card
   - Change `rotation` from current to a different value (e.g. 180 -> 0). The "Also rotate the N existing photo(s) by D° clockwise" row should appear.
   - Tick the checkbox (default checked) and submit.
   - The card swaps to `Update pending` (amber).
   - Within ~30s, the agent applies + capture restarts.
   - Heartbeat ack arrives -> server clears desired -> schedules BG task.
   - Card swaps to `↻ Reorienting N/M photos...` (sky-blue).
   - Once done, sticker disappears, `pending_reorient_delta` is null.
   - Open `/gallery?camera_id=X` -- old photos are now in the new orientation.
   - Open a previously-rotated photo's detail page -- the displayed dimensions match (swapped if 90/270).

4. **Watch for** during smoke testing:
   - Disk space: temporarily doubles during rotation (tmp file). At ~3 MB/photo and ~1k photos, ~3 GB peak. Verify enough free space before launching.
   - Lock contention: the BG task holds a `Session` for the duration of the loop. Other heartbeats / requests should still work (separate sessions). At ~1 photo/30s for capture and ~1 heartbeat/30s, no contention expected.
   - htmx polling on the card (every 5s) keeps `reorient_progress` updating live -- watch the counter increment.

---

## Out of scope

- Lossless `jpegtran` rotation (PIL re-encode q=95 is the v1 path; jpegtran can land later if quality loss is noticed).
- Cumulative rotation baseline tracking (operator handles repeated rotation changes manually -- if they screw up, second rotation fixes it).
- Persistent job queue (BackgroundTask is enough at current volumes; if photos go to ~10k, replace with Celery/RQ).
- Cancel-in-progress (job is started fire-and-forget; clicking Cancel update on the card after the BG task has started won't stop it. Acceptable for v1).
- Per-photo retry (failed photos are skipped, logged; operator can re-rotate by re-changing rotation later).
