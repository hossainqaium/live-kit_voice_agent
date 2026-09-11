"""Alembic environment.

Migrations run through asyncpg, the same driver the application uses, so there
is one connection path to configure and one set of type behaviours to reason
about.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the models package registers every table on Base.metadata.
# Without it, autogenerate would produce an empty diff and silently drop tables.
import app.db.models  # noqa: F401
from app.core.settings import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_dsn)

target_metadata = Base.metadata


def _render_item(type_, obj, autogen_context) -> str | bool:
    """Render third-party column types with the import they need.

    Autogenerate writes a fully-qualified type name into the revision but does
    not add the corresponding import, so a pgvector column produces a script
    that fails with NameError the first time it runs. Adding the import to the
    template instead would put an unused import in every revision.

    Returning False falls back to Alembic's default rendering.
    """
    if type_ == "type" and obj.__class__.__module__.startswith("pgvector"):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.{obj.__class__.__name__}()"
    return False


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Keep extension-owned tables out of autogenerate.

    pgvector and similar extensions create their own catalog objects; without
    this filter Alembic would try to drop them on every revision.
    """
    return not (type_ == "table" and name in {"spatial_ref_sys"})


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of applying it (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
        # Every migration runs in one transaction so a failure leaves no
        # half-applied schema behind.
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
