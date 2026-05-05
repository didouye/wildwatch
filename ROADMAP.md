# WildWatch roadmap

## V0.1 -- Setup and proof of concept

Goal: validate the hardware and the full end-to-end pipeline.

- [x] Initialize the Git repo and the monorepo layout
- [x] Configure the Camera Module 3 NoIR on DietPi (libcamera + dtoverlay imx708)
- [x] Add the `dietpi` user to the `video` and `render` groups
- [x] Verify the camera captures a photo (via picamera2 -- see note below)
- [x] Install Python 3.13 + uv 0.11 on the RPi
- [x] Write a minimal script that captures a photo and POSTs it to a test endpoint
- [x] Server side: minimal FastAPI endpoint that receives and stores a photo
- [x] Test the FastAPI server locally
- [x] Deploy the capture code on the RPi (clone + uv sync --system-site-packages)
- [x] Validate the full chain: RPi capture -> HTTP POST -> server stores

### V0.1 notes

**rpicam-apps v1.11.1 bug on RPi 2 v1.1 + Camera Module 3:** the CLI tools
`rpicam-still` and `rpicam-jpeg` exit 0 without producing a file (and
`rpicam-still --version` segfaults at exit after printing the version).
Capture via `picamera2` (Python) works perfectly. Our code uses picamera2
directly so this is not a blocker. Worth re-checking after future
`rpicam-apps` releases.

## V0.2 -- Motion detection

- [x] Adaptive background-subtraction detection (numpy + picamera2 lores YUV)
- [x] 640x480 preview + `switch_mode_and_capture_request` to 2304x1296 for
      stills (workaround for the 64 MB CMA limit on the RPi 2 v1.1)
- [x] Configurable trigger thresholds (pixel_threshold, area_threshold)
- [x] Cooldown between captures (anti-spam, 5s default)
- [x] Burst capture (3 photos by default, 0.5s spacing)
- [x] TOML configuration (`~/wildwatch/config.toml`, `/etc/wildwatch/` in V1.0)
- [x] TDD tests for the motion detector (9 tests green)
- [x] End-to-end validation on the RPi: detection -> burst -> HTTP upload

### V0.2 notes

**CMA capped at 64 MB on the RPi 2 v1.1:** the full 4608x2592 resolution is
out of reach because the V4L2 driver always allocates a minimum of 4 buffers
(4 x 36 MB > 64 MB). We stick to 2304x1296 (3 MP, ~36 MB) which fits
comfortably. Pushing CMA via `cma=256M` in `cmdline.txt` triggers a kernel
panic at boot on this platform. Worth exploring in V2+:
`dtoverlay=...,cma-size=...` in `config.txt` (safer because the bootloader
can fall back).

**DietPi blacklists `bcm2835_isp` and `bcm2835_codec` by default** through
`/etc/modprobe.d/dietpi-disable_rpi_camera.conf` and
`dietpi-disable_rpi_codec.conf`. Without those modules libcamera does not see
the camera. The `_recovery/setup_rpi.sh` script removes both files.

**Firmware variant:** with `gpu_mem_1024=16` (DietPi default) the firmware
uses `start_cd.elf` (cut-down), which has no camera support. We bump the
value to `gpu_mem_1024=96` to load the full `start.elf`.

## V0.3 -- Reliable upload and systemd service

- [x] Local queue at `~/wildwatch/queue/` plus a `dead/` folder for the
      permanent 4xx failures (auth, invalid payload)
- [x] Smart retry: 408/425/429/5xx + network errors stay in the queue,
      other 4xx go to dead-letter
- [x] Periodic cleanup of `~/wildwatch/sent/` (hourly, removes photos
      older than `sent_retention_days`)
- [x] Enriched JSON metadata per photo: motion_score, frame_index,
      burst_size, camera (resolution), sensor (model, exposure, gain, lux),
      system (hostname, cpu_temp, memory, load)
- [x] systemd service `wildwatch-capture.service` (Restart=on-failure,
      StartLimit, hardening), starts automatically at boot
- [x] API-key auth: `WILDWATCH_API_KEY` server side, `Authorization: Bearer
      <key>` client side, JSON sidecar persisted server side
- [x] 27 TDD tests green (20 capture + 7 server)

## V0.4a -- Server persistence and REST API

- [x] SQLite catalog (SQLModel + Alembic ready) with schema designed for a
      future PostgreSQL migration
- [x] Photos table: captured_at, received_at, file_path, file_size, hostname,
      motion_score, camera/sensor/system metadata, indexed on captured_at
      desc and hostname
- [x] Modular FastAPI app (routers split across health, photos, stats, admin)
- [x] `POST /api/photos` now writes the row to SQLite (no more JSON sidecars)
- [x] `GET /api/photos` paginated (limit/offset, max 200) with date range
      and hostname filters and `captured_at_desc|asc` ordering
- [x] `GET /api/photos/{id}`, `GET /api/photos/{id}/file`,
      `DELETE /api/photos/{id}`
- [x] `GET /api/stats` with `total`, `by_day`, `by_hostname`
- [x] `POST /api/admin/reindex` to ingest legacy V0.3 sidecars on disk
      (idempotent, falls back to file mtime when no sidecar)
- [x] 17 server tests green (auth, upload, list/filters/pagination, detail,
      download, delete, stats, reindex)

## V0.4b -- Web UI and thumbnails

- [x] Pillow thumbnail module: 3 sizes (150 / 400 / 800 px), EXIF-aware,
      idempotent, JPEG q=80
- [x] Async generation via FastAPI BackgroundTasks at upload time
- [x] Lazy fallback: GET /thumb/{size}/{id} regenerates synchronously when
      the cache file is missing
- [x] `POST /api/admin/regen_thumbnails` to rebuild missing thumbnails after
      a reindex
- [x] Server-rendered web UI (Jinja2 + htmx + Tailwind via CDN)
- [x] Routes: `/`, `/gallery`, `/photos/{id}`, `/photos/{id}/download`,
      `/thumb/{size}/{id}`
- [x] Gallery with paginated grid (24 photos/page) + htmx partial response
      for filter/pagination updates without a full page reload
- [x] Date range and hostname filters
- [x] Photo detail page with full metadata + prev/next navigation
- [x] 40 server tests green (17 API + 8 thumbnail + 15 UI/admin)

Web auth (login/password) and HTTPS termination are deferred to V1.0.

## V0.5 -- Sharing and management

- [x] Manual tags on photos (table-backed, case-insensitive, free-form names)
- [x] Favorites (boolean per photo, gallery filter, htmx star toggle)
- [x] Public share link (random per-photo `share_token`, revocable)
- [x] Public share view (`/share/{token}` template, hostname/system stats stripped)
- [x] Stats page with Chart.js (photos per day, per hour, per camera, per tag)
- [x] Bulk delete (UI selection mode + `POST /api/photos/bulk-delete`)
- [x] PATCH `/api/photos/{id}` for `is_favorite` + `tags`
- [x] `GET /api/tags` (with photo counts) + `DELETE /api/tags/{id}` (cascade unlinks)
- [x] In-place migration script `python -m wildwatch_server.migrations`
      that brings a V0.4 SQLite DB up to V0.5 idempotently
- [x] 57 server tests green (V0.4 carried over + 17 new V0.5 tests)

## V1.1 -- Multi-camera management

- [x] `cameras` table (token, hostname, display_name, status, timestamps,
      notes); `photos.camera_id` foreign key with `ON DELETE SET NULL`
- [x] Idempotent migration `upgrade_to_v11` (V1.0 SQLite -> V1.1)
- [x] Open enrollment with admin approval: `POST /api/cameras/enroll`
      (rate-limited 10/hour by default), `GET /api/cameras/me`,
      `PATCH /api/cameras/{id}`, `DELETE /api/cameras/{id}` (orphans
      photos via `camera_id=NULL`)
- [x] Dual-auth on `POST /api/photos`: camera token (RPi) or
      `WILDWATCH_API_KEY` (legacy/admin); pending/revoked -> 403
- [x] Gallery / list filter `?camera_id=N`; gallery dropdown driven by
      cameras
- [x] `/cameras` UI page with pending approvals, approved cameras
      (rename inline, see photos, revoke), revoked (re-approve or hard
      delete), and a modal with two enrollment one-liners (uv run /
      curl|bash)
- [x] Capture client treats 403 as transient (queue preserved while
      waiting for approval)
- [x] `_recovery/install_wildwatch.py --server URL` (auto-enroll instead
      of the V0.5 interactive flow)
- [x] `_recovery/setup.sh` -- single-line `curl|bash` installer that
      runs the system setup, enrolls the camera, writes config.toml,
      and starts the systemd service (with one-shot resume after the
      gpu_mem reboot)
- [x] 17 new server tests; 84 total green

## V1.0 -- Production deployment

- [x] Multi-stage `Dockerfile` (uv builder + slim runtime, non-root user,
      `/data` volume, healthcheck on `/health`)
- [x] `deploy/docker-compose.yml` pulling
      `ghcr.io/didouye/wildwatch-server:latest` (no local build needed)
- [x] Caddy reverse proxy with automatic Let's Encrypt HTTPS
      (`deploy/Caddyfile.example`)
- [x] `.github/workflows/docker-publish.yml` building and pushing the
      multi-arch image (`linux/amd64`, `linux/arm64`) to GHCR on every
      push to `main` and on every release/tag, with GitHub Actions cache
- [x] Web authentication: login form + signed session cookie, optional
      via env vars (`WILDWATCH_WEB_USER` + `WILDWATCH_WEB_PASSWORD_HASH`
      + `WILDWATCH_SESSION_SECRET`). `/share/{token}` stays public.
- [x] `python -m wildwatch_server.hash_password` CLI bundled in the image
- [x] API rate limiting via slowapi: `5/minute` on `POST /login`,
      `30/minute` on `POST /api/photos`, `200/minute` global default
- [x] RPi installer (already shipped in V0.5: `_recovery/install_wildwatch.py`)
- [x] Full deployment guide in `deploy/README.md` (prerequisites, secret
      generation, first-time setup, updates, backups, troubleshooting)
- [x] Server test suite covers V1.0: 67 tests (auth, rate limit included),
      ruff clean

---

## Future (V2+)

### Automatic species identification
- [ ] Integrate Google SpeciesNet on the server
- [ ] Run inference on every received photo
- [ ] Show the predicted species and confidence in the UI
- [ ] Filter photos by species

### Battery operation
- [ ] Add a PIR sensor (HC-SR501 or AM312) to wake the RPi
- [ ] Low-power sleep between detections
- [ ] Disable the WiFi dongle between uploads
- [ ] Measure and optimize power draw

### Night vision
- [ ] Add an IR illuminator
- [ ] Auto-switch day/night based on ambient light
- [ ] Tune camera parameters for night (ISO, exposure)

### Multi-camera
- [ ] Support multiple RPis pushing to a single server
- [ ] Identify each camera in the UI
- [ ] Multi-camera dashboard

### Alerts
- [ ] Real-time notifications (email, Telegram, webhook) on detection
- [ ] Per-species alert rules

### Video
- [ ] Capture short videos in addition to stills
- [ ] Live streaming (optional)
