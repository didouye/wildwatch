# WildWatch -- Initial design

Date: 2026-05-04

## Context

Automated wildlife camera trap. A Raspberry Pi fitted with an infrared camera
captures photos when motion is detected and uploads them to a web server.

## Hardware constraints

- RPi 2 v1.1 (no built-in WiFi, 1 GB RAM, quad-core 900 MHz)
- Camera Module 3 NoIR (12 MP, autofocus, no IR cut filter)
- DietPi OS
- USB WiFi dongle for connectivity
- V1 runs on wall power; battery considered later
- No PIR sensor for V1 (software detection)
- No IR illuminator for V1 (ambient light only)
- SSH access: `dietpi@dietpi.local`

## Technical decisions

| Decision                  | Choice                       | Reason                                                                                  |
|---------------------------|------------------------------|-----------------------------------------------------------------------------------------|
| RPi language              | Python                       | picamera2 is Python-only. Rust would barely save power since the hardware dominates.    |
| Server language           | Python (FastAPI)             | Single stack. SpeciesNet is Python-native.                                              |
| Dependency management     | uv                           | Fast, modern, becoming the Python standard.                                             |
| Motion detection          | Software (frame comparison)  | No PIR available; acceptable while V1 runs on wall power.                               |
| Database                  | SQLite                       | Lightweight, no DB server to manage. PostgreSQL migration possible later.               |
| Frontend                  | Jinja2 + htmx                | Lightweight, no JS build pipeline, server-rendered.                                     |
| Server deployment         | Docker Compose + Caddy       | Automatic HTTPS, easy to maintain.                                                      |
| Species identification    | SpeciesNet (V2+)             | Open source, 2000+ species, runs locally.                                               |

## Architecture

Two independent components, communicating one-way over HTTP (RPi -> server).

### wildwatch-capture (RPi)

- Monitoring loop using picamera2
- Low-resolution preview (640x480) for detection
- High-resolution capture (4608x2592) when motion is confirmed
- Burst of 3-5 photos per event
- Local queue with automatic retry
- TOML configuration
- systemd service

### wildwatch-server (VPS)

- FastAPI REST API (upload, CRUD, stats)
- File storage organized by date
- Automatic thumbnails (150 px, 400 px, 800 px)
- Web gallery (Jinja2 + htmx)
- API key auth (RPi) + login/password (web)
