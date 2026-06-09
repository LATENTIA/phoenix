# Phoenix container image.
#
# Goals:
#   * Stay slim (python:3.12-slim base).
#   * Cache-friendly: install deps before copying source so a code change
#     doesn't trigger a full pip reinstall.
#   * No secrets baked in. All runtime config flows in via env vars (see .env).
#   * Writable paths (data.db, downloaded/, logs/) are mounted in via volumes
#     so the container itself stays stateless.
#
# Build:   docker compose build
# Run:     docker compose up
# Logs:    docker compose logs -f
# Shell:   docker compose exec phoenix bash

FROM python:3.12-slim AS runtime

# Run as a non-root user inside the container. Even though the app is gated by
# basic auth + CSRF, defence in depth: if the Flask process ever gets RCE, the
# attacker doesn't land as root.
RUN useradd --create-home --shell /bin/bash --uid 1000 phoenix

WORKDIR /app

# System deps. python:3.12-slim is Debian-based and already has what pandas /
# requests / flask need at runtime (they all ship wheels for linux x86_64).
# We add ca-certificates explicitly because Phoenix calls IBKR over HTTPS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps FIRST so source-only edits don't bust this cache layer.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy ONLY the application source. The src/ layout keeps host-side
# operational files (Dockerfile, docker-compose.yml, .env*, docs/, scripts/,
# phoenix-data/) out of the image entirely. Smaller image, cleaner runtime
# filesystem, and the in-container layout becomes:
#   /app/app.py
#   /app/core/...
#   /app/reports/...
#   /app/templates/...
#   /app/static/...
COPY src /app

# Everything writable lives under /app/data, which docker-compose bind-mounts
# to a stable host directory (./phoenix-data by default, or whatever
# PHOENIX_DATA_DIR is set to on EC2). Keeping user data OUT of the project
# tree means:
#   - The app code in /app stays a clean read-only-ish layer.
#   - `docker compose down -v` doesn't wipe trade history (we don't use
#     anonymous Docker volumes, only host bind mounts).
#   - `git clean -fd` on the project repo can never touch your DB.
RUN mkdir -p /app/data/downloaded /app/data/logs \
 && chown -R phoenix:phoenix /app

USER phoenix

# Tell the app where on disk its writable state lives. The host path that
# /app/data maps to is set in docker-compose.yml (PHOENIX_DATA_DIR).
ENV PHOENIX_DB_PATH=/app/data/data.db \
    PHOENIX_DOWNLOADED_DIR=/app/data/downloaded \
    PHOENIX_LOG_DIR=/app/data/logs

# Flask binds 127.0.0.1 by default for safety. Inside the container we need
# 0.0.0.0 so the host port mapping can reach us. docker-compose binds the
# published port to 127.0.0.1 on the host, so LAN exposure stays zero.
ENV PHOENIX_BIND_HOST=0.0.0.0
EXPOSE 5000

# Unbuffered Python so `docker compose logs -f` streams output immediately.
ENV PYTHONUNBUFFERED=1

# Production server.
#
# Why gunicorn instead of `python app.py` (Flask dev server)?
#   - Werkzeug's dev server prints "do not use this in production" because
#     it's single-threaded and not battle-tested for concurrent traffic.
#   - Gunicorn handles concurrent requests, graceful reloads, and proper
#     signal handling for `docker compose stop`.
#
# Tuning:
#   GUNICORN_WORKERS  Number of worker processes. Keep this at 1 unless you
#                     swap flask-limiter's storage to Redis. Multiple workers
#                     mean separate in-memory rate-limit counters per worker,
#                     so the 5/min cap on /run/* would silently relax to
#                     5×workers/min.
#   GUNICORN_THREADS  Threads per worker. With workers=1, threads give you
#                     concurrent request handling (e.g. one user can browse
#                     reports while another POSTs).
#   GUNICORN_TIMEOUT  Per-request worker timeout in seconds. CGT pipeline
#                     can take a few seconds on big accounts, so we set 60.
#   PHOENIX_PORT      Container-internal port (compose maps it to the host).
#
# Tweak any of them via the .env file or env vars passed to docker compose
# without rebuilding the image.
ENV GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=60

# `exec` makes gunicorn PID 1 in the container so SIGTERM from `docker compose
# stop` reaches it directly (graceful shutdown). Shell form is required for
# env-var expansion in the args.
CMD exec gunicorn \
    --bind ${PHOENIX_BIND_HOST}:${PHOENIX_PORT:-5000} \
    --workers ${GUNICORN_WORKERS} \
    --threads ${GUNICORN_THREADS} \
    --timeout ${GUNICORN_TIMEOUT} \
    --access-logfile - \
    --error-logfile - \
    --access-logformat '%(h)s "%(r)s" %(s)s %(b)s %(L)ss "%(f)s"' \
    app:app
