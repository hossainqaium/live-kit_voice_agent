"""Test fixtures for the Configuration API."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

# Settings are read at import time, so the environment must be set before the
# application module is imported anywhere in the test session.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("JWT_SECRET", "test-secret")
# The drift loop would call LiveKit from the in-process ASGI lifespan.
os.environ.setdefault("LIVEKIT_DRIFT_CHECK_ENABLED", "false")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app():
    from app.main import app as fastapi_app

    return fastapi_app


@pytest.fixture
async def client(app) -> AsyncIterator:
    """HTTP client bound to the app in-process, with no network involved."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Iterator[None]:
    """Clear the settings singleton so per-test environment changes take hold."""
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
