# V1.2 PR 2 -- Pilotage (push desired_config + agent apply)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Operator changes a camera setting from `/cameras` (rotation, resolution, motion thresholds, burst). Server stores it as `desired_config`. Agent receives it on next heartbeat, writes `config.toml`, restarts `wildwatch-capture`. Capture comes back with the new `current_config`. Server reconciles, clears `desired_config`. UI shows `Update pending` then auto-clears.

**Architecture:**
- Operator submits a form → `POST /cameras/{id}/config` writes only the *changed* fields into `Camera.desired_config` (JSON blob).
- Heartbeat endpoint reads `Camera.desired_config` and returns it. When the agent's heartbeat carries `applied_at` *and* `reported_config == desired_config`, server clears `desired_config` (success). If `applied_at` is set but mismatched, server flags `apply_error_observed = true` in the stored heartbeat (UI shows red banner).
- Agent's `_tick` keeps a small state (`applied_desired_hash`, `last_apply_attempt_iso`). On a new desired (different hash), call `apply.py` which writes the TOML + `sudo systemctl restart wildwatch-capture`. On the next tick, send `applied_at`. When the server response stops carrying `desired_config`, agent resets state.

**Tech stack:** FastAPI + SQLModel + Jinja2 + htmx (server), httpx + tomllib + subprocess (agent), bash sudoers (already wired in PR1).

**Companion design:** `docs/plans/2026-05-06-camera-control-plane-design.md` -- read Section 3 (Heartbeat protocol reconciliation table), Section 4 (Apply procedure), Section 5 (UI / banners).

---

## Conventions

- TDD: failing test, run, confirm fail, implement, run, confirm pass, commit. Each task is one TDD cycle ending in a commit.
- Commit messages: `feat(v1.2): ...` or `fix(v1.2): ...`. Scope tag matches the rest of the V1.2 work.
- Test commands run from the package root (`server/`, `agent/`) with the existing `uv` venvs (already provisioned in PR1).
- The PR1 baseline: server tests = 102 passed, agent tests = 10 passed. After PR2: server ≈ 112, agent ≈ 14. Don't push.
- The schema migration was already applied in PR1's `upgrade_to_v12` (the `desired_config`, `last_heartbeat`, `agent_last_seen_at`, `pending_reorient_delta` columns are present). **No new migration in PR2.**

---

# Server tasks

## Task 1: Heartbeat returns `desired_config` + reconciliation

**Files:**
- Modify: `server/src/wildwatch_server/routes/agent.py`
- Modify: `server/tests/test_v12.py`

**Goal:** make the heartbeat endpoint actually do its end of the protocol:
- If `Camera.desired_config IS NULL` → return `{"desired_config": null, "commands": []}` (current behaviour).
- If `Camera.desired_config IS NOT NULL` → return `{"desired_config": <stored>, "commands": []}`.
- If the agent's heartbeat carries `applied_at` AND the desired was set:
  - If reported_config matches desired → **clear desired_config** (success).
  - If reported_config differs → flag `apply_error_observed: true` in the stored heartbeat (UI consumes this).

**Step 1: write 4 failing tests**

Append to `server/tests/test_v12.py`:

```python
def test_heartbeat_returns_desired_config_when_pending(client: TestClient) -> None:
    cam = _enroll(client)
    _approve(client, cam["id"])
    # Operator pre-sets a desired_config via the DB directly (POST /cameras/{id}/config
    # is implemented in Task 2 -- skip the route here).
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        row.desired_config = json.dumps({"rotation": 180})
        s.add(row)
        s.commit()

    res = _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"}})
    assert res.status_code == 200
    body = res.json()
    assert body["desired_config"] == {"rotation": 180}
    assert body["commands"] == []


def test_heartbeat_clears_desired_when_applied_and_reported_matches(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180, "capture_width": 2304})
        s.commit()

    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 5},
        "applied_at": "2026-05-06T00:00:00+00:00",
        "reported_config": {"rotation": 180, "capture_width": 2304},
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200
    assert res.json()["desired_config"] is None  # cleared on this very tick

    with SM(get_engine()) as s:
        assert s.get(Camera, cam["id"]).desired_config is None


def test_heartbeat_keeps_desired_and_flags_error_when_applied_but_mismatch(
    client: TestClient,
) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    payload = {
        "agent": {"version": "1.2.0", "uptime_s": 5},
        "applied_at": "2026-05-06T00:00:00+00:00",
        "reported_config": {"rotation": 0},  # capture is still on the OLD config
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.status_code == 200
    assert res.json()["desired_config"] == {"rotation": 180}  # still pending

    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        stored = json.loads(row.last_heartbeat)
        assert stored["capture"]["apply_error_observed"] is True


def test_heartbeat_no_applied_at_keeps_desired_no_error_flag(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0},
        # applied_at omitted -- agent hasn't applied yet (first time receiving desired)
    }
    res = _heartbeat(client, cam["token"], payload)
    assert res.json()["desired_config"] == {"rotation": 180}
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None  # still pending
        # Don't flag error: agent is still in the process of applying.
        stored = json.loads(row.last_heartbeat)
        assert stored.get("capture", {}).get("apply_error_observed", False) is False
```

**Step 2: run, confirm FAIL**

```bash
cd server
uv run pytest tests/test_v12.py -k "heartbeat_returns_desired or heartbeat_clears_desired or heartbeat_keeps_desired or heartbeat_no_applied_at" -v
```

Expected: 4 fails (response always returns `{"desired_config": None}` and reconciliation isn't implemented).

**Step 3: implement reconciliation**

Replace the body of `heartbeat(...)` in `agent.py`. The order matters:

```python
@router.post("/heartbeat")
@limiter.limit(HEARTBEAT_LIMIT)
async def heartbeat(
    request: Request,
    response: Response,
    status: str = Form(...),
    preview: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> dict:
    cam = _camera_from_authz(session, authorization)
    try:
        parsed = json.loads(status)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="status must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="status must be a JSON object")

    # --- Reconcile desired_config vs reported_config ---
    desired = (
        json.loads(cam.desired_config) if cam.desired_config else None
    )
    applied_at = parsed.get("applied_at")
    reported = parsed.get("reported_config") or {}

    apply_error_observed = False
    if desired is not None and applied_at:
        # Agent claims it applied. Did it actually take effect?
        if all(reported.get(k) == v for k, v in desired.items()):
            # Success: clear desired
            cam.desired_config = None
            desired = None
        else:
            apply_error_observed = True

    # Inject the apply error flag into the stored heartbeat so the UI can render it.
    parsed.setdefault("capture", {})
    parsed["capture"]["apply_error_observed"] = apply_error_observed

    cam.last_heartbeat = json.dumps(parsed, separators=(",", ":"))
    cam.agent_last_seen_at = datetime.now(timezone.utc)
    session.add(cam)
    session.commit()

    if preview is not None:
        contents = await preview.read()
        if contents:
            target = previews_dir() / f"{cam.id}.jpg"
            tmp = target.with_suffix(".jpg.tmp")
            tmp.write_bytes(contents)
            tmp.replace(target)
    return {"desired_config": desired, "commands": []}
```

Notes:
- The `all(reported.get(k) == v for k, v in desired.items())` checks **only the keys that were in desired**. Other keys (like `capture_height` if only `rotation` changed) don't matter.
- `parsed.setdefault("capture", {})` is defensive — agents that don't include the `capture` block still get the flag set.

**Step 4: confirm GREEN**

```bash
uv run pytest tests/test_v12.py -k "heartbeat_returns_desired or heartbeat_clears_desired or heartbeat_keeps_desired or heartbeat_no_applied_at" -v
uv run pytest -q  # full suite
uv run ruff check .
```

Expected: 4 new tests pass; full suite at 106 (102 + 4); ruff clean.

**Step 5: commit**

```bash
git add server/src/wildwatch_server/routes/agent.py server/tests/test_v12.py
git commit -m "feat(v1.2): heartbeat returns desired_config + reconciles on applied_at"
```

---

## Task 2: `POST /cameras/{id}/config` (set desired_config from form)

**Files:**
- Modify: `server/src/wildwatch_server/routes/ui.py`
- Modify: `server/tests/test_v12.py`

**Goal:** when the operator submits the edit settings form, write a *minimal* `desired_config` (only the fields that differ from `reported_config`) into the camera row. Returns the freshly-rendered card fragment so htmx can swap it in place.

**Step 1: write 3 failing tests**

```python
def test_post_camera_config_sets_minimal_diff(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    # Reported config: rotation=0, capture=2304x1296. Operator wants rotation=180, capture stays.
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
    )
    assert res.status_code == 200, res.text
    # Returns the card fragment
    assert "Update pending" in res.text
    assert "rotation" in res.text and "180" in res.text

    # DB: desired_config holds ONLY the changed field
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        row = s.get(Camera, cam["id"])
        assert row.desired_config is not None
        stored = json.loads(row.desired_config)
        assert stored == {"rotation": 180}  # capture_width unchanged → not in diff


def test_post_camera_config_no_diff_clears_pending(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 180, "capture_width": 2304, "capture_height": 1296},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "180", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
    )
    assert res.status_code == 200
    # No diff = no pending desired_config
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        assert s.get(Camera, cam["id"]).desired_config is None


def test_post_camera_config_validates_rotation(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    res = client.post(
        f"/cameras/{cam['id']}/config",
        data={"rotation": "45", "capture_width": "2304", "capture_height": "1296",
              "detection_width": "640", "detection_height": "480",
              "pixel_threshold": "25", "area_threshold": "0.02",
              "background_alpha": "0.05", "warmup_frames": "30",
              "cooldown_seconds": "5.0", "burst_count": "3",
              "burst_interval_seconds": "0.5"},
    )
    assert res.status_code == 400  # invalid rotation
    assert "rotation" in res.text.lower()
```

**Step 2: confirm FAIL** — route doesn't exist (404).

**Step 3: implement**

In `routes/ui.py`, add helpers + route:

```python
# Allowed fields and their casts (form values arrive as strings).
_CONFIG_FIELDS = {
    "rotation": int,
    "capture_width": int,
    "capture_height": int,
    "detection_width": int,
    "detection_height": int,
    "pixel_threshold": int,
    "area_threshold": float,
    "background_alpha": float,
    "warmup_frames": int,
    "cooldown_seconds": float,
    "burst_count": int,
    "burst_interval_seconds": float,
}


def _validate_config_form(form: dict) -> dict:
    """Cast form values to the right types and validate ranges."""
    parsed: dict = {}
    for name, caster in _CONFIG_FIELDS.items():
        raw = form.get(name)
        if raw is None or raw == "":
            raise HTTPException(status_code=400, detail=f"Missing field: {name}")
        try:
            parsed[name] = caster(raw)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid value for {name}: {raw}"
            ) from exc

    # Range checks
    if parsed["rotation"] not in {0, 90, 180, 270}:
        raise HTTPException(status_code=400, detail="rotation must be 0, 90, 180, or 270")
    if parsed["capture_width"] <= 0 or parsed["capture_height"] <= 0:
        raise HTTPException(status_code=400, detail="capture dimensions must be positive")
    if parsed["detection_width"] <= 0 or parsed["detection_height"] <= 0:
        raise HTTPException(status_code=400, detail="detection dimensions must be positive")
    if not (0 < parsed["area_threshold"] <= 1):
        raise HTTPException(status_code=400, detail="area_threshold must be in (0, 1]")
    if not (0 < parsed["background_alpha"] <= 1):
        raise HTTPException(status_code=400, detail="background_alpha must be in (0, 1]")
    if parsed["burst_count"] < 1:
        raise HTTPException(status_code=400, detail="burst_count must be >= 1")
    return parsed


def _diff_against_reported(submitted: dict, reported: dict | None) -> dict:
    """Return only the fields where submitted differs from reported.

    If `reported` is None or missing, all submitted fields are 'changed'."""
    if not reported:
        return submitted
    return {k: v for k, v in submitted.items() if reported.get(k) != v}


@router.post("/cameras/{camera_id}/config", response_class=HTMLResponse)
async def post_camera_config(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    form = await request.form()
    submitted = _validate_config_form(form)

    hb = json.loads(cam.last_heartbeat) if cam.last_heartbeat else {}
    reported = (hb.get("reported_config") or {}) if hb else {}
    diff = _diff_against_reported(submitted, reported)

    cam.desired_config = json.dumps(diff) if diff else None
    session.add(cam)
    session.commit()
    session.refresh(cam)

    # Return the freshly-rendered card fragment for htmx swap.
    return templates.TemplateResponse(
        request=request,
        name="_camera_card.html",
        context=_camera_card_context(cam, session),
    )
```

Add `import json` at the top of `ui.py` if not already.

**Step 4: confirm GREEN**

```bash
uv run pytest tests/test_v12.py -k "post_camera_config" -v
uv run pytest -q
uv run ruff check .
```

Expected: 3 new tests pass; full suite at 109; ruff clean.

**Step 5: commit**

```bash
git add server/src/wildwatch_server/routes/ui.py server/tests/test_v12.py
git commit -m "feat(v1.2): POST /cameras/{id}/config writes minimal desired_config diff"
```

---

## Task 3: `POST /cameras/{id}/config/cancel` (clear pending)

**Files:**
- Modify: `server/src/wildwatch_server/routes/ui.py`
- Modify: `server/tests/test_v12.py`

**Step 1: failing test**

```python
def test_post_cancel_clears_desired_config(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    res = client.post(f"/cameras/{cam['id']}/config/cancel")
    assert res.status_code == 200
    with SM(get_engine()) as s:
        assert s.get(Camera, cam["id"]).desired_config is None
```

**Step 2: confirm FAIL** (404).

**Step 3: implement**

```python
@router.post("/cameras/{camera_id}/config/cancel", response_class=HTMLResponse)
def cancel_camera_config(
    camera_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cam = session.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    cam.desired_config = None
    session.add(cam)
    session.commit()
    session.refresh(cam)
    return templates.TemplateResponse(
        request=request,
        name="_camera_card.html",
        context=_camera_card_context(cam, session),
    )
```

**Step 4: GREEN + commit**

```bash
uv run pytest -q  # 110 passed
uv run ruff check .
git add -A
git commit -m "feat(v1.2): POST /cameras/{id}/config/cancel clears pending desired"
```

---

## Task 4: Edit settings modal (form with all 10 fields)

**Files:**
- Create: `server/src/wildwatch_server/templates/_edit_settings_modal.html`
- Modify: `server/src/wildwatch_server/templates/cameras.html` (include the modal alongside the update modal, outside the polled card)
- Modify: `server/src/wildwatch_server/templates/_camera_card.html` (add an "Edit settings" button that opens the modal)
- Modify: `server/tests/test_v12.py`

**Step 1: failing test**

```python
def test_cameras_page_includes_edit_settings_modal(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
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
    assert res.status_code == 200
    assert f'id="edit-settings-modal-{cam["id"]}"' in res.text
    assert f'action="/cameras/{cam["id"]}/config"' in res.text
    # All 12 form fields rendered
    for field in ("rotation", "capture_width", "capture_height", "detection_width",
                  "detection_height", "pixel_threshold", "area_threshold",
                  "background_alpha", "warmup_frames", "cooldown_seconds",
                  "burst_count", "burst_interval_seconds"):
        assert f'name="{field}"' in res.text
    # Pre-filled with reported_config values
    assert 'value="0"' in res.text  # rotation
    assert 'value="2304"' in res.text  # capture_width


def test_camera_card_has_edit_settings_button(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    _heartbeat(client, cam["token"], {"agent": {"version": "1.2.0"},
                                       "reported_config": {"rotation": 0}})
    res = client.get(f"/cameras/{cam['id']}/card")
    assert "Edit settings" in res.text
    assert f'edit-settings-modal-{cam["id"]}' in res.text  # references modal id
```

**Step 2: FAIL** (modal template missing, button not in card).

**Step 3: implement**

Create `server/src/wildwatch_server/templates/_edit_settings_modal.html`:

```html
{# Edit settings modal -- per-camera, rendered from cameras.html outside the htmx-polled card. #}
{% set rc = ctx.hb.reported_config if ctx.hb and ctx.hb.reported_config else {} %}
{% set dc = ctx.cam.desired_config_dict if ctx.cam.desired_config_dict else {} %}
{% set effective = rc | default({}) %}
{% if dc %}
  {% set effective = dict(rc, **dc) %}
{% endif %}

<div
  id="edit-settings-modal-{{ ctx.cam.id }}"
  class="hidden fixed inset-0 z-50 bg-slate-900/80 backdrop-blur p-4 flex items-center justify-center"
  onclick="if(event.target === this) this.classList.add('hidden')"
>
  <div class="w-full max-w-2xl rounded-lg bg-slate-800 border border-slate-700 p-6 space-y-4 max-h-[90vh] overflow-y-auto">
    <header class="flex items-baseline justify-between">
      <h2 class="text-xl font-semibold">Edit settings -- {{ ctx.cam.display_name or ctx.cam.hostname }}</h2>
      <button
        type="button"
        onclick="document.getElementById('edit-settings-modal-{{ ctx.cam.id }}').classList.add('hidden')"
        class="text-slate-400 hover:text-slate-200 text-2xl leading-none"
      >&times;</button>
    </header>

    <p class="text-sm text-slate-400">
      Changes are pushed to the camera at its next heartbeat (within 30s).
      <code>wildwatch-capture</code> restarts to pick up the new config; expect
      a few seconds of downtime per setting change.
    </p>

    <form
      method="post"
      action="/cameras/{{ ctx.cam.id }}/config"
      hx-post="/cameras/{{ ctx.cam.id }}/config"
      hx-target="#camera-card-{{ ctx.cam.id }}"
      hx-swap="outerHTML"
      class="space-y-4 text-sm"
    >
      <fieldset class="space-y-2">
        <legend class="font-semibold text-slate-200">Orientation</legend>
        <label class="flex items-center gap-3">
          <span class="w-32">Rotation</span>
          <select name="rotation" class="bg-slate-900 border border-slate-700 rounded px-2 py-1">
            {% for r in [0, 90, 180, 270] %}
              <option value="{{ r }}" {% if effective.get("rotation", 0) == r %}selected{% endif %}>{{ r }}°</option>
            {% endfor %}
          </select>
        </label>
      </fieldset>

      <fieldset class="space-y-2">
        <legend class="font-semibold text-slate-200">Resolution</legend>
        {% for label, name, default in [
          ("Capture width", "capture_width", 2304),
          ("Capture height", "capture_height", 1296),
          ("Detection width", "detection_width", 640),
          ("Detection height", "detection_height", 480),
        ] %}
          <label class="flex items-center gap-3">
            <span class="w-32">{{ label }}</span>
            <input type="number" name="{{ name }}" min="1"
                   value="{{ effective.get(name, default) }}"
                   class="bg-slate-900 border border-slate-700 rounded px-2 py-1 w-32" required>
          </label>
        {% endfor %}
      </fieldset>

      <fieldset class="space-y-2">
        <legend class="font-semibold text-slate-200">Motion detection</legend>
        {% for label, name, default, step in [
          ("Pixel threshold", "pixel_threshold", 25, "1"),
          ("Area threshold", "area_threshold", 0.02, "0.001"),
          ("Background alpha", "background_alpha", 0.05, "0.01"),
          ("Warmup frames", "warmup_frames", 30, "1"),
          ("Cooldown (s)", "cooldown_seconds", 5.0, "0.1"),
        ] %}
          <label class="flex items-center gap-3">
            <span class="w-32">{{ label }}</span>
            <input type="number" name="{{ name }}" step="{{ step }}"
                   value="{{ effective.get(name, default) }}"
                   class="bg-slate-900 border border-slate-700 rounded px-2 py-1 w-32" required>
          </label>
        {% endfor %}
      </fieldset>

      <fieldset class="space-y-2">
        <legend class="font-semibold text-slate-200">Burst</legend>
        {% for label, name, default, step in [
          ("Photos per burst", "burst_count", 3, "1"),
          ("Interval (s)", "burst_interval_seconds", 0.5, "0.1"),
        ] %}
          <label class="flex items-center gap-3">
            <span class="w-32">{{ label }}</span>
            <input type="number" name="{{ name }}" step="{{ step }}"
                   value="{{ effective.get(name, default) }}"
                   class="bg-slate-900 border border-slate-700 rounded px-2 py-1 w-32" required>
          </label>
        {% endfor %}
      </fieldset>

      <div class="flex justify-end gap-2 pt-2">
        <button type="button"
                onclick="document.getElementById('edit-settings-modal-{{ ctx.cam.id }}').classList.add('hidden')"
                class="text-slate-400 hover:text-slate-200 px-3 py-1.5">Cancel</button>
        <button type="submit"
                onclick="document.getElementById('edit-settings-modal-{{ ctx.cam.id }}').classList.add('hidden')"
                class="rounded bg-emerald-600 hover:bg-emerald-500 px-3 py-1.5 font-medium">
          Apply
        </button>
      </div>
    </form>
  </div>
</div>
```

Note: this template uses `ctx.cam`, `ctx.hb`, etc., so the include needs `ctx` in scope (we'll handle that via `{% with %}` in `cameras.html`).

Helper on the model: add a `desired_config_dict` property to `Camera` (in `models.py`) so templates can access it without manual `json.loads`:

```python
# server/src/wildwatch_server/models.py, in Camera:

@property
def desired_config_dict(self) -> dict | None:
    if self.desired_config:
        import json
        return json.loads(self.desired_config)
    return None
```

In `cameras.html`, the existing block that loops modals (added in PR1.5) needs a sibling for edit settings. Add after the update-modal loop:

```html
{# Edit settings modals: one per approved camera, outside the polled card. #}
{% for ctx in approved %}
  {% include "_edit_settings_modal.html" %}
{% endfor %}
```

In `_camera_card.html`, add the "Edit settings" button to the action row (it currently has "See photos" + "Revoke"):

```html
<button
  type="button"
  onclick="document.getElementById('edit-settings-modal-{{ cam.id }}').classList.remove('hidden')"
  class="text-emerald-300 hover:text-emerald-200"
>Edit settings</button>
```

Place it near "See photos" / "Revoke" — match the existing row's layout.

**Step 4: GREEN + commit**

```bash
uv run pytest -q  # 112 passed
uv run ruff check .
git add -A
git commit -m "feat(v1.2): edit-settings modal with all 12 form fields"
```

---

## Task 5: "Update pending" + "Apply failed" banners on the card

**Files:**
- Modify: `server/src/wildwatch_server/templates/_camera_card.html`
- Modify: `server/src/wildwatch_server/routes/ui.py` (add `apply_error_observed` to `_camera_card_context`)
- Modify: `server/tests/test_v12.py`

**Step 1: failing tests**

```python
def test_card_shows_update_pending_banner_with_diff(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    payload = {
        "agent": {"version": "1.2.0"},
        "reported_config": {"rotation": 0, "capture_width": 2304},
    }
    _heartbeat(client, cam["token"], payload)
    # Set desired (just rotation differs)
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()

    res = client.get(f"/cameras/{cam['id']}/card")
    assert "Update pending" in res.text
    assert "rotation" in res.text
    assert "0" in res.text and "180" in res.text  # diff arrow
    assert f'/cameras/{cam["id"]}/config/cancel' in res.text  # cancel button


def test_card_shows_apply_error_banner(client: TestClient) -> None:
    cam = _enroll(client); _approve(client, cam["id"])
    # Set desired
    from wildwatch_server.db import get_engine
    from sqlmodel import Session as SM
    with SM(get_engine()) as s:
        s.get(Camera, cam["id"]).desired_config = json.dumps({"rotation": 180})
        s.commit()
    # Heartbeat with applied_at + reported NOT matching → flags apply_error_observed
    payload = {
        "agent": {"version": "1.2.0"},
        "applied_at": "2026-05-06T00:00:00+00:00",
        "reported_config": {"rotation": 0},
    }
    _heartbeat(client, cam["token"], payload)

    res = client.get(f"/cameras/{cam['id']}/card")
    assert "Last apply" in res.text and "fail" in res.text.lower()
    assert "Cancel update" in res.text or "/cancel" in res.text
```

**Step 2: FAIL** (no banners rendered).

**Step 3: implement**

Update `_camera_card_context` in `ui.py` to expose:

```python
hb = json.loads(cam.last_heartbeat) if cam.last_heartbeat else {}
desired = cam.desired_config_dict
apply_error = bool(hb.get("capture", {}).get("apply_error_observed"))
diff = None
if desired:
    reported = hb.get("reported_config") or {}
    diff = [
        {"field": k, "from": reported.get(k, "?"), "to": v}
        for k, v in desired.items()
    ]

return {
    ...
    "desired_config": desired,
    "config_diff": diff,
    "apply_error_observed": apply_error,
}
```

Update `_camera_card.html` -- add the banners between the metrics row and the action buttons:

```html
{% if config_diff %}
  {% if apply_error_observed %}
    <div class="rounded border border-rose-700 bg-rose-950/40 p-2 space-y-1 text-xs">
      <p class="text-rose-200">&times; Last apply attempt failed.</p>
      <p class="text-slate-400">
        The new config was written but capture didn't pick it up (likely an
        invalid value -- check the camera's logs).
      </p>
      <ul class="text-slate-300">
        {% for d in config_diff %}
          <li><code>{{ d.field }}</code>: {{ d.from }} &rarr; {{ d.to }}</li>
        {% endfor %}
      </ul>
      <form method="post" action="/cameras/{{ cam.id }}/config/cancel"
            hx-post="/cameras/{{ cam.id }}/config/cancel"
            hx-target="#camera-card-{{ cam.id }}" hx-swap="outerHTML">
        <button class="text-rose-200 hover:text-rose-100 underline">Cancel update</button>
      </form>
    </div>
  {% else %}
    <div class="rounded border border-amber-700 bg-amber-950/40 p-2 space-y-1 text-xs">
      <p class="text-amber-200">&uarr; Update pending -- waiting for agent.</p>
      <ul class="text-slate-300">
        {% for d in config_diff %}
          <li><code>{{ d.field }}</code>: {{ d.from }} &rarr; {{ d.to }}</li>
        {% endfor %}
      </ul>
      <form method="post" action="/cameras/{{ cam.id }}/config/cancel"
            hx-post="/cameras/{{ cam.id }}/config/cancel"
            hx-target="#camera-card-{{ cam.id }}" hx-swap="outerHTML">
        <button class="text-amber-200 hover:text-amber-100 underline">Cancel update</button>
      </form>
    </div>
  {% endif %}
{% endif %}
```

**Step 4: GREEN + commit**

```bash
uv run pytest -q  # 114 passed
uv run ruff check .
git add -A
git commit -m "feat(v1.2): update pending + apply error banners on camera card"
```

---

# Agent tasks

## Task 6: `apply.py` (write config.toml + restart capture)

**Files:**
- Create: `agent/src/wildwatch_agent/apply.py`
- Create: `agent/tests/test_apply.py`

**Goal:** apply a `desired_config` dict to the local `~/wildwatch/config.toml`, then `sudo systemctl restart wildwatch-capture`.

**Important:**
- **Never** touch the `[upload]` section. Even if the desired_config (somehow) has a key like `server_url`, ignore it. Only `[camera]`, `[motion]`, `[capture]` are writeable.
- Atomic write (`tmp + rename`).
- Subprocess for restart: `subprocess.run(["sudo", "/bin/systemctl", "restart", "wildwatch-capture"], check=True)`. Use the exact path the sudoers entry lists.

**Step 1: failing tests**

`agent/tests/test_apply.py`:

```python
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
```

**Step 2: confirm FAIL** (module doesn't exist).

**Step 3: implement `agent/src/wildwatch_agent/apply.py`**

```python
"""Apply a `desired_config` to ~/wildwatch/config.toml and restart capture.

The agent receives a `desired_config` dict from the heartbeat response, calls
`apply_desired_config(...)`, and on success memorises the apply timestamp so
the next heartbeat carries `applied_at`.

Only the `[camera]`, `[motion]`, `[capture]` sections of the TOML are
writeable. The `[upload]` section is never touched (it carries the camera
token + server URL). Unknown keys in the desired dict are silently dropped.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("wildwatch_agent.apply")

# Map each pushable field to the TOML section it lives in.
FIELD_TO_SECTION: dict[str, str] = {
    "capture_width": "camera",
    "capture_height": "camera",
    "detection_width": "camera",
    "detection_height": "camera",
    "rotation": "camera",
    "pixel_threshold": "motion",
    "area_threshold": "motion",
    "background_alpha": "motion",
    "warmup_frames": "motion",
    "cooldown_seconds": "motion",
    "burst_count": "capture",
    "burst_interval_seconds": "capture",
}


def _format_value(v: object) -> str:
    """Render a Python value as TOML scalar.

    Handles bool, int, float, str. For nested types we'd need tomlkit; YAGNI.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # Escape backslashes and double-quotes -- minimal but enough for filesystem paths.
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML value type: {type(v).__name__}")


def _merge_into_config_toml(config_path: Path, changes: dict) -> None:
    """Edit the file in-place: replace `key = ...` lines under their owning section.

    The strategy is a regex-per-line edit instead of a full TOML round-trip
    because we want to preserve the operator's comments + ordering. We only
    touch lines whose key is in FIELD_TO_SECTION, and only inside that
    field's expected section.
    """
    text = config_path.read_text()

    # Group changes by section.
    by_section: dict[str, dict] = {}
    for k, v in changes.items():
        section = FIELD_TO_SECTION.get(k)
        if section is None:
            log.warning("Ignoring unknown desired field: %s", k)
            continue
        by_section.setdefault(section, {})[k] = v

    if not by_section:
        return  # nothing to do

    # Walk the file line-by-line, tracking the current section header.
    current_section: str | None = None
    new_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^\s*\[([A-Za-z_][A-Za-z_0-9]*)\]\s*$", line)
        if m:
            current_section = m.group(1)
            new_lines.append(line)
            continue

        if current_section in by_section:
            # See if this line sets one of our target keys.
            key_match = re.match(r"^(\s*)([A-Za-z_][A-Za-z_0-9]*)(\s*=\s*).+$", line)
            if key_match:
                indent, key, eq = key_match.group(1), key_match.group(2), key_match.group(3)
                if key in by_section[current_section]:
                    new_value = _format_value(by_section[current_section].pop(key))
                    suffix = "\n" if line.endswith("\n") else ""
                    new_lines.append(f"{indent}{key}{eq}{new_value}{suffix}")
                    continue
        new_lines.append(line)

    # Any leftover changes mean the section was missing the key entirely; append.
    for section, leftovers in by_section.items():
        if not leftovers:
            continue
        if not new_lines or not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"[{section}]\n")
        for key, value in leftovers.items():
            new_lines.append(f"{key} = {_format_value(value)}\n")

    # Atomic write.
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text("".join(new_lines))
    os.replace(tmp, config_path)


SYSTEMCTL_BIN = "/bin/systemctl"


def apply_desired_config(desired: dict, config_path: Path) -> None:
    """Write the change to config.toml then restart wildwatch-capture.

    Raises on failure (subprocess error, file write error). The caller
    catches and decides whether to retry or skip.
    """
    log.info("Applying desired_config: %s", desired)
    _merge_into_config_toml(config_path, desired)
    log.info("Wrote config.toml; restarting wildwatch-capture")
    subprocess.run(
        ["sudo", SYSTEMCTL_BIN, "restart", "wildwatch-capture"],
        check=True,
    )
```

**Step 4: GREEN**

```bash
cd agent && uv run pytest -q
# 15 passed (10 existing + 5 new)
uv run ruff check .
```

**Step 5: commit**

```bash
git add agent/src/wildwatch_agent/apply.py agent/tests/test_apply.py
git commit -m "feat(v1.2): agent apply.py -- TOML merge + systemctl restart"
```

---

## Task 7: agent `_tick` integrates the apply state machine

**Files:**
- Modify: `agent/src/wildwatch_agent/main.py`
- Modify: `agent/tests/test_main.py`

**Goal:** the agent's tick loop now:
1. Sends `applied_at` in the heartbeat (from internal state).
2. On response with `desired_config`:
   - If `null`: clear local apply state.
   - If non-null: hash it. If different from `applied_desired_hash`, call `apply.apply_desired_config(...)`, record the hash + timestamp.
   - If same hash: don't re-apply (server hasn't ack'd yet).

**Step 1: failing tests**

```python
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
```

**Step 2: FAIL** (`ApplyState` doesn't exist; `_tick` doesn't take `apply_state`/`config_path`).

**Step 3: implement**

In `main.py`:

```python
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from wildwatch_agent import apply as apply_module


@dataclass
class ApplyState:
    applied_desired_hash: str | None = None
    last_apply_attempt_iso: str | None = None


def _hash_desired(desired: dict) -> str:
    canonical = json.dumps(desired, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
```

Update `run(...)` and `main()` to construct an `ApplyState` and a `config_path` and thread them through:

```python
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
```

Note: tests will need an `import json` in `test_main.py` if not already present.

**Step 4: GREEN + commit**

```bash
cd agent
uv run pytest -q  # 19 passed (15 + 4 new)
uv run ruff check .
git add -A
git commit -m "feat(v1.2): agent _tick applies desired_config + tracks apply state"
```

---

## Task 8: ROADMAP entry for PR2

**Files:**
- Modify: `ROADMAP.md`

Add a new block under the existing V1.2 section listing what shipped in PR2:

```markdown
### V1.2 PR 2: pilotage (push desired_config + agent apply)

- [x] Heartbeat endpoint returns `desired_config` from `Camera.desired_config`
      and reconciles based on `applied_at`: clears `desired_config` when
      `reported_config == desired_config`, flags `apply_error_observed=true`
      when applied but mismatched
- [x] `POST /cameras/{id}/config`: form-driven (12 fields), validates ranges
      (rotation in {0,90,180,270}, etc.), writes the *minimal diff* vs
      `reported_config` into `desired_config`. Returns the freshly-rendered
      card fragment for htmx swap
- [x] `POST /cameras/{id}/config/cancel`: clears `desired_config` (recovery
      from apply errors or operator changing their mind)
- [x] `_edit_settings_modal.html`: per-camera form grouped by section
      (Orientation / Resolution / Motion detection / Burst), pre-filled
      from reported_config (or desired_config if a change is pending)
- [x] Card banners: amber "Update pending" with diff arrow + "Cancel"
      button when `desired_config IS NOT NULL`; red "Last apply failed"
      when `apply_error_observed=true`
- [x] `agent/src/wildwatch_agent/apply.py`: in-place TOML merge (preserves
      comments + ordering of unrelated lines), atomic write, calls
      `sudo /bin/systemctl restart wildwatch-capture`. Never touches
      `[upload]` section
- [x] Agent `_tick` apply state machine: hash the desired, apply once
      per distinct desired, send `applied_at` until server ack's by
      clearing
- [x] Tests: server +X new, agent +Y new. Ruff clean.
```

Commit: `docs(v1.2): roadmap entry for PR2 config apply`.

---

## Final integration verification

After all 7 implementation tasks + ROADMAP:

1. **All test suites green:**
   ```bash
   cd /Users/didouye/Workspace/birdyphotobooth/server && uv run pytest -q   # ≥ 114
   cd /Users/didouye/Workspace/birdyphotobooth/capture && uv run pytest -q  # 26 (untouched)
   cd /Users/didouye/Workspace/birdyphotobooth/agent && uv run pytest -q    # ≥ 19
   ```

2. **Ruff clean** on all four packages:
   ```bash
   for d in server capture agent; do
     ( cd /Users/didouye/Workspace/birdyphotobooth/$d && uv run ruff check . )
   done
   ```

3. **Manual end-to-end smoke test on the actual RPi:**
   - Push the branch + merge to main locally OR push to a test branch
   - Update RPi via `update.sh`
   - Open `/cameras` in the browser
   - Click "Edit settings" on the camera card
   - Change `rotation` from 0 to 180, click Apply
   - Card should swap to show the amber "Update pending" banner
   - Wait ≤30s for the next heartbeat
   - Camera will receive desired_config and apply
   - On the next heartbeat after capture restarts, server clears `desired_config`
   - Card returns to nominal state, badge "Capture online" stays green
   - **The new photos arriving from this point should be in the new orientation.**
     (Old photos are NOT reoriented — that's PR3.)

4. **Watch out for** during smoke testing:
   - Sudoers must allow `/bin/systemctl restart wildwatch-capture` — already wired in PR1.
   - If apply silently fails (capture won't start with new config), the red banner should appear within ~60s. Operator clicks Cancel update.
   - The htmx polling (5s on the card) should NOT close any modal — modals are rendered outside the polled `<li>` (already fixed).

---

## Out of scope (PR3 or later)

- Reorient existing photos when rotation changes (`pending_reorient_delta` -- PR3).
- App self-update from the agent (a `commands` channel use case).
- "Take photo now" / live preview / SSH tunnel via the agent.
- Per-camera schedules.
- A "this config is invalid for your hardware" pre-check (e.g., 4K capture doesn't fit in 64MB CMA on RPi 2 v1.1) -- could be a v2 nicety; for v1 the operator gets the red banner if capture refuses to start.
