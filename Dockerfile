# syntax=docker/dockerfile:1
#
# kgaf3-chat (Lite) — CPU FastAPI service on :5013.
#
# Multi-stage build (per docker_plan.md §2):
#   builder : uv installs the locked dependency set into a self-contained venv.
#   runtime : python:3.12-slim + tini(PID1) + curl(healthcheck), venv + app source.
#
# The app is run *from source* (`python run.py`, which imports the `app` package
# relative to WORKDIR /app). We therefore install only the locked dependencies
# (`--no-install-project`) — the project itself is never built as a wheel, so no
# README.md / build backend is required at image-build time. Target ~250-350 MB.

# ============================================================================
# Stage 1: builder — resolve & install dependencies into /opt/venv
# ============================================================================
FROM python:3.12-slim AS builder

# uv binary copied directly from the official image (no `pip install uv`).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Only the lockfile + manifest are needed to materialise the dependency venv.
# Copying these alone keeps this (expensive) layer cached across app-code edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ============================================================================
# Stage 2: runtime
# ============================================================================
FROM python:3.12-slim AS runtime

# tini : PID 1 reaper -> clean SIGTERM forwarding to uvicorn (graceful shutdown).
# curl : used by HEALTHCHECK (avoids Python cold-start cost per probe).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. UID/GID default to 1000 so bind-mounted host paths (owned by a
# uid=1000 host user) stay writable. Override at build time to match the host:
#   docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" app 2>/dev/null || true \
    && useradd -m -u "${UID}" -g "${GID}" -s /usr/sbin/nologin app 2>/dev/null || true \
    && mkdir -p /data /data/work /data/uploads \
    && chown -R "${UID}:${GID}" /data

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=5013

WORKDIR /app

# Dependency venv from the builder.
COPY --from=builder --chown=${UID}:${GID} /opt/venv /opt/venv

# Application source (run-from-source; no wheel install).
COPY --chown=${UID}:${GID} app /app/app
COPY --chown=${UID}:${GID} static /app/static
COPY --chown=${UID}:${GID} templates /app/templates
COPY --chown=${UID}:${GID} prompts /app/prompts
COPY --chown=${UID}:${GID} run.py /app/run.py

USER app
EXPOSE 5013

# Readiness probe: returns 503 until SQLite (the only hard dependency) is ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS --max-time 3 http://127.0.0.1:5013/health/ready || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
