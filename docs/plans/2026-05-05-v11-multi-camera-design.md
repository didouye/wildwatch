# V1.1 -- Multi-camera enrollment and management

Date: 2026-05-05

## Goal

Move WildWatch from single-RPi-with-shared-API-key to a fleet model. The
operator clicks "Add a camera" in the web UI, the new RPi self-enrolls, the
operator approves it, and from that point on the RPi uploads photos with
its own per-device token. The shared `WILDWATCH_API_KEY` stays around but
narrows to operator/admin operations only.

## Decisions

- **Open enrollment with admin approval.** Any RPi reachable to the server
  can `POST /api/cameras/enroll`. The row lands in `status='pending'`,
  uploads are refused, and the operator approves it from the UI. No
  pre-shared tokens to distribute.
- **Per-camera token = credential.** The enroll response returns
  `secrets.token_urlsafe(32)`. The RPi sends it in the
  `Authorization: Bearer <token>` header on every upload.
- **WILDWATCH_API_KEY scope shrinks** to admin endpoints (`/api/admin/*`,
  `/api/tags`, `/api/stats`). It is still accepted on `/api/photos`
  for testing purposes (uploads land with `camera_id=NULL`); the RPi
  never uses it.
- **Soft-revoke by default.** Revoking a camera flips status to `revoked`
  and stops accepting its uploads, but the existing photos stay. Hard
  delete (cascading photos to `camera_id=NULL`) is a separate explicit
  action.
- **Existing single-RPi keeps its install** -- the operator just runs the
  new "Add a camera" flow on it, which re-enrolls it cleanly. We do not
  ship an in-place migration script for the capture client.

## Schema diff

```sql
CREATE TABLE cameras (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    hostname     TEXT NOT NULL,
    display_name TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | revoked
    enrolled_at  TIMESTAMP NOT NULL,
    approved_at  TIMESTAMP,
    last_seen_at TIMESTAMP,
    notes        TEXT
);
CREATE INDEX idx_cameras_token  ON cameras(token);
CREATE INDEX idx_cameras_status ON cameras(status);

ALTER TABLE photos ADD COLUMN camera_id INTEGER REFERENCES cameras(id) ON DELETE SET NULL;
CREATE INDEX idx_photos_camera_id ON photos(camera_id);
```

`camera_id` stays nullable so legacy photos (uploaded with the old shared
key) and admin-key uploads continue to work. The existing `hostname`
column on `photos` stays for backward compat; new uploads also fill it
from `cameras.hostname` for fast filtering without a join.

`migrations.upgrade_to_v11(engine)` adds the table and the column
idempotently (checked via `PRAGMA table_info`). Wired into `init_db`.

## API

### Public (rate-limited)

```
POST /api/cameras/enroll
     body: { hostname: str, system: dict }
     -> 201 { id, token, status, hostname, display_name, enrolled_at }
     limit: 10/hour per IP
```

### Camera-authenticated (Bearer = camera token)

```
GET  /api/cameras/me           -> own row (status, display_name, ...)
                                  used by the capture client to detect
                                  approval after a 403.
```

### Admin (Bearer = WILDWATCH_API_KEY, or web session for the UI routes)

```
GET    /api/cameras                        list all (?status=...)
PATCH  /api/cameras/{id}                   { status, display_name, notes }
DELETE /api/cameras/{id}                   hard delete -> photos.camera_id = NULL
```

### Modified

```
POST /api/photos
  Auth: Bearer <camera_token>      -> photo.camera_id = camera.id
                                      requires status=approved
                                      403 otherwise
        Bearer <WILDWATCH_API_KEY> -> photo.camera_id = NULL (legacy/admin)
GET  /api/photos                   + ?camera_id=N filter
GET  /gallery                      + camera_id filter (UI)
```

## UI

A new `/cameras` route (web auth like the rest of the UI) with three
sections: pending approvals, approved cameras, and revoked cameras
(collapsed by default). The header gains a "Cameras" link.

`+ Add a camera` opens a modal (htmx swap) with two tabs:

- **From your computer:** copy/paste a `uv run` invocation that pulls
  `install_wildwatch.py` from the repo and passes `--server <domain>`.
- **From the RPi (SSH):** copy/paste a single
  `curl -fsSL .../setup.sh | bash -s -- --server <domain>` line.

Each pending row has Approve / Reject buttons (htmx). Each approved row
shows hostname, display name (editable), photo count, last seen time,
plus "See photos" (links to `/gallery?camera_id=N`) and "Revoke".

The gallery's hostname dropdown becomes a camera dropdown driven by
`cameras.display_name or cameras.hostname`. The photo detail panel
gains a "Camera" line.

## Capture client

- `wildwatch_capture/uploader.py`: when `POST /api/photos` returns 403
  ("Camera pending approval" or "Camera revoked"), the uploader:
  1. Polls `GET /api/cameras/me` once per `retry_interval_seconds`.
  2. Logs a single info message per status change (avoid log spam).
  3. Resumes uploads as soon as `status == "approved"`.
  Photos already in the queue are preserved untouched.
- `config.toml` `[upload].api_key` is reused as the camera token. The
  semantics widen but the field stays the same -- existing setups keep
  working.

## Scripts

- `_recovery/setup.sh`: new `curl | bash` entry point. Calls into the
  existing `setup_rpi.sh` system setup, then clones the repo, builds the
  venv, calls `POST /api/cameras/enroll` with `--server URL`, writes
  `~/wildwatch/config.toml` with the returned token, and starts the
  systemd service.
- `_recovery/install_wildwatch.py`: gain a `--server URL` flag. When
  provided, skip the interactive "is the server already deployed?"
  prompt and call the enrollment endpoint directly. Otherwise behave as
  in V0.5.

## Tests

Server (~12 new):
- `test_enroll_creates_pending_camera`
- `test_enroll_rate_limited_after_10`
- `test_enroll_returns_token_and_id`
- `test_upload_with_camera_token_works_when_approved`
- `test_upload_with_camera_token_returns_403_when_pending`
- `test_upload_with_camera_token_returns_403_when_revoked`
- `test_upload_with_admin_key_still_works`
- `test_get_me_returns_own_status`
- `test_patch_approve_sets_status_and_approved_at`
- `test_revoke_keeps_photos`
- `test_delete_camera_nulls_photo_camera_id`
- `test_gallery_filter_by_camera_id`
- `test_migration_v10_to_v11_idempotent`

Capture (1 new):
- `test_uploader_handles_403_pending` (with mocked httpx)

## Out of scope

- Per-camera schedules / quotas
- Push notifications when a camera goes silent
- Photo "owned by camera" privacy (any camera token can still only upload,
  not list other cameras' photos)
- Web auth multi-user (still single user in V1.1)
