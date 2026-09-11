"""Tenant-scoped data access (spec 6, 7).

Tenant isolation enforced by discipline fails eventually: it takes one
forgotten ``WHERE tenant_id = ...`` in one endpoint, and nothing in the code
looks wrong. So the filter is applied here, once, and handlers are given a
repository that cannot omit it.

``TenantRepository`` binds a session to exactly one tenant. Every read and
write it issues is filtered or stamped, and a row belonging to another tenant
is reported as absent rather than forbidden — a 404 rather than a 403, because
"you may not see this" still confirms the row exists, and cross-tenant
existence is itself information spec 7 does not permit leaking.

PostgreSQL Row Level Security is the intended second layer (Phase 3 item 3.6),
so that a future query written outside this class is still constrained by the
database. This class is the first layer, not the only one.
"""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.models import TENANT_OPTIONAL_TABLES, TENANT_OWNED_TABLES
from shared.logging import get_logger

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=Base)


class TenantIsolationError(RuntimeError):
    """A model was used with the wrong kind of repository.

    Raised loudly rather than degrading to an unscoped query: a mistake here is
    a cross-tenant data leak, so it must fail in development and in tests
    rather than quietly returning another tenant's rows.
    """


def _table_name(model: type[Base]) -> str:
    return str(model.__tablename__)


def assert_tenant_owned(model: type[Base]) -> None:
    """Fail unless the model is classified tenant-owned.

    The classification lives in ``app.db.models`` and is asserted by tests, so
    a new model has to be explicitly categorised before it can be used here.
    """
    name = _table_name(model)
    if name in TENANT_OWNED_TABLES:
        return
    if name in TENANT_OPTIONAL_TABLES:
        raise TenantIsolationError(
            f"{name!r} has a nullable tenant_id and cannot be scoped automatically; "
            "use an explicit query and state the intent"
        )
    raise TenantIsolationError(
        f"{name!r} is not a tenant-owned table; use the session directly for "
        "platform-level data, or classify it in app.db.models"
    )


class TenantRepository:
    """Data access bound to a single tenant.

    Constructed from a ``TenantScope``, so the tenant comes from the verified
    token and not from anything a caller can influence.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    @property
    def session(self) -> AsyncSession:
        """The underlying session.

        Exposed for platform-level joins and multi-step transactions. Using it
        for tenant-owned data bypasses the scoping this class exists to
        provide, so prefer the methods below.
        """
        return self._session

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def scoped(self, model: type[ModelT]) -> Select[tuple[ModelT]]:
        """A SELECT already filtered to this tenant.

        The building block for anything more complex than the helpers below:
        start from this rather than from ``select(Model)`` and the filter
        cannot be forgotten.
        """
        assert_tenant_owned(model)
        return select(model).where(model.tenant_id == self._tenant_id)  # type: ignore[attr-defined]

    async def get(self, model: type[ModelT], row_id: uuid.UUID) -> ModelT | None:
        """Fetch one row by id, within this tenant.

        Another tenant's row returns ``None``. The caller cannot distinguish
        "does not exist" from "belongs to someone else", which is the point.
        """
        result = await self._session.execute(
            self.scoped(model).where(model.id == row_id)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        model: type[ModelT],
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: Any | None = None,
    ) -> list[ModelT]:
        """A page of this tenant's rows."""
        statement = self.scoped(model)
        if order_by is not None:
            statement = statement.order_by(order_by)
        result = await self._session.execute(statement.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count(self, model: type[ModelT]) -> int:
        """How many rows this tenant has."""
        assert_tenant_owned(model)
        result = await self._session.execute(
            select(func.count()).select_from(model).where(model.tenant_id == self._tenant_id)  # type: ignore[attr-defined]
        )
        return int(result.scalar_one())

    async def exists(self, model: type[ModelT], row_id: uuid.UUID) -> bool:
        return await self.get(model, row_id) is not None

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def add(self, instance: ModelT) -> ModelT:
        """Stage a new row, stamping this tenant.

        A caller-supplied ``tenant_id`` that disagrees is refused rather than
        overwritten: silently rewriting it would hide a bug that only shows up
        as data in the wrong place.
        """
        model = type(instance)
        assert_tenant_owned(model)

        existing = getattr(instance, "tenant_id", None)
        if existing is not None and existing != self._tenant_id:
            raise TenantIsolationError(
                f"refusing to write a {_table_name(model)} row for tenant {existing} "
                f"from a repository scoped to {self._tenant_id}"
            )

        instance.tenant_id = self._tenant_id  # type: ignore[attr-defined]
        self._session.add(instance)
        return instance

    async def delete(self, model: type[ModelT], row_id: uuid.UUID) -> bool:
        """Delete one row within this tenant.

        Returns whether anything was deleted. Issued as a filtered DELETE
        rather than load-then-delete so that a concurrent tenant change cannot
        open a window between the check and the write.
        """
        assert_tenant_owned(model)
        result = await self._session.execute(
            delete(model).where(
                model.id == row_id,  # type: ignore[attr-defined]
                model.tenant_id == self._tenant_id,  # type: ignore[attr-defined]
            )
        )
        return bool(result.rowcount)

    # ------------------------------------------------------------------ #
    # Guards
    # ------------------------------------------------------------------ #

    def assert_owned(self, instance: Any) -> None:
        """Fail unless an already-loaded row belongs to this tenant.

        For rows obtained some other way — a join, a relationship, a
        platform-level query. The check is cheap and turns a silent
        cross-tenant write into an immediate error.
        """
        owner = getattr(instance, "tenant_id", None)
        if owner != self._tenant_id:
            raise TenantIsolationError(
                f"{type(instance).__name__} belongs to tenant {owner}, " f"not {self._tenant_id}"
            )


def repository_for(scope: Any) -> TenantRepository:
    """Build a repository from a ``TenantScope``.

    Kept as a function so the dependency wiring stays in ``app.core`` and this
    module does not import the HTTP layer.
    """
    return TenantRepository(session=scope.session, tenant_id=scope.tenant_id)
