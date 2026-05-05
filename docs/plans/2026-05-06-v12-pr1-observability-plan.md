# V1.2 PR 1 -- Observation (heartbeat read-only end-to-end)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a heartbeat from RPi to server every 30s carrying system metrics, capture status, and a low-res preview, and expose all of that in the existing `/cameras` page. **Read-only**: the response's `desired_config` is ignored at this stage; PR 2 will wire the apply path.

**Architecture:** Two systemd units on the RPi -- `wildwatch-capture` (unchanged behaviour) gains the side-effect of publishing its state in `/run/wildwatch/{status.json, preview.jpg}`; new `wildwatch-agent` reads those files + system probes, POSTs `/api/cameras/agent/heartbeat` (multipart). The server stores everything in a JSON blob `last_heartbeat` on the existing `cameras` row, writes the preview to `data/previews/{id}.jpg`, and surfaces it on `/cameras` with htmx polling.

**Tech stack:** FastAPI + SQLModel + Jinja2 + htmx (server), picamera2 + Pillow + httpx (capture/agent), `_recovery/setup.sh` (install).

**Companion design doc:** `docs/plans/2026-05-06-camera-control-plane-design.md`. Read it first; this plan is the build-order decomposition of its PR 1 section.

---

## Conventions

- TDD discipline: write the failing test, run it, see it fail with the expected message, then write the minimal code, run again, see green, commit.
- Commit after **each** passing test cycle. Small commits are cheap; a 30-line commit message that mixes unrelated concerns is expensive.
- Test commands assume the working directory is the package root (`server/` or `capture/` or `agent/`) with the venv pre-activated (`source .venv/bin/activate`) or invoked via `uv run pytest`.
- File paths are relative to the repo root unless prefixed with `~/` (RPi-side).

---

# Server tasks

## Task 1: Migration v12 -- add 4 nullable columns

**Files:**
- Modify: `server/src/wildwatch_server/migrations.py`
- Modify: `server/src/wildwatch_server/models.py:162-180` (Camera)
- Test: `server/tests/test_v12.py` (new)

**Step 1: Write the failing test**

Create `server/tests/test_v12.py`. Reuse the `_reload_app` helper pattern from `tests/test_v11.py` (lines 14-44). Import the new `upgrade_to_v12` function and exercise it twice on a v1.1 schema to prove idempotency.

```python
"""Tests for V1.2: schema migration + heartbeat infrastructure."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import SQLModel

from wildwatch_server import db as db_module


def _reload_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WILDWATCH_API_KEY", "admin-key")
    monkeypatch.setenv("WILDWATCH_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.setenv("WILDWATCH_DB_URL", f"sqlite:///{tmp_path / 'wildwatch.db'}")
    for var in ("ENROLL", "UPLOAD", "LOGIN", "DEFAULT"):
        monkeypatch.setenv(f"WILDWATCH_RATE_{var}", "1000/minute")
    db_module.reset_engine_cache()
    import wildwatch_server.rate_limit as rl
    importlib.reload(rl)
    rl.limiter.reset()
    import wildwatch_server.routes.cameras as cameras_routes
    import wildwatch_server.routes.photos as photos_routes
    importlib.reload(photos_routes)
    importlib.reload(cameras_routes)
    import wildwatch_server.main as main_module
    importlib.reload(main_module)
    SQLModel.metadata.create_all(db_module.get_engine())
    return main_module.app, db_module.get_engine()


def test_migration_v11_to_v12_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine = _reload_app(tmp_path, monkeypatch)

    # Drop the new columns so we simulate a pre-v12 schema.
    with engine.begin() as conn:
        for col in (
            "desired_config",
            "last_heartbeat",
            "agent_last_seen_at",
            "pending_reorient_delta",
        ):
            try:
                conn.execute(text(f"ALTER TABLE cameras DROP COLUMN {col}"))
            except Exception:
                pass

    from wildwatch_server.migrations import upgrade_to_v12

    result1 = upgrade_to_v12(engine)
    result2 = upgrade_to_v12(engine)

    assert result1["columns_added"] == 4
    assert result2["columns_added"] == 0  # second call is a no-op

    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(cameras)")).fetchall()}
    assert {"desired_config", "last_heartbeat", "agent_last_seen_at", "pending_reorient_delta"} <= cols
```

**Step 2: Run test to verify it fails**

```bash
cd server
uv run pytest tests/test_v12.py::test_migration_v11_to_v12_idempotent -v
```
Expected: `ImportError: cannot import name 'upgrade_to_v12' from 'wildwatch_server.migrations'`.

**Step 3: Add the migration function**

Append to `server/src/wildwatch_server/migrations.py`:

```python
def upgrade_to_v12(engine: Engine) -> dict[str, int]:
    """Idempotently bring the schema from V1.1 to V1.2 (camera control plane).

    Adds 4 nullable columns to the `cameras` table:
      desired_config, last_heartbeat (TEXT, JSON-serialized),
      agent_last_seen_at (TIMESTAMP), pending_reorient_delta (INTEGER).
    """
    actions = {"columns_added": 0, "tables_created": 0}
    if not _table_exists(engine, "cameras"):
        return actions

    cols = _existing_columns(engine, "cameras")
    spec = [
        ("desired_config", "TEXT"),
        ("last_heartbeat", "TEXT"),
        ("agent_last_seen_at", "TIMESTAMP"),
        ("pending_reorient_delta", "INTEGER"),
    ]
    with engine.begin() as conn:
        for name, sql_type in spec:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE cameras ADD COLUMN {name} {sql_type}"))
                actions["columns_added"] += 1

    if actions["columns_added"]:
        log.info("V1.2 migration applied: %s", actions)
    return actions
```

Wire it into `main()`:
```python
def main() -> None:
    ...
    init_db()
    upgrade_to_v05(get_engine())
    upgrade_to_v11(get_engine())
    upgrade_to_v12(get_engine())
```

Add `from wildwatch_server.migrations import upgrade_to_v05, upgrade_to_v11, upgrade_to_v12` to `db.py` (search where `init_db` calls `upgrade_to_v11`) and call `upgrade_to_v12(engine)` immediately after.

**Step 4: Add the SQLModel fields**

In `server/src/wildwatch_server/models.py` Camera class, add:

```python
# V1.2 -- camera control plane
desired_config: str | None = Field(default=None)            # JSON
last_heartbeat: str | None = Field(default=None)            # JSON
agent_last_seen_at: datetime | None = Field(default=None)
pending_reorient_delta: int | None = Field(default=None)
```

**Step 5: Verify green**

```bash
uv run pytest tests/test_v12.py::test_migration_v11_to_v12_idempotent -v
```
Expected: PASS.

Also run the full server suite to ensure nothing else broke:
```bash
uv run pytest -q
```
Expected: 84+ passed (the V1.1 baseline + 1 new).

**Step 6: Commit**

```bash
git add server/src/wildwatch_server/migrations.py \
        server/src/wildwatch_server/models.py \
        server/src/wildwatch_server/db.py \
        server/tests/test_v12.py
git commit -m "feat(v1.2): schema migration -- add 4 columns to cameras"
```

---

## Task 2: Heartbeat endpoint -- auth gates (401 / 403)

**Files:**
- Create: `server/src/wildwatch_server/routes/agent.py`
- Modify: `server/src/wildwatch_server/main.py` (mount the new router)
- Modify: `server/tests/test_v12.py`

**Step 1: Write 2 failing tests**

Append to `test_v12.py`:

```python
import json
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app, _ = _reload_app(tmp_path, monkeypatch)
    return TestClient(app)


def _enroll(client: TestClient, hostname: str = "rpi") -> dict:
    return client.post("/api/cameras/enroll", json={"hostname": hostname}).json()


def _approve(client: TestClient, camera_id: int) -> None:
    res = client.patch(
        f"/api/cameras/{camera_id}",
        headers={"Authorization": "Bearer admin-key"},
        json={"status": "approved"},
    )
    assert res.status_code == 200


def _heartbeat(client: TestClient, token: str, status: dict, preview: bytes | None = None):
    files: dict = {"status": (None, json.dumps(status))}
    if preview is not None:
        files["preview"] = ("preview.jpg", preview, "image/jpeg")
    return client.post(
        "/api/cameras/agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )


def test_heartbeat_requires_camera_token(client: TestClient) -> None:
    res = client.post("/api/cameras/agent/heartbeat", files={"status": (None, "{}")})
    assert res.status_code == 401


def test_heartbeat_returns_403_when_pending(client: TestClient) -> None:
    cam = _enroll(client)
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 403
```

**Step 2: Run, verify FAIL**

```bash
uv run pytest tests/test_v12.py -k heartbeat -v
```
Expected: 404 (route not mounted) or import error.

**Step 3: Create the router skeleton**

`server/src/wildwatch_server/routes/agent.py`:

```python
"""Camera agent heartbeat endpoint (V1.2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from sqlmodel import Session, select

from wildwatch_server.db import get_session
from wildwatch_server.models import Camera
from wildwatch_server.rate_limit import limiter

router = APIRouter(prefix="/api/cameras/agent")


def _camera_from_authz(session: Session, authorization: str | None) -> Camera:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    cam = session.exec(select(Camera).where(Camera.token == token)).first()
    if cam is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if cam.status != "approved":
        raise HTTPException(status_code=403, detail=f"Camera is {cam.status}")
    return cam


HEARTBEAT_LIMIT = "120/minute"


@router.post("/heartbeat")
@limiter.limit(HEARTBEAT_LIMIT)
def heartbeat(
    request: Request,
    response: Response,
    status: str = Form(...),
    preview: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict:
    cam = _camera_from_authz(session, authorization)
    return {"desired_config": None, "commands": []}
```

Mount it in `main.py` next to the other `app.include_router(...)` calls.

**Step 4: Verify green**

```bash
uv run pytest tests/test_v12.py -k heartbeat -v
```
Expected: 2 PASS.

**Step 5: Commit**

```bash
git add server/src/wildwatch_server/routes/agent.py server/src/wildwatch_server/main.py \
        server/tests/test_v12.py
git commit -m "feat(v1.2): heartbeat endpoint skeleton with auth"
```

---

## Task 3: Heartbeat persists `last_heartbeat` + `agent_last_seen_at`

**Files:**
- Modify: `server/src/wildwatch_server/routes/agent.py`
- Modify: `server/tests/test_v12.py`

**Step 1: Write the failing test**

Append:

```python
def test_heartbeat_stores_blob_and_timestamp(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 42},
        "system": {"cpu_temp_c": 45.0, "memory_avail_mb": 200, "memory_total_mb": 512,
                    "load_avg_1min": 0.3, "disk_avail_mb": 1024, "queue_size": 0},
        "capture": {"service_active": True, "status_age_s": 1, "preview_age_s": 2,
                     "last_capture_at": None, "last_detection_at": None,
                     "error_count": 0, "apply_error_observed": False},
        "reported_config": {"rotation": 0, "capture_width": 2304},
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200, res.text

    detail = client.get(
        "/api/cameras",
        headers={"Authorization": "Bearer admin-key"},
    ).json()[0]
    # Re-fetch via direct DB session for the new fields (they aren't in CameraRead yet).
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.agent_last_seen_at is not None
        stored = json.loads(row.last_heartbeat)
        assert stored["agent"]["version"] == "1.2.0"
        assert stored["reported_config"]["rotation"] == 0
```

**Step 2: Run, verify FAIL**

Expected: `assert row.agent_last_seen_at is not None` fails (None).

**Step 3: Wire persistence**

Replace the body of `heartbeat()`:

```python
    cam = _camera_from_authz(session, authorization)
    try:
        parsed = json.loads(status)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="status must be valid JSON") from exc

    cam.last_heartbeat = json.dumps(parsed, separators=(",", ":"))
    cam.agent_last_seen_at = datetime.now(timezone.utc)
    session.add(cam)
    session.commit()
    return {"desired_config": None, "commands": []}
```

**Step 4: Verify green** + **Step 5: Commit**

```bash
uv run pytest tests/test_v12.py -v
git add -A && git commit -m "feat(v1.2): heartbeat persists last_heartbeat blob + agent_last_seen_at"
```

---

## Task 4: Heartbeat writes preview JPEG to disk

**Files:**
- Modify: `server/src/wildwatch_server/storage.py`
- Modify: `server/src/wildwatch_server/routes/agent.py`
- Modify: `server/tests/test_v12.py`

**Step 1: Failing test**

```python
def test_heartbeat_stores_preview_to_disk(client: TestClient, tmp_path: Path) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100
    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}}, preview=fake_jpeg)
    assert res.status_code == 200, res.text

    from wildwatch_server.storage import previews_dir
    p = previews_dir() / f"{cam['id']}.jpg"
    assert p.exists() and p.read_bytes() == fake_jpeg
```

**Step 2: FAIL** -- `previews_dir` is undefined.

**Step 3: Add `previews_dir()` + write logic**

In `server/src/wildwatch_server/storage.py`:

```python
def previews_dir() -> Path:
    base = photos_dir().parent / "previews"
    base.mkdir(parents=True, exist_ok=True)
    return base
```

In `agent.py`, after the commit:

```python
    if preview is not None:
        from wildwatch_server.storage import previews_dir
        contents = await preview.read()
        if contents:
            target = previews_dir() / f"{cam.id}.jpg"
            target.write_bytes(contents)
```

Note: must change `def heartbeat` to `async def heartbeat` for `await preview.read()`.

**Step 4: Verify** + **Step 5: Commit**

```bash
uv run pytest tests/test_v12.py -v
git add -A && git commit -m "feat(v1.2): heartbeat stores preview JPEG on disk"
```

---

## Task 5: `GET /preview/{camera_id}` serves the JPEG

**Files:**
- Modify: `server/src/wildwatch_server/routes/ui.py` (add the route)
- Modify: `server/tests/test_v12.py`

**Step 1: Failing test**

```python
def test_get_preview_returns_jpeg(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    blob = b"\xff\xd8\xff\xe0jpegdata"
    _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}}, preview=blob)

    # No web session in tests yet; UI routes have web auth. Use the
    # WILDWATCH_DISABLE_WEB_AUTH escape hatch if it exists, otherwise
    # extend `_reload_app` to bypass it. (See auth_web.py for the env var.)
    res = client.get(f"/preview/{cam['id']}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == blob


def test_get_preview_returns_404_when_missing(client: TestClient) -> None:
    res = client.get("/preview/9999")
    assert res.status_code == 404
```

Check `server/src/wildwatch_server/auth_web.py` for the existing `require_web_session` bypass mechanism. If `tests/test_ui.py` has a working pattern for unauthenticated client (likely yes), reuse it.

**Step 2: FAIL** -- route not found.

**Step 3: Implement**

In `routes/ui.py`, alongside the existing routes:

```python
@router.get("/preview/{camera_id}", include_in_schema=False)
def preview(camera_id: int) -> FileResponse:
    from wildwatch_server.storage import previews_dir
    p = previews_dir() / f"{camera_id}.jpg"
    if not p.exists():
        raise HTTPException(status_code=404, detail="No preview yet")
    return FileResponse(
        p,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
```

**Step 4: Verify** + **Step 5: Commit**

```bash
uv run pytest tests/test_v12.py -v
git add -A && git commit -m "feat(v1.2): GET /preview/{id} serves the latest preview"
```

---

## Task 6: `GET /cameras/{id}/card` -- HTML fragment for htmx polling

**Files:**
- Create: `server/src/wildwatch_server/templates/_camera_card.html`
- Modify: `server/src/wildwatch_server/routes/ui.py`
- Modify: `server/src/wildwatch_server/templates/cameras.html`
- Modify: `server/tests/test_v12.py`

**Step 1: Failing test**

```python
def test_camera_card_renders_status(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 60},
        "system": {"cpu_temp_c": 47.3, "memory_avail_mb": 234, "memory_total_mb": 512,
                    "load_avg_1min": 0.5, "disk_avail_mb": 1024, "queue_size": 0},
        "capture": {"service_active": True, "status_age_s": 1, "preview_age_s": 2,
                     "last_capture_at": None, "last_detection_at": None,
                     "error_count": 0, "apply_error_observed": False},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get(f"/cameras/{cam['id']}/card")
    assert res.status_code == 200
    body = res.text
    # The fragment carries the running config + agent badge.
    assert "47.3" in body
    assert "Agent" in body
    assert "Capture" in body
    assert "rotation" in body and "0" in body  # current value
```

**Step 2: FAIL** -- 404.

**Step 3: Implement the route**

In `routes/ui.py`:

```python
def _camera_card_context(cam: Camera) -> dict:
    """Compute fields the card template needs (parses last_heartbeat)."""
    import json
    from datetime import datetime, timezone

    hb = json.loads(cam.last_heartbeat) if cam.last_heartbeat else {}
    now = datetime.now(timezone.utc)
    agent_seen = cam.agent_last_seen_at
    if agent_seen is not None and agent_seen.tzinfo is None:
        agent_seen = agent_seen.replace(tzinfo=timezone.utc)
    agent_age_s = (now - agent_seen).total_seconds() if agent_seen else None
    agent_online = agent_age_s is not None and agent_age_s < 60

    capture_block = hb.get("capture") or {}
    capture_online = (
        agent_online
        and bool(capture_block.get("service_active"))
        and (capture_block.get("status_age_s") or 999) < 30
    )

    return {
        "cam": cam,
        "hb": hb,
        "agent_online": agent_online,
        "agent_age_s": agent_age_s,
        "capture_online": capture_online,
    }


@router.get("/cameras/{camera_id}/card", response_class=HTMLResponse)
def camera_card(
    camera_id: int, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="_camera_card.html",
        context=_camera_card_context(cam),
    )
```

**Step 4: Build the fragment template** `templates/_camera_card.html`:

```html
<li
  id="camera-card-{{ cam.id }}"
  class="rounded-lg bg-slate-800 border border-slate-700 p-3 space-y-2"
  hx-get="/cameras/{{ cam.id }}/card"
  hx-trigger="every 5s"
  hx-swap="outerHTML"
>
  <div class="flex items-baseline justify-between gap-3">
    <p class="font-medium">{{ cam.display_name or cam.hostname }}</p>
    <div class="flex gap-2 text-xs">
      <span class="px-2 py-0.5 rounded {% if agent_online %}bg-emerald-700{% else %}bg-rose-700{% endif %}">
        Agent {{ "online" if agent_online else "offline" }}
      </span>
      <span class="px-2 py-0.5 rounded {% if capture_online %}bg-emerald-700{% elif agent_online %}bg-amber-700{% else %}bg-rose-700{% endif %}">
        Capture {{ "online" if capture_online else "offline" }}
      </span>
    </div>
  </div>

  <div class="flex gap-3">
    <img
      src="/preview/{{ cam.id }}{% if cam.agent_last_seen_at %}?v={{ cam.agent_last_seen_at.timestamp()|int }}{% endif %}"
      alt="Preview"
      class="w-40 h-30 object-cover rounded bg-slate-900 border border-slate-700"
      onerror="this.style.opacity=0.3;this.alt='no preview yet';"
    >
    <div class="text-xs text-slate-300 space-y-0.5 grow">
      {% if hb.system %}
        <div>CPU {{ hb.system.cpu_temp_c }}°C &middot;
             RAM {{ hb.system.memory_avail_mb }}/{{ hb.system.memory_total_mb }}MB &middot;
             Disk {{ (hb.system.disk_avail_mb / 1024)|round(1) }}GB free</div>
        <div>Queue {{ hb.system.queue_size }} &middot;
             Errors {{ hb.capture.error_count }} &middot;
             {% if hb.capture.last_capture_at %}Last capture {{ hb.capture.last_capture_at }}{% else %}No capture yet{% endif %}</div>
      {% else %}
        <div class="italic text-slate-500">Waiting for first heartbeat...</div>
      {% endif %}
      {% if hb.agent %}
        <div class="text-slate-500">Agent v{{ hb.agent.version }} &middot; uptime {{ hb.agent.uptime_s }}s</div>
      {% endif %}
    </div>
  </div>

  {% if hb.reported_config %}
    <p class="text-xs text-slate-400">
      Config (running):
      rotation={{ hb.reported_config.rotation }} &middot;
      capture={{ hb.reported_config.capture_width }}×{{ hb.reported_config.capture_height }} &middot;
      motion thr={{ hb.reported_config.area_threshold }}
    </p>
  {% endif %}

  <div class="flex gap-3 text-xs">
    <a href="/gallery?camera_id={{ cam.id }}" class="text-emerald-300 hover:text-emerald-200">See photos &rarr;</a>
    <form method="post" action="/cameras/{{ cam.id }}/revoke" onsubmit="return confirm('Revoke this camera?');">
      <button class="text-rose-400 hover:text-rose-300">Revoke</button>
    </form>
  </div>
</li>
```

**Step 5: Verify** + commit

```bash
uv run pytest tests/test_v12.py -v
git add -A && git commit -m "feat(v1.2): camera card fragment + htmx polling endpoint"
```

---

## Task 7: Wire the new card into the `/cameras` page

**Files:**
- Modify: `server/src/wildwatch_server/templates/cameras.html`
- Modify: `server/src/wildwatch_server/routes/ui.py:345-365` (cameras_page passes context)

**Step 1: Failing test**

In `test_v12.py`:

```python
def test_cameras_page_uses_card_fragment(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    res = client.get("/cameras")
    assert res.status_code == 200
    # The polling URL must be present (sanity that the new fragment was rendered).
    assert f'hx-get="/cameras/{cam["id"]}/card"' in res.text
```

**Step 2: FAIL** -- the existing `cameras.html` lists approved cameras inline.

**Step 3: Replace the approved-section loop with the fragment include**

In `templates/cameras.html` lines 53-99 (the `Approved` section), replace each `<li>...</li>` body with `{% include '_camera_card.html' %}` for an approved row -- but the include needs the context. Cleanest: pre-compute it server-side.

In `routes/ui.py` cameras_page, replace the build of `cams` with:

```python
    rows = session.exec(select(Camera).order_by(Camera.enrolled_at.desc())).all()
    pending = [{"cam": c, "photo_count": _photo_count(session, c)} for c in rows if c.status == "pending"]
    revoked = [{"cam": c, "photo_count": _photo_count(session, c)} for c in rows if c.status == "revoked"]
    approved = [_camera_card_context(c) | {"photo_count": _photo_count(session, c)} for c in rows if c.status == "approved"]
```

(Extract the existing `_camera_with_count` body into `_photo_count(session, cam) -> int`.)

In `cameras.html`, the approved loop becomes:

```html
<ul class="space-y-2">
  {% for ctx in approved %}
    {% with cam=ctx.cam, hb=ctx.hb, agent_online=ctx.agent_online,
            capture_online=ctx.capture_online %}
      {% include "_camera_card.html" %}
    {% endwith %}
  {% endfor %}
</ul>
```

(Jinja2 `with` block keeps the included template's variable interface clean.)

**Step 4: Verify** + commit.

```bash
uv run pytest tests/test_v12.py tests/test_ui.py -v
git add -A && git commit -m "feat(v1.2): /cameras page uses card fragment + live polling"
```

---

# Capture-side tasks (RPi)

These tasks add **side-effects** to `wildwatch-capture`: it publishes its current state to `/run/wildwatch/`. Behaviour (motion, capture, upload) is unchanged.

## Task 8: `state_publisher` module

**Files:**
- Create: `capture/src/wildwatch_capture/state_publisher.py`
- Create: `capture/tests/test_state_publisher.py`

**Step 1: Failing test**

```python
"""Tests for the state publisher (writes /run/wildwatch/{status.json,preview.jpg})."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from wildwatch_capture.state_publisher import StatePublisher


def test_status_json_atomic_write(tmp_path: Path) -> None:
    pub = StatePublisher(state_dir=tmp_path)
    snap = {
        "service_active": True,
        "current_config": {"rotation": 0, "capture_width": 2304},
        "last_capture_at": None, "last_detection_at": None,
        "error_count": 0, "uptime_s": 12,
    }
    pub.write_status(snap)
    out = tmp_path / "status.json"
    assert out.exists()
    assert json.loads(out.read_text()) == snap


def test_preview_writes_320x240_jpeg(tmp_path: Path) -> None:
    pub = StatePublisher(state_dir=tmp_path)
    # Simulate a 640x480 grayscale frame coming from camera.read_detection_frame().
    frame = (np.random.rand(480, 640) * 255).astype("uint8")
    pub.write_preview(frame)
    out = tmp_path / "preview.jpg"
    assert out.exists()
    # Soft assert: file is a JPEG (starts with SOI marker).
    assert out.read_bytes()[:2] == b"\xff\xd8"


def test_status_json_no_partial_file_on_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pub = StatePublisher(state_dir=tmp_path)
    pub.write_status({"a": 1})  # seed
    original = (tmp_path / "status.json").read_text()

    # Force a crash mid-write by patching os.replace.
    import os
    monkeypatch.setattr(os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated")))
    with pytest.raises(OSError):
        pub.write_status({"a": 2})

    # The original file should still be intact, no half-written content.
    assert (tmp_path / "status.json").read_text() == original
```

**Step 2: FAIL** -- module doesn't exist.

**Step 3: Implement**

`capture/src/wildwatch_capture/state_publisher.py`:

```python
"""Publish wildwatch-capture's current state to /run/wildwatch/.

The agent reads these files (status.json, preview.jpg) and forwards them
to the server. We never read the agent back -- this is one-way only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

DEFAULT_STATE_DIR = Path("/run/wildwatch")
PREVIEW_W, PREVIEW_H = 320, 240


class StatePublisher:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR) -> None:
        self._dir = state_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def write_status(self, snap: dict[str, Any]) -> None:
        """Atomic write of status.json (tmp + replace)."""
        target = self._dir / "status.json"
        tmp = self._dir / "status.json.tmp"
        tmp.write_text(json.dumps(snap, separators=(",", ":")))
        os.replace(tmp, target)

    def write_preview(self, frame: np.ndarray) -> None:
        """Encode a 2D grayscale frame as a 320x240 JPEG.

        `frame` is whatever `camera.read_detection_frame()` returns (Y plane,
        uint8). We resize with PIL and save as quality=70 -- this is a status
        preview, not the canonical capture.
        """
        target = self._dir / "preview.jpg"
        tmp = self._dir / "preview.jpg.tmp"
        img = Image.fromarray(frame).convert("L")
        img.thumbnail((PREVIEW_W, PREVIEW_H), Image.LANCZOS)
        img.save(tmp, "JPEG", quality=70, optimize=True)
        os.replace(tmp, target)
```

Add `Pillow>=10` to `capture/pyproject.toml` `dependencies` (Pillow is already present on the RPi via apt, but keep it in deps for the dev box test environment).

**Step 4: Verify** + commit

```bash
cd capture
uv run pytest tests/test_state_publisher.py -v
git add -A && git commit -m "feat(v1.2): capture state_publisher module"
```

---

## Task 9: Capture main loop publishes status + preview each cycle

**Files:**
- Modify: `capture/src/wildwatch_capture/main.py`

**Step 1: Failing test**

In `capture/tests/test_state_publisher.py` (or a new `test_main_publishes.py` if you prefer separation), add an integration-ish test that asserts `run()` calls the publisher once per loop. Since `run()` runs forever, factor out a single iteration.

The cleanest refactor: introduce `_loop_once(camera, detector, uploader, publisher, state)` and have `run()` call it in a `while True:`. Test `_loop_once` with mocks.

```python
from unittest.mock import MagicMock
import numpy as np


def test_loop_once_publishes_status_and_preview(tmp_path: Path) -> None:
    from wildwatch_capture import main as main_module

    publisher = MagicMock()
    publisher._dir = tmp_path

    camera = MagicMock()
    camera.read_detection_frame.return_value = np.zeros((480, 640), dtype="uint8")

    detector = MagicMock()
    detector.process.return_value = False  # no motion

    uploader = MagicMock()
    uploader.flush.return_value = 0

    state = main_module.LoopState()
    main_module._loop_once(camera, detector, uploader, publisher, state, config=...)

    publisher.write_status.assert_called_once()
    publisher.write_preview.assert_called_once()
```

You'll need a `LoopState` dataclass holding `last_capture_at`, `last_detection_at`, `error_count`, `started_at` etc. The `_loop_once` signature carries it by reference, mutates it.

**Step 2: FAIL** -- `_loop_once`, `LoopState` don't exist.

**Step 3: Refactor `run()` into `_loop_once` + `LoopState`**

```python
@dataclass
class LoopState:
    started_at_mono: float = field(default_factory=time.monotonic)
    last_cleanup_at: float = 0.0
    last_capture_at: datetime | None = None
    last_detection_at: datetime | None = None
    error_count: int = 0
    last_preview_at_mono: float = 0.0


def _build_status(state: LoopState, config: Config) -> dict:
    return {
        "service_active": True,
        "uptime_s": int(time.monotonic() - state.started_at_mono),
        "last_capture_at": state.last_capture_at.isoformat() if state.last_capture_at else None,
        "last_detection_at": state.last_detection_at.isoformat() if state.last_detection_at else None,
        "error_count": state.error_count,
        "current_config": {
            "rotation": config.camera.rotation,
            "capture_width": config.camera.capture_width,
            "capture_height": config.camera.capture_height,
            "detection_width": config.camera.detection_width,
            "detection_height": config.camera.detection_height,
            "pixel_threshold": config.motion.pixel_threshold,
            "area_threshold": config.motion.area_threshold,
            "background_alpha": config.motion.background_alpha,
            "warmup_frames": config.motion.warmup_frames,
            "cooldown_seconds": config.motion.cooldown_seconds,
            "burst_count": config.capture.burst_count,
            "burst_interval_seconds": config.capture.burst_interval_seconds,
        },
    }


PREVIEW_INTERVAL_S = 5.0


def _loop_once(camera, detector, uploader, publisher, state: LoopState, config: Config) -> None:
    frame = camera.read_detection_frame()
    triggered = detector.process(frame, now=time.monotonic())
    if triggered:
        state.last_detection_at = datetime.now(timezone.utc)
        score = detector.last_motion_score
        log.info("Motion detected (score=%.3f), capturing burst", score)
        captured = _capture_burst(camera, uploader, config, score)
        if captured:
            state.last_capture_at = datetime.now(timezone.utc)
        log.info("Burst done: %d photo(s) enqueued", captured)
    sent = uploader.flush()
    if sent:
        log.info("%d photo(s) uploaded to the server", sent)

    # State publishing -- always status, preview throttled.
    publisher.write_status(_build_status(state, config))
    now_mono = time.monotonic()
    if now_mono - state.last_preview_at_mono > PREVIEW_INTERVAL_S:
        try:
            publisher.write_preview(frame)
            state.last_preview_at_mono = now_mono
        except Exception:
            log.exception("Failed to write preview")

    # Cleanup unchanged.
    if now_mono - state.last_cleanup_at > CLEANUP_INTERVAL_SECONDS:
        deleted = uploader.cleanup_old_sent()
        if deleted:
            log.info("Cleanup: removed %d old photo(s) from sent/", deleted)
        state.last_cleanup_at = now_mono


def run(config: Config) -> None:
    detector = MotionDetector(config.motion)
    uploader = Uploader(config.upload)
    publisher = StatePublisher()
    state = LoopState()

    log.info("Starting camera")
    with Camera(config.camera) as camera:
        log.info("Monitoring loop started")
        while True:
            _loop_once(camera, detector, uploader, publisher, state, config)
```

**Step 4: Verify** + commit

```bash
uv run pytest tests/ -v
git add -A && git commit -m "feat(v1.2): capture publishes state each loop iteration"
```

---

# Agent (new package)

## Task 10: `agent/` package skeleton

**Files:**
- Create: `agent/pyproject.toml`
- Create: `agent/src/wildwatch_agent/__init__.py`
- Create: `agent/tests/__init__.py`
- Create: `agent/.python-version` (mirror capture/)

**Step 1: Bootstrap with `uv`**

```bash
mkdir -p agent/src/wildwatch_agent agent/tests agent/systemd
touch agent/src/wildwatch_agent/__init__.py agent/tests/__init__.py
```

`agent/pyproject.toml`:

```toml
[project]
name = "wildwatch-agent"
version = "0.1.0"
description = "WildWatch RPi control plane (heartbeat + remote config + future ops)"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27"]

[project.scripts]
wildwatch-agent = "wildwatch_agent.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/wildwatch_agent"]

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6", "respx>=0.21"]
```

**Step 2: Initialise the venv**

```bash
cd agent && uv sync
```

**Step 3: Smoke-test**

```bash
uv run python -c "import wildwatch_agent; print('ok')"
```
Expected: `ok`.

**Step 4: Commit**

```bash
git add agent/
git commit -m "feat(v1.2): agent package skeleton"
```

---

## Task 11: Agent `system_info` module

**Files:**
- Create: `agent/src/wildwatch_agent/system_info.py`
- Create: `agent/tests/test_system_info.py`

**Step 1: Failing test**

```python
"""Tests for agent system_info -- runs on macOS dev (no /sys, /proc) too."""

from __future__ import annotations

from wildwatch_agent import system_info


def test_snapshot_keys() -> None:
    snap = system_info.snapshot(queue_dir=None)
    # All present, may be None on macOS.
    expected = {"cpu_temp_c", "memory_avail_mb", "memory_total_mb", "load_avg_1min",
                "disk_avail_mb", "queue_size"}
    assert expected <= set(snap.keys())


def test_queue_size_counts_files(tmp_path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"y")
    (tmp_path / "c.json").write_bytes(b"{}")  # not a jpg
    snap = system_info.snapshot(queue_dir=tmp_path)
    assert snap["queue_size"] == 2
```

**Step 2: FAIL** -- module not found.

**Step 3: Implement**

`agent/src/wildwatch_agent/system_info.py`:

```python
"""System probes the agent attaches to each heartbeat."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def cpu_temp_c() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return round(int(raw) / 1000.0, 1)
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def memory_mb() -> tuple[float | None, float | None]:
    """Return (available_mb, total_mb)."""
    try:
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split()
            if len(value) >= 1 and value[0].isdigit():
                info[key] = int(value[0])
        avail = (info.get("MemAvailable") or info.get("MemFree"))
        total = info.get("MemTotal")
        return (
            round(avail / 1024.0, 1) if avail else None,
            round(total / 1024.0, 1) if total else None,
        )
    except (FileNotFoundError, PermissionError, ValueError):
        return None, None


def disk_avail_mb(path: str = "/") -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return round(usage.free / (1024 * 1024), 1)
    except OSError:
        return None


def load_avg_1min() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def queue_size(queue_dir: Path | None) -> int:
    if queue_dir is None or not queue_dir.exists():
        return 0
    return sum(1 for p in queue_dir.iterdir() if p.suffix.lower() == ".jpg")


def snapshot(queue_dir: Path | None) -> dict:
    avail, total = memory_mb()
    return {
        "cpu_temp_c": cpu_temp_c(),
        "memory_avail_mb": avail,
        "memory_total_mb": total,
        "load_avg_1min": load_avg_1min(),
        "disk_avail_mb": disk_avail_mb(),
        "queue_size": queue_size(queue_dir),
    }
```

**Step 4: Verify** + commit

```bash
uv run pytest tests/test_system_info.py -v
git add -A && git commit -m "feat(v1.2): agent system_info probes"
```

---

## Task 12: Agent `state_reader` module

**Files:**
- Create: `agent/src/wildwatch_agent/state_reader.py`
- Create: `agent/tests/test_state_reader.py`

**Step 1: Failing test**

```python
"""Reads /run/wildwatch/{status.json, preview.jpg}."""

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
    # Force mtime in the past.
    old = time.time() - 60
    import os; os.utime(p, (old, old))
    reader = StateReader(state_dir=tmp_path)
    blob, age = reader.read_preview(max_age_s=10)
    assert blob is None
    assert age is not None and age >= 60
```

**Step 2: FAIL.**

**Step 3: Implement**

`agent/src/wildwatch_agent/state_reader.py`:

```python
"""Reads the live state files published by wildwatch-capture."""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_STATE_DIR = Path("/run/wildwatch")


class StateReader:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR) -> None:
        self._dir = state_dir

    def _age_s(self, p: Path) -> float | None:
        try:
            return max(0.0, time.time() - p.stat().st_mtime)
        except FileNotFoundError:
            return None

    def read_status(self) -> tuple[dict | None, float | None]:
        p = self._dir / "status.json"
        age = self._age_s(p)
        if age is None:
            return None, None
        try:
            return json.loads(p.read_text()), age
        except (FileNotFoundError, json.JSONDecodeError):
            return None, age

    def read_preview(self, max_age_s: float) -> tuple[bytes | None, float | None]:
        p = self._dir / "preview.jpg"
        age = self._age_s(p)
        if age is None:
            return None, None
        if age > max_age_s:
            return None, age
        try:
            return p.read_bytes(), age
        except FileNotFoundError:
            return None, age
```

**Step 4: Verify** + commit.

---

## Task 13: Agent `heartbeat` module (build payload + POST)

**Files:**
- Create: `agent/src/wildwatch_agent/heartbeat.py`
- Create: `agent/tests/test_heartbeat.py`

**Step 1: Failing test**

```python
"""Build payload and POST it -- httpx is mocked via respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from wildwatch_agent.heartbeat import HeartbeatClient, build_payload


def test_build_payload_shape() -> None:
    sys_info = {"cpu_temp_c": 47.0, "memory_avail_mb": 234, "memory_total_mb": 512,
                "load_avg_1min": 0.5, "disk_avail_mb": 1024, "queue_size": 0}
    capture_status = {"service_active": True, "current_config": {"rotation": 0},
                      "last_capture_at": None, "last_detection_at": None,
                      "error_count": 0, "uptime_s": 60}
    payload = build_payload(
        agent_version="1.2.0", agent_uptime_s=120,
        system=sys_info,
        capture_status=capture_status, status_age_s=1.0, preview_age_s=2.0,
    )
    assert payload["agent"] == {"version": "1.2.0", "uptime_s": 120}
    assert payload["system"] == sys_info
    assert payload["reported_config"] == {"rotation": 0}
    assert payload["capture"]["service_active"] is True
    assert payload["capture"]["status_age_s"] == 1.0


@respx.mock
def test_post_heartbeat_with_preview(tmp_path) -> None:
    route = respx.post("https://example.test/api/cameras/agent/heartbeat").mock(
        return_value=httpx.Response(200, json={"desired_config": None, "commands": []})
    )
    client = HeartbeatClient(server_url="https://example.test", token="abc", timeout=5.0)
    res = client.send(payload={"agent": {"version": "1.2.0"}}, preview=b"\xff\xd8data")
    assert res == {"desired_config": None, "commands": []}
    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer abc"
    # multipart contains the JSON `status` field + `preview` file.
    body = request.content
    assert b'name="status"' in body
    assert b'name="preview"' in body


@respx.mock
def test_post_heartbeat_403_returns_none() -> None:
    respx.post("https://example.test/api/cameras/agent/heartbeat").mock(
        return_value=httpx.Response(403, json={"detail": "Camera is pending"})
    )
    client = HeartbeatClient(server_url="https://example.test", token="abc", timeout=5.0)
    res = client.send(payload={"agent": {}}, preview=None)
    assert res is None  # caller must back off
```

**Step 2: FAIL.**

**Step 3: Implement**

`agent/src/wildwatch_agent/heartbeat.py`:

```python
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
            r = httpx.post(self._url, files=files, headers=self._headers,
                           timeout=self._timeout, follow_redirects=True)
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
```

**Step 4: Verify** + commit.

---

## Task 14: Agent `main` loop

**Files:**
- Create: `agent/src/wildwatch_agent/main.py`
- Create: `agent/tests/test_main.py` (smoke only)

This task wires the parts. Heavily-mocked tests aren't worth it for a glue loop -- one smoke test that the module imports and the loop body is callable is enough.

**Step 1: Smoke test**

```python
"""Smoke test for agent main loop -- ensures imports + signatures."""

from __future__ import annotations

from pathlib import Path
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
```

**Step 2: FAIL.**

**Step 3: Implement**

`agent/src/wildwatch_agent/main.py`:

```python
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
from datetime import datetime, timezone
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
    *, agent_version: str, agent_started_at_mono: float,
    state_reader: StateReader, heartbeat_client: HeartbeatClient,
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
```

**Step 4: Verify** + commit.

---

## Task 15: systemd unit + sudoers + tmpfiles.d

**Files:**
- Create: `agent/systemd/wildwatch-agent.service`
- Create: `_recovery/wildwatch-tmpfiles.conf`
- Create: `_recovery/wildwatch-sudoers`
- Modify: `_recovery/setup.sh`

**No tests** -- these are config files. Verify by inspection + dry-run.

**Step 1: Create the service unit**

`agent/systemd/wildwatch-agent.service`:

```
[Unit]
Description=WildWatch RPi agent (heartbeat + config sync)
Documentation=https://github.com/didouye/wildwatch
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dietpi
Group=dietpi

WorkingDirectory=/home/dietpi/wildwatch-src/agent
ExecStart=/home/dietpi/wildwatch-src/agent/.venv/bin/wildwatch-agent --log-level INFO

Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=wildwatch-agent

# Hardening: keep NoNewPrivileges off because we sudo in PR2.
NoNewPrivileges=false
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/dietpi/wildwatch /run/wildwatch
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

**Step 2: tmpfiles.d**

`_recovery/wildwatch-tmpfiles.conf` (will be installed to `/etc/tmpfiles.d/wildwatch.conf`):

```
# /run/wildwatch/ shared between wildwatch-capture (writes) and wildwatch-agent (reads).
d /run/wildwatch 0775 dietpi dietpi -
```

**Step 3: sudoers (prepared for PR2 use, installed now)**

`_recovery/wildwatch-sudoers`:

```
# WildWatch agent privileges (V1.2 PR2 will use the restart command)
dietpi ALL=(root) NOPASSWD: /bin/systemctl restart wildwatch-capture, /bin/systemctl is-active wildwatch-capture
```

**Step 4: Update setup.sh**

In the section that installs `wildwatch-capture.service`, after copying it, add:

```bash
echo "==> Installing wildwatch-agent.service"
sudo cp "$DEST/agent/systemd/wildwatch-agent.service" /etc/systemd/system/

echo "==> Installing /etc/tmpfiles.d/wildwatch.conf"
sudo cp "$DEST/_recovery/wildwatch-tmpfiles.conf" /etc/tmpfiles.d/wildwatch.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/wildwatch.conf

echo "==> Installing /etc/sudoers.d/wildwatch"
sudo install -m 0440 -o root -g root \
    "$DEST/_recovery/wildwatch-sudoers" /etc/sudoers.d/wildwatch
sudo visudo -c -q  # validate

echo "==> Building agent venv"
(cd "$DEST/agent" && uv sync)

echo "==> Enabling + starting wildwatch-agent"
sudo systemctl daemon-reload
sudo systemctl enable --now wildwatch-agent
sudo systemctl restart wildwatch-capture  # capture now also writes /run/wildwatch
```

(Adjust to match the existing setup.sh idioms -- look at how `wildwatch-capture.service` is currently installed and mirror.)

**Step 5: Commit**

```bash
git add agent/systemd _recovery/wildwatch-tmpfiles.conf \
        _recovery/wildwatch-sudoers _recovery/setup.sh
git commit -m "feat(v1.2): install agent service, tmpfiles, sudoers"
```

---

## Task 16: `install_wildwatch.py` deploys both packages

**Files:**
- Modify: `_recovery/install_wildwatch.py`

The script currently clones the repo, builds the capture venv, and writes config. It must now also build the agent venv and install both systemd units.

**Step 1: Inspect** the existing script to understand its phases (`grep -n "capture" _recovery/install_wildwatch.py`).

**Step 2: Add an agent-deploy phase**

Find the section that does `subprocess.run(["uv", "sync"], cwd=capture_dir)` (or similar) and replicate for `agent_dir = root / "agent"`. Add a `systemctl daemon-reload` + enable for `wildwatch-agent.service` symmetric to the capture service.

**Step 3: Manual verification on a test RPi or in a Docker check-runner**

```bash
# On a test RPi reachable as dietpi.local:
ssh dietpi@dietpi.local "bash -s" < _recovery/setup.sh -- --server <server>
# Then verify both services are up:
ssh dietpi@dietpi.local "systemctl is-active wildwatch-capture wildwatch-agent"
# Expected: active\nactive
```

**Step 4: Commit**

```bash
git add _recovery/install_wildwatch.py
git commit -m "feat(v1.2): install_wildwatch.py deploys agent alongside capture"
```

---

# Final integration verification

After all 16 tasks:

1. **Server-side full test pass**
   ```bash
   cd server && uv run pytest -q
   ```
   Expected: 84 (V1.1 baseline) + ~10 V1.2 tests = ~94 passed, 0 failed.

2. **Capture-side full test pass**
   ```bash
   cd capture && uv run pytest -q
   ```
   Expected: existing tests + 3-4 new = green.

3. **Agent test pass**
   ```bash
   cd agent && uv run pytest -q
   ```
   Expected: ~6 tests, green.

4. **Lint**
   ```bash
   cd server && uv run ruff check .
   cd ../capture && uv run ruff check .
   cd ../agent && uv run ruff check .
   ```

5. **Manual smoke on the actual RPi**
   - `ssh dietpi@dietpi.local`
   - `systemctl status wildwatch-capture wildwatch-agent` -- both active
   - `ls /run/wildwatch/` -- shows status.json and preview.jpg, mtime within last 30s
   - `journalctl -u wildwatch-agent -f` -- shows a heartbeat every 30s with no errors
   - Open `https://wildwatch.didouye.com/cameras` in browser -- the camera card shows
     - Agent badge green
     - Capture badge green
     - Preview thumbnail (greyscale, 320x240)
     - CPU temp / RAM / Queue numbers updating every 5s (htmx polling)

6. **Update `ROADMAP.md`** -- add a `## V1.2 PR1 -- Heartbeat observability` section above `## Future (V2+)`, listing the user-visible changes.

7. **Open the PR**
   ```bash
   git push -u origin feat/v1.2-pr1-observability
   gh pr create --base main --title "V1.2 PR1: heartbeat observability" \
       --body "$(cat <<'EOF'
   ## Summary
   - New `wildwatch-agent` systemd service on the RPi pushes a heartbeat every 30s
   - `wildwatch-capture` publishes its state to `/run/wildwatch/{status.json,preview.jpg}`
   - Server stores heartbeat blob + writes preview to disk; `/cameras` page shows live status, metrics, preview thumbnail
   - Read-only: PR2 wires the apply path for `desired_config`

   ## Test plan
   - [ ] Server tests pass (`cd server && uv run pytest`)
   - [ ] Capture tests pass (`cd capture && uv run pytest`)
   - [ ] Agent tests pass (`cd agent && uv run pytest`)
   - [ ] Manual smoke on `dietpi.local`: both services active, /cameras shows live data

   Companion design: `docs/plans/2026-05-06-camera-control-plane-design.md`.
   EOF
   )"
   ```

---

## Out of scope for this PR (recap)

- Apply path (`desired_config` write to `config.toml` + `systemctl restart`) -- **PR 2**
- Edit modal for desired config -- **PR 2**
- Reorient existing photos -- **PR 3**
- App self-update / SSH tunnel / "take photo now" commands -- future
