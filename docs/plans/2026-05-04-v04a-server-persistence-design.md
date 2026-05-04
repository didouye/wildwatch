# V0.4a -- Server persistence and REST API

Date: 2026-05-04

## Goal

Move the server from filesystem-only storage (photo files + JSON sidecars on
disk) to a SQLite-backed catalog. Expose a full REST API for listing,
filtering, paginating, fetching, and deleting photos. Prepare the schema for
a future migration to PostgreSQL once the project starts handling multiple
RPi cameras.

This is the first half of V0.4. The thumbnails and the web UI are split out
into V0.4b.

## Decisions

- **SQLite is the source of truth** for V0.4a. The server stops writing
  `.meta.json` sidecars on upload. Existing sidecars produced by V0.3 stay
  on disk for archive/reindex purposes.
- **ORM: SQLModel + Alembic.** SQLModel ties Pydantic and SQLAlchemy
  together with the same ergonomics FastAPI already uses, and Alembic gives
  us a proper migration history -- both stay valid when we move to
  PostgreSQL later.
- **Auto-upgrade on boot** in V0.4a (calls `alembic upgrade head` from the
  app startup hook). We will switch to a manual deploy step in V1.0.
- **No `cameras` table yet.** We store the source `hostname` directly on
  each photo. A dedicated table will arrive in V0.5+ when we genuinely have
  multiple cameras to manage.
- **Auth stays as in V0.3** (single shared API key via `WILDWATCH_API_KEY`).
  Web auth (login/password) is V0.4b.

## Schema

```sql
CREATE TABLE photos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at     TIMESTAMP NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    file_path       TEXT NOT NULL UNIQUE,         -- relative to PHOTOS_DIR
    file_size       INTEGER NOT NULL,
    hostname        TEXT,
    motion_score    REAL,
    frame_index     INTEGER,
    burst_size      INTEGER,
    camera_width    INTEGER,
    camera_height   INTEGER,
    sensor_model    TEXT,
    exposure_time_us INTEGER,
    analogue_gain   REAL,
    lux             REAL,
    cpu_temp        REAL,
    memory_avail_mb REAL,
    load_avg_1min   REAL
);

CREATE INDEX idx_photos_captured_at ON photos(captured_at DESC);
CREATE INDEX idx_photos_hostname    ON photos(hostname);
```

The descending index on `captured_at` matches the dominant query (paginated
gallery sorted by capture date desc). All metadata fields are nullable so a
photo missing a sidecar (or partial metadata) still records cleanly.

## API

```
GET    /health                         public, unchanged
POST   /api/photos                     upload, modified to INSERT into DB
GET    /api/photos                     list paginated
       ?limit=50                         default 50, max 200
       ?offset=0
       ?from=2026-05-01                  inclusive lower date bound
       ?to=2026-05-04                    inclusive upper date bound
       ?hostname=DietPi                  filter by source RPi
       ?order=captured_at_desc|captured_at_asc
GET    /api/photos/{id}                photo detail
GET    /api/photos/{id}/file           binary download
DELETE /api/photos/{id}                remove DB row + file on disk
GET    /api/stats                      { total, by_day, by_hostname }
POST   /api/admin/reindex              scan filesystem and ingest sidecars
```

`POST /api/photos` keeps its existing upload contract (multipart with `file`
+ `captured_at` + `metadata`). The metadata JSON is parsed and persisted
into the corresponding columns instead of being written as a `.meta.json`
sidecar. The response now also includes the new SQLite `id`.

`POST /api/admin/reindex` is the migration tool from V0.3 to V0.4. It walks
`PHOTOS_DIR`, reads each `.jpg` plus its `.meta.json` if present, and does
`INSERT OR IGNORE` based on `UNIQUE(file_path)`. Photos without a sidecar
fall back to `captured_at = file mtime` and NULL metadata.

## File layout

```
server/
|- alembic.ini
|- alembic/
|  |- env.py
|  |- versions/
|     |- 0001_initial.py
|- pyproject.toml                # + sqlmodel, alembic
|- src/wildwatch_server/
|  |- main.py                    # app factory, mounts routers
|  |- db.py                      # engine + get_session()
|  |- models.py                  # SQLModel Photo
|  |- routes/
|  |  |- __init__.py
|  |  |- health.py
|  |  |- photos.py
|  |  |- admin.py
|  |- reindex.py                 # filesystem scan + ingest
|- tests/
|  |- test_api.py                # existing + new endpoints
|  |- test_reindex.py
```

## Tests (TDD)

- Upload: `test_upload_inserts_into_db` -- after POST, the row exists with
  the parsed metadata fields.
- List: `test_list_pagination`, `test_list_filter_by_date`,
  `test_list_filter_by_hostname`, `test_list_default_order_desc`.
- Detail: `test_get_photo_by_id` (200) and `test_get_photo_404`.
- Download: `test_download_streams_file`, `test_download_404` when row but
  no file.
- Delete: `test_delete_removes_db_row_and_file`, `test_delete_404`.
- Stats: `test_stats_groups_by_day_and_hostname`.
- Reindex: `test_reindex_ingests_existing_sidecars`,
  `test_reindex_is_idempotent`, `test_reindex_handles_missing_sidecar`.

Tests use the existing `client` fixture pattern (env-injected
`WILDWATCH_PHOTOS_DIR` + `WILDWATCH_API_KEY` and a tmp_path-scoped DB).

## Idempotency and migration story

- `UNIQUE(file_path)` keeps every photo single-rowed regardless of how many
  times reindex runs.
- `alembic upgrade head` is rerunnable -- the migrations are versioned and
  Alembic tracks the current state in `alembic_version`.
- A user upgrading from V0.3 will: pull the new code, restart the server
  (auto-creates the DB), and call `POST /api/admin/reindex` once to
  back-fill the catalog with all the photos already on disk.

## Out of scope (deferred to V0.4b)

- Thumbnail generation (150 / 400 / 800 px)
- Web UI (gallery, detail page, date filters)
- Web auth (login form, sessions)

## Out of scope (deferred to V0.5+)

- `cameras` table + per-camera API keys
- Tags, favorites, public share links, bulk operations
- Species predictions (V2+)
