# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ffmpeg and libsndfile back the audio paths; build-essential covers wheels
# without arm64 binaries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential curl ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY services/ai-agent-worker/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY services/shared /opt/shared
RUN pip install -e /opt/shared

COPY services/ai-agent-worker/ /app/

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Health/readiness/metrics endpoint. Realtime media does not pass through it.
EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8081/health || exit 1

CMD ["python", "-m", "worker.main"]
