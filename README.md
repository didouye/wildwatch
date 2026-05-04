# WildWatch

Automated wildlife camera trap, built around a Raspberry Pi and an infrared camera.

The system snaps photos whenever motion is detected and forwards them to a web
server for browsing, organizing, and species identification.

## Architecture

The project has two parts:

- **wildwatch-capture** (RPi): standalone capture client that detects motion and uploads the photos
- **wildwatch-server** (VPS): web server that receives, stores, and exposes the photos via a web UI

One-way communication: the RPi pushes photos to the server over HTTPS. The
server never reaches back into the RPi.

## Hardware

| Component   | Model                       | Notes                                                       |
|-------------|-----------------------------|-------------------------------------------------------------|
| SBC         | Raspberry Pi 2 v1.1         | Quad-core ARM Cortex-A7 @ 900 MHz, 1 GB RAM                 |
| OS          | DietPi (latest)             | Lightweight RPi-tuned distribution                          |
| Camera      | Camera Module 3 NoIR        | 12 MP, autofocus, no IR cut filter (night vision)           |
| Network     | USB WiFi dongle             | The RPi 2 has no built-in WiFi                              |
| Power       | Wall power (V1)             | Battery operation planned for a later release               |

### RPi access

```
ssh dietpi@dietpi.local
```

## Tech stack

| Component                    | Technologies                              |
|------------------------------|-------------------------------------------|
| Capture (RPi)                | Python, picamera2, libcamera, uv          |
| Server                       | Python, FastAPI, SQLite, Jinja2, htmx, uv |
| Server deployment            | Docker Compose, Caddy (HTTPS reverse proxy) |
| Species identification       | Google SpeciesNet (planned V2+)           |

## Project layout

```
wildwatch/
|- capture/                       # wildwatch-capture (RPi code)
|  |- pyproject.toml
|  |- config.toml.example
|  |- src/
|  |  |- wildwatch_capture/
|  |     |- __init__.py
|  |     |- main.py               # Entry point, main loop
|  |     |- camera.py             # picamera2 wrapper (preview + switch_mode)
|  |     |- motion.py             # Background-subtraction motion detector
|  |     |- uploader.py           # Local queue + HTTP upload
|  |     |- config.py             # TOML config loader
|  |- tests/
|  |- systemd/
|- server/                        # wildwatch-server (server code)
|  |- pyproject.toml
|  |- src/
|  |  |- wildwatch_server/
|  |     |- __init__.py
|  |     |- main.py               # FastAPI app
|  |- static/
|- docs/
|  |- SETUP-RPI.md                # Full install guide + gotchas/workarounds
|  |- plans/
|- _recovery/
|  |- install_wildwatch.py        # Python orchestrator (uv run)
|  |- setup_rpi.sh                # System-level setup on the RPi
|  |- install_systemd.sh          # systemd unit installation
|- README.md
|- ROADMAP.md
```

## Quick start

See **[docs/SETUP-RPI.md](docs/SETUP-RPI.md)** for the complete RPi install
procedure (DietPi flash, configuration, deployment) and the list of gotchas
encountered with their workarounds.

The fastest path is the orchestrator:

```bash
uv run _recovery/install_wildwatch.py
```

## How it works

### wildwatch-capture (RPi)

1. The camera runs in a low-resolution preview config (640x480 YUV420).
2. The luminance plane feeds an adaptive background-subtraction motion detector.
3. On confirmed motion, picamera2 switches to a still config and captures a
   burst at 2304x1296.
4. Photos are stored locally in `~/wildwatch/queue/`.
5. An uploader sends them to the server over HTTP POST and moves the
   acknowledged photos to `~/wildwatch/sent/`.
6. If WiFi drops, the queue persists on disk and uploads resume automatically.
7. All settings live in `~/wildwatch/config.toml`.

### wildwatch-server (VPS)

- Receives photos via the REST API (API-key authentication).
- Will generate thumbnails (V0.4: 150 px, 400 px, 800 px).
- Stores the photos on the filesystem, organized by date.
- Will expose (V0.4+) a web UI to browse, filter, tag, and share photos.

### API

```
POST   /api/photos           Upload photo + metadata
GET    /api/photos           List photos (pagination, filters)        -- V0.4
GET    /api/photos/{id}      Photo detail                              -- V0.4
GET    /api/photos/{id}/file Image download                            -- V0.4
PATCH  /api/photos/{id}      Update metadata (tags, species, favorite) -- V0.5
DELETE /api/photos/{id}      Delete                                    -- V0.4
GET    /api/stats            Statistics                                -- V0.5
```

## Security

- HTTPS between the RPi and the server (V1.0)
- API-key authentication (`Authorization: Bearer <token>`) (V0.3)
- Password-protected web UI (V0.4)
- Rate limiting on the upload endpoint (V1.0)

## License

TODO
