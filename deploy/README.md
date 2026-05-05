# WildWatch -- Production deployment

This directory contains everything needed to run the WildWatch server in
production behind Caddy with automatic HTTPS, on a Linux VPS with Docker.

> **Deploying via Portainer instead?** Use
> [`docker-compose.portainer.yml`](docker-compose.portainer.yml) and inject
> the secrets through Portainer's "Environment variables" UI. Portainer's
> built-in compose runner cannot read host-mounted env files, so the
> Caddy + `wildwatch.env` setup below does not apply -- bring your own
> reverse proxy (Traefik, Nginx Proxy Manager, ...) for HTTPS.
>
> **Watch out for `$` escaping** when you paste the bcrypt hash into
> Portainer: every `$` must be doubled (e.g. `$2b$12$abc...` becomes
> `$$2b$$12$$abc...`). Portainer writes the values to a `.env` file that
> compose still substitutes. If you skip the doubling, your hash is
> truncated to `$2b$12` and login is rejected.

You only need three files on the host: `docker-compose.yml`, `Caddyfile`,
and `.env`. The image is pulled from GHCR -- no local build, no checkout
of the repo on the server.

## Prerequisites

- A Linux VPS with at least 1 vCPU and 1 GB RAM. Hetzner CX11, OVH VLE-2,
  DigitalOcean droplet 1 GB all work.
- Ports 80 and 443 reachable from the internet. Caddy needs both for the
  Let's Encrypt HTTP challenge and to serve traffic.
- A DNS record pointing your domain (e.g. `wildwatch.example.com`) at the
  VPS's public IP. Wait for propagation before continuing.
- Docker Engine + the Docker Compose plugin (`docker compose ...`):

  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER
  # log out / log back in for the group to apply
  ```

## First-time setup

### 1. Drop the three files on the host

```bash
mkdir -p /srv/wildwatch && cd /srv/wildwatch
curl -O https://raw.githubusercontent.com/didouye/wildwatch/main/deploy/docker-compose.yml
curl -O https://raw.githubusercontent.com/didouye/wildwatch/main/deploy/Caddyfile.example
curl -O https://raw.githubusercontent.com/didouye/wildwatch/main/deploy/wildwatch.env.example
mv Caddyfile.example Caddyfile
mv wildwatch.env.example wildwatch.env
chmod 600 wildwatch.env
```

### 2. Generate the secrets

```bash
# API key for the RPi capture client
openssl rand -hex 32   # → WILDWATCH_API_KEY

# Cookie session secret
openssl rand -hex 32   # → WILDWATCH_SESSION_SECRET

# Web auth: admin password hash (you will be prompted twice)
docker run --rm -it ghcr.io/didouye/wildwatch-server:latest \
    python -m wildwatch_server.hash_password
# → paste the resulting bcrypt string into WILDWATCH_WEB_PASSWORD_HASH
```

### 3. Edit `wildwatch.env`

```ini
WILDWATCH_DOMAIN=wildwatch.example.com
WILDWATCH_API_KEY=<from step 2>
WILDWATCH_WEB_USER=admin
WILDWATCH_WEB_PASSWORD_HASH=<from step 2>
WILDWATCH_SESSION_SECRET=<from step 2>
```

Paste the bcrypt hash verbatim, including all the `$` characters. The
compose file uses `format: raw` so no shell-escaping is needed.

The Caddyfile reads `${WILDWATCH_DOMAIN}` automatically -- no edit needed
unless you want to customize the headers or rate limits.

### 4. Boot the stack

```bash
docker compose up -d
docker compose logs -f
```

Caddy will request a Let's Encrypt certificate on the first HTTPS hit;
this can take up to a minute. Once it succeeds, the cert persists in the
`caddy_data` volume.

### 5. Smoke test

```bash
curl -fsS https://wildwatch.example.com/health
# {"status":"ok"}
```

Open `https://wildwatch.example.com/login` in a browser.

## Updates

Only the image moves; the compose file rarely changes.

```bash
cd /srv/wildwatch
docker compose pull
docker compose up -d
docker image prune -f       # reclaim old layers
```

If the schema changes (rare in V1.x), the server runs the in-place
migration on startup -- no extra step on the operator side.

## Backups

Photos and the SQLite database both live in the `wildwatch_data` named
volume. The simplest backup is a tarball of that volume:

```bash
docker run --rm \
  -v wildwatch_wildwatch_data:/data \
  -v "$PWD":/backup \
  alpine tar czf /backup/wildwatch-$(date +%F).tar.gz -C / data
```

For a hot backup of the SQLite file specifically (consistent even with
the server running), use `sqlite3 .backup`:

```bash
docker compose exec server sqlite3 /data/wildwatch.db ".backup /tmp/wildwatch.db"
docker compose cp server:/tmp/wildwatch.db ./wildwatch.db
```

Thumbnails live in the same volume but are reproducible -- the
`POST /api/admin/regen_thumbnails` endpoint rebuilds them from the source
photos if the cache is wiped.

## Troubleshooting

- **Cert not provisioned** -- check that DNS resolves to your VPS and
  ports 80 / 443 are open. `docker compose logs caddy` will show ACME
  errors verbatim.
- **502 from Caddy** -- the server container failed to start. Check
  `docker compose logs server`. Common cause: missing
  `WILDWATCH_SESSION_SECRET` while web auth is enabled.
- **Login always says "Invalid username or password"** -- the bcrypt
  hash got mangled on the way into the container. Confirm with
  `docker compose exec server printenv WILDWATCH_WEB_PASSWORD_HASH`.
  If the value is truncated at `$2b$12`, your env file is being
  interpolated. Make sure you are using `wildwatch.env` (not `.env`),
  the compose file declares `format: raw`, and your Docker Compose is
  2.24 or newer.
- **Login loops** -- the cookie is rejected because
  `WILDWATCH_SESSION_SECRET` changed between requests. Rotate the secret
  *only* when you are ready to log everyone out, then restart `server`.
- **Rate limit too tight for a busy RPi** -- override
  `WILDWATCH_RATE_UPLOAD` in `.env` (e.g. `60/minute`) and restart the
  server.

## Security checklist

- `wildwatch.env` is `chmod 600` and not in source control.
- `WILDWATCH_API_KEY` is at least 32 bytes of random entropy.
- `WILDWATCH_SESSION_SECRET` is unique per deployment.
- Web user password is high-entropy; the bcrypt cost in passlib defaults
  to 12 (~250 ms hash time on a modern CPU).
- HSTS is commented out in `Caddyfile.example`; enable it once HTTPS is
  proven to work, since it is hard to revert.
