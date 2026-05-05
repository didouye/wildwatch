# V1.2 -- Camera control plane (heartbeat agent + remote config + photo reorientation)

Date: 2026-05-06

## Goal

Today an enrolled RPi pushes photos and nothing else. The server has no
read on whether the device is alive between uploads, no way to change
its config, and no way to recover photos that were already uploaded with
a wrong orientation (e.g. camera mounted upside-down).

This iteration introduces a dedicated **agent** running alongside the
capture process. The agent:

- announces the RPi every 30s (heartbeat) with system metrics, capture
  status, and a low-res preview frame
- receives a `desired_config` from the server when the operator changes
  a setting in the web UI, applies it locally (rewrites `config.toml`,
  restarts `wildwatch-capture`), and acknowledges the change in the next
  heartbeat
- preserves the option to grow into self-update / SSH-tunnel / "take a
  photo now" without changing the protocol

A second feature rides on top of this: when the operator changes the
**rotation** of a camera, the UI offers to also rotate the photos that
were uploaded before the change.

## Decisions

- **Two systemd units on the RPi.** `wildwatch-capture` stays focused on
  motion + capture + upload. `wildwatch-agent` is the new control plane
  (heartbeat, config apply, future remote ops). Picamera2 is exclusive,
  so the agent never opens the camera; it observes capture via shared
  files in `/run/wildwatch/`.
- **File-based observation** (`P` in brainstorm). `wildwatch-capture`
  publishes `/run/wildwatch/preview.jpg` and `/run/wildwatch/status.json`
  every cycle. Agent reads them; mtime gives liveness for free; no IPC.
- **Single multipart heartbeat endpoint** (`H1`). One round-trip = full
  sync. Status JSON + optional preview JPEG go up; `desired_config` and
  a reserved `commands` array come down.
- **Config sync is operator-driven** (`F1`). `desired_config` only
  exists *during* a transition. NULL = in sync. The agent acks via
  `applied_at` + matching `reported_config`; server clears
  `desired_config` on success.
- **Push scope = capture-only knobs** (`C`). Rotation, capture/detection
  resolutions, motion thresholds, burst params. **No** OS hostname push,
  **no** OS reboot. "Apply" means `systemctl restart wildwatch-capture`.
- **Storage = JSON blobs, not typed columns**. `desired_config` and
  `last_heartbeat` are `TEXT` (JSON). Cardinality is small, no SQL filter
  needs them, schema evolves without ALTER.
- **Reorient existing photos is opt-in but checked by default** when a
  rotation change is submitted. Delta is rotation-on-disk → rotation-new,
  computed at submit time. No baseline tracking across multiple changes
  (operator's responsibility).

## Architecture

```
RPi                                                      Server
─────────────────────────────────────────────────────    ────────────────────
┌──────────────────────────┐  writes  ┌─────────────┐    POST  /api/cameras/
│ wildwatch-capture        │ ────────►│ /run/wild-  │    agent/heartbeat
│  - motion + capture      │          │  watch/     │    (multipart: status
│  - reads ~/wildwatch/    │  reads   │  status.json│    + preview.jpg)
│    config.toml           │ ◄─────── │  preview.jpg│ ──────────────────────►
└──────────────────────────┘          └─────────────┘
        ▲                                  ▲
        │ systemctl restart                │ reads
        │                                  │
┌──────────────────────────┐               │
│ wildwatch-agent          │ ──────────────┘
│  - heartbeat 30s         │
│  - apply desired_config  │ ──── writes ──► ~/wildwatch/config.toml
│  - sudo systemctl restart wildwatch-capture
└──────────────────────────┘
```

`wildwatch-capture` is unaware of the agent. It just publishes its
state. The agent is the only process that talks to the server (besides
the photo uploader inside capture, which is unchanged).

Auth: the agent reuses the existing `Camera.token` (Bearer). No new
credential.

## Schema diff

```sql
ALTER TABLE cameras ADD COLUMN desired_config            TEXT;        -- JSON, NULL = in sync
ALTER TABLE cameras ADD COLUMN last_heartbeat            TEXT;        -- JSON, overwritten each heartbeat
ALTER TABLE cameras ADD COLUMN agent_last_seen_at        TIMESTAMP;   -- distinct from last_seen_at (photo-driven)
ALTER TABLE cameras ADD COLUMN pending_reorient_delta    INTEGER;     -- 90 | 180 | 270, NULL when no job pending
```

`migrations.upgrade_to_v12(engine)` adds the four columns idempotently
(checked via `PRAGMA table_info`). Wired into `init_db`.

`last_heartbeat` shape:

```json
{
  "agent": { "version": "1.2.0", "uptime_s": 12345 },
  "system": {
    "cpu_temp_c": 45.2,
    "memory_avail_mb": 234, "memory_total_mb": 512,
    "load_avg_1min": 0.5,
    "disk_avail_mb": 1024,
    "queue_size": 5
  },
  "capture": {
    "service_active": true,
    "status_age_s": 1, "preview_age_s": 3,
    "last_capture_at": "2026-05-06T20:01:33Z",
    "last_detection_at": "2026-05-06T20:01:31Z",
    "error_count": 0,
    "apply_error_observed": false
  },
  "reported_config": { "rotation": 180, "capture_width": 2304, "capture_height": 1296,
                        "detection_width": 640, "detection_height": 480,
                        "pixel_threshold": 25, "area_threshold": 0.02,
                        "background_alpha": 0.05, "warmup_frames": 30,
                        "cooldown_seconds": 5.0,
                        "burst_count": 3, "burst_interval_seconds": 0.5 }
}
```

Preview JPEG is **not** in the DB. Stored on disk at
`data/previews/{camera_id}.jpg` (overwritten on each upload that
includes a preview), served via `GET /preview/{camera_id}`.

## API

### Agent-authenticated (Bearer = camera token)

```
POST /api/cameras/agent/heartbeat
     Content-Type: multipart/form-data
     fields:
       status   (string, required)  JSON status payload
       preview  (file,   optional)  JPEG, ~25-50 KB

     -> 200 { "desired_config": {...} | null, "commands": [] }
     -> 403 if camera is pending/revoked
     limit: 120/minute (4x nominal cadence)
```

Status payload extra field: `applied_at: ISO timestamp | null`. Sent
non-null in the heartbeat that immediately follows a config apply. Server
reconciliation:

| Server state               | `applied_at` | `reported == desired` | Action                                                   |
|----------------------------|--------------|-----------------------|----------------------------------------------------------|
| `desired_config` is NULL   | -            | -                     | normal heartbeat                                         |
| `desired_config` set       | null         | -                     | reply with `desired_config`, agent will apply            |
| `desired_config` set       | non-null     | true                  | apply succeeded → clear `desired_config`                 |
| `desired_config` set       | non-null     | false                 | apply failed → keep `desired_config`, set `apply_error_observed=true` for the UI |

### Web (web session)

```
GET  /cameras/{id}/card                 fragment HTML for htmx polling
POST /cameras/{id}/config               form data → set desired_config (+ optional reorient flag)
POST /cameras/{id}/config/cancel        clear desired_config (recovery from apply error)
GET  /preview/{camera_id}               serves data/previews/{id}.jpg
```

`commands` array on the heartbeat response stays empty in V1.2 but is
shipped from day one to avoid a future protocol change for `update_app`,
`take_photo_now`, `open_ssh_tunnel`, etc.

## Capture client changes

`wildwatch_capture` learns to publish state. No new config field, no
behavioural change in the motion / capture / upload path.

- After every motion-check cycle: write `/run/wildwatch/status.json`
  atomically (`tmp + os.rename`), with `current_config`,
  `last_capture_at`, `last_detection_at`, `error_count`, `uptime_s`.
- Every ~5s while idle, write `/run/wildwatch/preview.jpg`: 320x240
  grayscale JPEG produced from the existing Y-plane preview frame
  (no extra picamera2 mode switch, no extra CMA pressure).
- `/run/wildwatch/` is created by `tmpfiles.d` (added in setup.sh) so
  permissions are correct on boot before either service starts.

## Agent client (new package)

Layout `agent/` mirrors `capture/`:

```
agent/
  pyproject.toml
  systemd/wildwatch-agent.service
  src/wildwatch_agent/
    main.py            # loop
    heartbeat.py       # POST + multipart construction
    apply.py           # config.toml diff/merge + systemctl restart
    system_info.py     # cpu/mem/disk/queue
```

Reads existing `~/wildwatch/config.toml` `[upload]` section for
`server_url` and `api_key` (= camera token). Optional `[agent]` section
for non-default `heartbeat_interval_s` etc.

Apply procedure:

1. Load current `~/wildwatch/config.toml` (TOML)
2. Merge keys of `desired_config` into `[camera]`, `[motion]`, `[capture]`
   only -- `[upload]` is never touched
3. Atomic write (`tmp + rename`)
4. `subprocess.run(["sudo", "systemctl", "restart", "wildwatch-capture"])`
5. Memorise `last_apply_attempt = now()` for the next heartbeat
6. Return immediately to the heartbeat loop. The next pass reads the
   fresh `status.json` and reports the actually-running config.

Privileges: `_recovery/setup.sh` writes `/etc/sudoers.d/wildwatch`:

```
dietpi ALL=(root) NOPASSWD: /bin/systemctl restart wildwatch-capture, /bin/systemctl is-active wildwatch-capture
```

Two commands only, no shell, no wildcard.

## UI

Page `/cameras` enriches each *approved* camera card. (Pending and
revoked stay as today.)

```
┌─────────────────────────────────────────────────────────────────┐
│ "Birdy 1" (hostname: dietpi)        ● Agent  ● Capture          │
│  ┌──────────┐  CPU 47°C   RAM 234/512MB   Disk 2.1GB free       │
│  │ preview  │  Queue 0    Errors 0        Last capture 2s ago    │
│  │  320×240 │  Agent v1.2.0  uptime 3h                            │
│  └──────────┘                                                     │
│  Config (running): rotation=0  capture=2304×1296  motion thr=0.02│
│  [Edit settings]  [See photos]  [Revoke]                         │
└─────────────────────────────────────────────────────────────────┘
```

Badges:

- *Agent*: green if `now - agent_last_seen_at < 60s`, red otherwise
  with relative timestamp ("Last seen 3min ago")
- *Capture*: green if agent_online AND `service_active` AND
  `status_age_s < 30s`; amber if agent up but capture stale; red if
  agent down

`<img src="/preview/{id}?v={agent_last_seen_at_unix}">` -- the query
param invalidates the browser cache when a new preview lands.

`<div hx-get="/cameras/{id}/card" hx-trigger="every 5s" hx-swap="outerHTML">`
keeps the card live without a full reload.

### "Update pending" banner

When `desired_config IS NOT NULL`, the card shows an amber banner
between the config line and the action buttons:

```
⚠ Update pending  (waiting for agent)
   rotation:        0  →  180
   capture_width:   2304  →  1536    (only changed fields)
   [Cancel update]
```

If the latest heartbeat carries `apply_error_observed: true`, the
banner switches to red:

```
✗ Last apply attempt failed at 20:01:33
   The new config was written but capture didn't pick it up.
   [Retry restart]  [Cancel update]
```

### Edit modal

Form grouped by section, prefilled from `reported_config` (or
`desired_config` if one is pending so the operator edits the pending
state, not the running state):

- **Orientation**: `rotation` `<select>` 0 / 90 / 180 / 270 (primary)
- **Resolution**: `capture_width × capture_height`,
  `detection_width × detection_height`
- **Motion detection**: `pixel_threshold`, `area_threshold`,
  `background_alpha`, `warmup_frames`, `cooldown_seconds`
- **Burst**: `burst_count`, `burst_interval_seconds`

Submit → `POST /cameras/{id}/config`. Server stores only the *changed*
fields in `desired_config` (minimal diff for clearer banner).

## Historical photo reorientation

Activated by a checkbox in the edit modal that appears **only when
`rotation` actually changes**:

```
Also rotate the 1 234 existing photos of this camera by 180° clockwise
[✓] Yes, reorient existing photos
```

Photo count is fetched on modal open
(`<span hx-get="/cameras/{id}/photos/count">`). Delta computed JS-side:
`(new_rotation - reported_config.rotation) mod 360`. Checkbox is checked
by default.

Server flow:

1. `POST /cameras/{id}/config` with `reorient=true` → server sets both
   `desired_config` and `pending_reorient_delta` (the delta in degrees).
2. Agent applies the config; ack arrives.
3. Server clears `desired_config`. If `pending_reorient_delta IS NOT NULL`,
   a `BackgroundTask` is scheduled.
4. The job iterates photos with
   `camera_id = X AND captured_at <= ack_timestamp`:
   - `PIL.Image.open(p).rotate(-delta, expand=True).save(p, quality=95)`
   - Delete cached thumbs `thumbnails/{150,400,800}/{file_path}` (lazy
     regeneration on next view)
   - Swap `camera_width` / `camera_height` in DB if delta ∈ {90, 270}
5. On completion: clear `pending_reorient_delta`.

In-process progress map `reorient_progress[camera_id] = (done, total)`
exposed by `/cameras/{id}/card`. If the server restarts mid-job, the
counter disappears; photos already rotated stay rotated, the rest aren't.
At today's volume (~1k photos, ~30s of work) acceptable.

Lossless `jpegtran` rotation could land later as an optim. V1.2 ships
PIL re-encode q=95 -- works everywhere, simple.

## Tests

Server (~16 new):

- `test_heartbeat_requires_camera_token`
- `test_heartbeat_updates_agent_last_seen_at`
- `test_heartbeat_stores_last_heartbeat_blob`
- `test_heartbeat_writes_preview_to_disk`
- `test_heartbeat_returns_desired_config_when_pending`
- `test_heartbeat_clears_desired_config_when_applied_and_reported_matches`
- `test_heartbeat_keeps_desired_config_and_flags_error_when_applied_but_mismatch`
- `test_heartbeat_returns_403_when_revoked`
- `test_post_cameras_config_sets_desired_config_minimal_diff`
- `test_post_cameras_config_with_reorient_sets_pending_delta`
- `test_post_cameras_config_cancel_clears_desired_config`
- `test_get_preview_serves_jpeg`
- `test_card_fragment_renders_status_badges`
- `test_reorient_job_rotates_photos_and_swaps_dims_for_90`
- `test_reorient_job_skips_photos_uploaded_after_ack`
- `test_migration_v11_to_v12_idempotent`

Capture (~3 new):

- `test_capture_writes_status_json_each_cycle`
- `test_capture_writes_preview_jpg`
- `test_status_json_atomic_replace`

Agent (~6 new):

- `test_agent_collects_system_info`
- `test_agent_reads_status_when_fresh_and_skips_when_stale`
- `test_agent_applies_desired_config_writes_toml_and_restarts`
- `test_agent_skips_apply_when_desired_matches_current`
- `test_agent_handles_403_with_backoff`
- `test_agent_includes_applied_at_only_in_post_apply_heartbeat`

## Scripts

- `_recovery/setup.sh`: install second systemd unit
  `wildwatch-agent.service`, write `/etc/sudoers.d/wildwatch`, drop a
  `/etc/tmpfiles.d/wildwatch.conf` to create `/run/wildwatch/` on boot.
- `_recovery/install_wildwatch.py`: deploy the `agent/` package alongside
  `capture/` (same `uv sync` pattern), enable both services.

## Phasing (3 PRs + optional Phase 0)

### Phase 0 -- Quick fix (no PR)

SSH into `dietpi`, edit `~/wildwatch/config.toml` to `rotation = 180`,
`sudo systemctl restart wildwatch-capture`. Photos arrive right-side up
immediately while V1.2 is being built. Optional, one-shot.

### PR 1 -- Observation (read-only end-to-end)

- Capture writes `/run/wildwatch/{status.json, preview.jpg}`
- New `agent/` package + service, posts heartbeats. Apply path **not
  implemented** yet (response `desired_config` ignored).
- Migration `upgrade_to_v12` (all 4 columns, including
  `pending_reorient_delta` reserved for PR3)
- `POST /api/cameras/agent/heartbeat`, `GET /preview/{id}`
- `/cameras` page: badges, preview, metrics, reported config (read-only)
- htmx 5s polling on each card
- `_recovery/setup.sh` installs the agent unit + sudoers + tmpfiles.d

User-visible value: the cameras page becomes a live dashboard.

### PR 2 -- Pilotage (push desired_config)

- Edit modal (10 fields)
- `POST /cameras/{id}/config` + `POST /cameras/{id}/config/cancel`
- Agent diff / write config.toml / `sudo systemctl restart`
- `applied_at` echo + reconciliation logic
- "Update pending" + "Apply failed" banners

User-visible value: change rotation (and the rest) from the web UI.

### PR 3 -- Reorient historical photos

- Checkbox + photo count in modal (JS delta calc)
- `pending_reorient_delta` write path on POST
- BackgroundTask job on ack
- In-memory progress + UI indicator
- Cached thumb purge + camera_width/height swap

User-visible value: changing rotation cleans up the historical archive.

## Out of scope (explicitly deferred)

- App self-update from the agent (will be a `commands: ["update_app"]`
  payload later)
- SSH tunnel via the agent
- "Take photo now" command
- Live streaming (already roadmapped V2+)
- DB-backed job queue for the reorient task (BackgroundTask is enough at
  current volumes)
- Cumulative rotation baseline tracking (operator handles repeated
  rotation changes manually)
- OS hostname push, OS reboot, network reconfiguration -- agent never
  touches these
