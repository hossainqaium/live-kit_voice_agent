"""Configuration API entrypoint.

This is the Control Plane (spec 2, 10): the central authority between the Web
UI and PostgreSQL, Redis, the LiveKit APIs and AI worker configuration.

It must never process a realtime audio stream. Audio belongs entirely to the
Voice Execution Plane.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1 import api_router
from app.core.middleware import AccessLogMiddleware, CorrelationMiddleware
from app.core.settings import get_settings
from app.db.session import dispose_engine
from shared.logging import configure_logging, get_logger
from shared.telemetry import CONTENT_TYPE_LATEST, ControlPlaneMetrics, render_metrics

settings = get_settings()
configure_logging(service=settings.service_name, level=settings.log_level)
logger = get_logger(__name__)

metrics = ControlPlaneMetrics()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "service_starting",
        extra={"environment": settings.environment, "api_base_path": settings.api_base_path},
    )
    yield
    await dispose_engine()
    logger.info("service_stopped")


app = FastAPI(
    title="AI Voice Agent Platform — Configuration API",
    description=(
        "Control Plane for a multi-tenant AI voice agent platform built on LiveKit. "
        "Manages tenants, users, PBXs, SIP trunks, DIDs, agents, routing, providers, "
        "voices, tools, knowledge bases and LiveKit resources."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Middleware runs bottom-up, so correlation is added last to wrap everything.
app.add_middleware(AccessLogMiddleware, metrics=metrics)
app.add_middleware(CorrelationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# Probes stay off the versioned path so orchestrators need no version knowledge.
app.include_router(health_router)
app.include_router(api_router, prefix=settings.api_base_path)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": settings.service_name,
        "version": app.version,
        "docs": "/docs",
        "api": settings.api_base_path,
    }
