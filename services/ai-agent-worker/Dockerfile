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

# Fetch the Silero VAD weights at build time. Downloading them on first use
# would add seconds to the first call of every fresh pod, which is exactly the
# latency the spec-56 budget is measured against.
RUN python -m worker.main download-files || \
    echo "warning: model prefetch failed; models will be fetched on first use"

# Health/readiness/metrics endpoint. Realtime media does not pass through it.
# 8090, not 8081: the LiveKit Agents runtime binds 8081 for its own server.
EXPOSE 8090

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8090/health || exit 1

CMD ["python", "-m", "worker.main"]
