"""Database access for the worker.

The worker reads configuration and writes call records. It is not the
Configuration API: it never serves configuration to anyone, and it holds no
tenant-specific logic (spec 23).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from worker.settings import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide engine.

    Pool sizing matters here in a way it does not in the API: each concurrent
    call holds a connection only briefly — configuration is loaded once per
    call (spec 45) and state transitions are short — so a small pool serves
    many more calls than its size.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.sqlalchemy_dsn,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)
    return _factory


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
