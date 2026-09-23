# PH Store API — production image.
#
# Two things this image deliberately does NOT do:
#
#   * It does not run migrations on start. A container that migrates on boot
#     will happily run two migrations at once the moment you scale to two
#     replicas, or re-run them on every restart loop. `docker compose run
#     --rm migrate` does it once, on purpose — see docker-compose.yml.
#   * It does not seed. `rova seed` creates demo accounts with a published
#     password; that belongs in development and nowhere near a real pharmacy.
#
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg, build tools only while installing, then removed
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# dependency layer first so a code change does not reinstall the world
COPY pyproject.toml ./
COPY src ./src
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && pip install --no-cache-dir . \
 && apt-get purge -y gcc libpq-dev \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY alembic.ini ./
COPY migrations ./migrations
COPY docs ./docs
COPY scripts ./scripts

# uploads and price-list files land here; mounted as a volume in compose so
# they survive a redeploy
RUN mkdir -p /app/var/storage
ENV ROVA_STORAGE_DIR=/app/var/storage

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8099/healthz || exit 1

CMD ["python", "-m", "uvicorn", "--factory", "rova.main:create_app", \
     "--host", "0.0.0.0", "--port", "8099", "--workers", "2"]
