# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build dependencies for asyncpg / cryptography wheels that lack arm64 binaries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Requirements first so dependency layers survive source edits.
COPY services/configuration-api/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# The shared package is installed editable so a bind mount in development
# reflects changes without a rebuild.
COPY services/shared /opt/shared
RUN pip install -e /opt/shared

COPY services/configuration-api/ /app/

# Run as a non-root user. Kubernetes will enforce this anyway; matching it
# locally means permission problems surface in development, not in production.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Compose and Kubernetes both probe /health; keeping the check in the image
# means `docker compose ps` reports truthful health without extra wiring.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
