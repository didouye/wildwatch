# syntax=docker/dockerfile:1.7
# ----- Stage 1: builder ---------------------------------------------------
# We use the same WORKDIR in both stages so the venv shebangs (which embed
# absolute paths to the venv's python) keep working after the COPY.
FROM python:3.13-slim AS builder

RUN pip install --no-cache-dir 'uv>=0.11'

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# ----- Stage 2: runtime ---------------------------------------------------
FROM python:3.13-slim AS runtime

ARG APP_USER=wildwatch
ARG APP_UID=1000

RUN useradd --create-home --uid ${APP_UID} ${APP_USER} \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Same path as the builder stage so the venv binaries resolve correctly.
COPY --from=builder --chown=${APP_USER}:${APP_USER} /app/.venv /app/.venv
COPY --chown=${APP_USER}:${APP_USER} src/ /app/src/

# Photos and DB live under /data (mounted as a volume).
RUN mkdir -p /data/photos /data/thumbnails && chown -R ${APP_USER}:${APP_USER} /data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    WILDWATCH_PHOTOS_DIR=/data/photos \
    WILDWATCH_DB_URL=sqlite:////data/wildwatch.db

USER ${APP_USER}
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsSL http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "wildwatch_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
