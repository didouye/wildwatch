# V1.0 -- Production deployment

Date: 2026-05-04

## Goal

Take the WildWatch server from "single-user dev box" to "internet-exposed
production": Docker image published to GHCR, ready-to-run docker-compose
stack with Caddy + automatic Let's Encrypt HTTPS, web authentication
(login/password), API rate limiting, and a documented deploy procedure.

## Decisions

- **Web auth: login form + signed session cookie.** Single user in V1.0
  (env vars `WILDWATCH_WEB_USER` + `WILDWATCH_WEB_PASSWORD_HASH`).
  Bcrypt hashes via passlib. Cookie signed with itsdangerous, no
  server-side session store. Auth disabled when the password hash is
  unset (preserves the V0.5 LAN/dev experience).
- **Deploy target: VPS Linux + Docker + Caddy.** The compose stack only
  needs the operator to fill in `.env` and the `Caddyfile`. No build step
  on the target -- the image is pulled from GHCR.
- **Image distribution: GHCR.** A GitHub Actions workflow builds and
  pushes `ghcr.io/didouye/wildwatch-server:latest` (plus SHA and version
  tags) on every push to `main` and on every `v*` tag.
- **HTTPS: Caddy + automatic Let's Encrypt.** Caddyfile reads the domain
  from `${WILDWATCH_DOMAIN}` env var; ACME certs persist in a named
  Docker volume.
- **Rate limit: slowapi inside FastAPI.** `5/minute` on POST /login,
  `30/minute` on POST /api/photos, `200/minute` global default. Caddy
  does not enforce limits beyond connection counts.
- **Single user.** Multi-user accounts/registration are deferred (V2+).

## Web auth

### Module layout

```
server/src/wildwatch_server/
|- auth_web.py            # cookie session helpers + require_web_session dep
|- hash_password.py       # CLI: prompt for password, print bcrypt hash
|- routes/
|  |- auth.py             # GET/POST /login, POST /logout
|- templates/
|  |- login.html
```

### Configuration

| Env var                       | Purpose                                          |
|-------------------------------|--------------------------------------------------|
| `WILDWATCH_WEB_USER`          | Username (login form)                            |
| `WILDWATCH_WEB_PASSWORD_HASH` | Bcrypt hash. If unset, auth is disabled.         |
| `WILDWATCH_SESSION_SECRET`    | 32+ bytes, signs cookies. Required if auth on.   |

### Cookie

- Name: `wildwatch_session`
- Payload (signed): `{"u": "<username>", "exp": <unix_ts>}`
- Lifetime: 30 days, rolling refresh when older than half its life
- Flags: `HttpOnly`, `SameSite=Lax`, `Secure` (only when request is HTTPS)

### Routes

```
GET  /login              login page (anonymous)
POST /login              { username, password, next? } -> set cookie + redirect
POST /logout             clear cookie -> redirect /login
```

### Protection

`require_web_session` is added as a dependency on every UI router (gallery,
photo detail, stats, ui actions). When auth is disabled (env var unset),
the dependency is a no-op so the LAN dev mode keeps working unchanged.

`/share/{token}*` routes stay public on purpose.

`/api/*` routes keep their existing API-key gate.

### Hash CLI

```
$ python -m wildwatch_server.hash_password
Password: ********
Confirm:  ********
$2b$12$....   # paste this into WILDWATCH_WEB_PASSWORD_HASH
```

## Rate limiting (slowapi)

Slowapi is mounted as a FastAPI middleware. The IP of the client is read
from the standard `X-Forwarded-For` header that Caddy injects.

| Route                       | Limit          |
|-----------------------------|----------------|
| `POST /login`               | 5 / minute     |
| `POST /api/photos`          | 30 / minute    |
| Default for everything else | 200 / minute   |

Exceeding a bucket returns `429 Too Many Requests`.

## Docker stack

### Dockerfile (multi-stage)

```
# Stage 1 -- builder with uv + venv
FROM python:3.13-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Stage 2 -- runtime, no build tools
FROM python:3.13-slim
RUN useradd --create-home --uid 1000 wildwatch
WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/src /app/src
ENV PATH=/app/.venv/bin:$PATH
ENV PYTHONPATH=/app/src
USER wildwatch
EXPOSE 8000
CMD ["uvicorn", "wildwatch_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### docker-compose.yml (deploy/)

```yaml
services:
  server:
    image: ghcr.io/didouye/wildwatch-server:latest
    restart: unless-stopped
    environment:
      - WILDWATCH_API_KEY
      - WILDWATCH_WEB_USER
      - WILDWATCH_WEB_PASSWORD_HASH
      - WILDWATCH_SESSION_SECRET
      - WILDWATCH_PHOTOS_DIR=/data/photos
      - WILDWATCH_DB_URL=sqlite:////data/wildwatch.db
    volumes:
      - wildwatch_data:/data
    expose: ["8000"]

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    environment:
      - WILDWATCH_DOMAIN
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config

volumes:
  wildwatch_data:
  caddy_data:
  caddy_config:
```

### Caddyfile.example

```
{$WILDWATCH_DOMAIN} {
    reverse_proxy server:8000

    encode gzip zstd

    @uploads path /api/photos
    request_body @uploads {
        max_size 50MB
    }
}
```

### CI workflow (.github/workflows/docker-publish.yml)

Triggered on push to `main` and on tags matching `v*`. Builds the image
under `server/` and pushes to GHCR with tags `latest`, `sha-<short>`, and
the matching version tag. Uses GitHub Actions cache for fast incremental
builds.

## Deployment doc

`deploy/README.md` walks through:

1. VPS prerequisites (Docker, Docker Compose plugin, ports 80/443 open).
2. DNS pointing your domain to the VPS public IP.
3. Generating secrets:
   - `openssl rand -hex 32` for `WILDWATCH_API_KEY`
   - `openssl rand -hex 32` for `WILDWATCH_SESSION_SECRET`
   - `docker run --rm -it ghcr.io/didouye/wildwatch-server:latest python -m wildwatch_server.hash_password` for the password hash
4. Filling in `.env` and `Caddyfile` (replace `{$WILDWATCH_DOMAIN}` with
   the real domain).
5. `docker compose up -d`.
6. Verifying with `curl https://wildwatch.example.com/health`.
7. Updating: `docker compose pull && docker compose up -d`.
8. Backup: tar/copy the `wildwatch_data` volume (photos + sqlite DB).

## Tests (TDD)

Web auth:
- `test_login_form_renders`
- `test_login_with_valid_credentials_sets_cookie`
- `test_login_with_wrong_password_returns_error`
- `test_protected_route_redirects_when_no_cookie`
- `test_logout_clears_cookie`
- `test_share_view_works_without_auth`
- `test_no_web_auth_when_password_unset`

Rate limiting:
- `test_login_rate_limited_after_5_attempts`
- `test_upload_rate_limited_after_30_attempts` (parametrized with a smaller
  limit fixture to keep the test fast)

Total target: 57 (V0.5) + ~9 new = ~66 tests green.

## Out of scope

- Multi-user accounts, password reset, registration -- V2+.
- Cloud-managed Postgres -- V2+ (when the multi-RPi tier needs it).
- Per-camera API keys -- V2+ alongside the cameras table.
