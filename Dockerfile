# Production image for `checkpoint serve` — the dashboard + JSON API.
#
# Two-stage build:
#   1. node:20 builds the SPA bundle from checkpoint/dashboard/web/.
#      We intentionally rebuild from source rather than trust the committed
#      bundle so deploys can't accidentally ship stale UI.
#   2. python:3.12-slim installs the wheel + the freshly-built bundle
#      and runs uvicorn as a non-root user with a healthcheck.
#
# Build:    docker build -t checkpoint:latest .
# Run:      docker run -p 4001:4001 -v ck-runs:/data/runs -e CHECKPOINT_HOME=/data checkpoint:latest
# Compose:  docker compose up      (see docker-compose.yml)
# Cloud:    flyctl deploy          (see fly.toml)

# ---------- Stage 1: build the SPA ----------
# Node 22 to match the version CI builds the SPA with. Vite 8 (rolldown)
# requires ^20.19 || >=22.12, so an older major silently drifts from what is
# actually tested.
FROM node:22-alpine AS web-builder
WORKDIR /web
COPY checkpoint/dashboard/web/package.json checkpoint/dashboard/web/package-lock.json ./
RUN npm ci
COPY checkpoint/dashboard/web/ ./
RUN npm run build && ls -la ../static/

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# Build-time labels for image registries.
LABEL org.opencontainers.image.title="checkpoint"
LABEL org.opencontainers.image.description="Local + cloud agent-eval dashboard. Tests AI agents against stateful synthetic SaaS twins."
LABEL org.opencontainers.image.source="https://github.com/Aaditya2605/checkpoint"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# System deps. tini reaps zombies (uvicorn workers) cleanly. curl powers HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — never run a network-facing process as root.
ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} app \
    && useradd  --system --uid ${APP_UID} --gid app --home /app --create-home app

WORKDIR /app

# Install Python deps first (best layer cache hit rate on app code changes).
COPY pyproject.toml MANIFEST.in README.md ./
COPY checkpoint ./checkpoint

# Drop the source SPA + node_modules — only the built bundle should ship.
RUN rm -rf checkpoint/dashboard/web

# Inject the freshly-built SPA bundle from the web-builder stage.
COPY --from=web-builder /web/../static ./checkpoint/dashboard/static

# Editable install pulls in fastapi, uvicorn, click, etc. from pyproject.toml.
RUN pip install --no-cache-dir -e .

# Persistent data lives in /data — mount a volume here so runs + config
# survive container restarts. CHECKPOINT_HOME points the user-config there too.
RUN mkdir -p /data/runs /data/config /data/scenarios \
    && chown -R app:app /data /app

ENV PYTHONUNBUFFERED=1 \
    CHECKPOINT_HOME=/data/config \
    CHECKPOINT_LOG_LEVEL=INFO \
    PORT=4001

# Symlink the in-image runs cache to the volume so existing code paths
# (.checkpoint/cache/runs/*.json) write into persistent storage.
RUN mkdir -p /app/.checkpoint/cache \
    && ln -sf /data/runs /app/.checkpoint/cache/runs \
    && chown -R app:app /app/.checkpoint

USER app

EXPOSE 4001

# Healthcheck hits the FastAPI /healthz endpoint — load balancers can rely on it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

# tini = PID 1, then our entrypoint script.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python -m checkpoint.cli serve --host 0.0.0.0 --port ${PORT} --scenarios /data/scenarios"]
