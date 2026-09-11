"""Tenant isolation (spec 7).

Spec 7 requires isolation to be **tested explicitly**, so these are not
incidental assertions inside feature tests. They exercise the mechanism
itself: that the repository cannot be talked into touching another tenant's
rows, and that a client cannot supply its own tenant identity.

The repository tests use fakes rather than a database. What is being tested is
the scoping logic — which filter is applied, which write is refused — and that
is decided before any SQL is emitted.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest

from app.db.models import (
    PLATFORM_TABLES,
    TENANT_OPTIONAL_TABLES,
    TENANT_OWNED_TABLES,
    Agent,
    Call,
    Pbx,
    Provider,
    ProviderCredential,
    SipTrunk,
    Tenant,
    User,
)
from app.db.repository import (
    TenantIsolationError,
    TenantRepository,
    assert_tenant_owned,
)

TENANT_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class FakeSession:
    """Records what the repository would execute, without a database."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.statements: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def execute(self, statement: object) -> object:  # pragma: no cover - shape only
        self.statements.append(statement)
        raise AssertionError("this test asserts on the statement, not its result")


def repo(tenant_id: uuid.UUID = TENANT_A) -> TenantRepository:
    return TenantRepository(session=FakeSession(), tenant_id=tenant_id)  # type: ignore[arg-type]


def rendered_sql(statement: object) -> str:
    """Compile a statement to SQL with literals inlined.

    PostgreSQL UUID literals render as unhyphenated hex, so assertions compare
    against ``uuid.hex`` rather than ``str(uuid)``.
    """
    return str(statement.compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# The classification itself
# --------------------------------------------------------------------------- #


class TestModelClassification:
    """A model must be explicitly classified before it can be scoped."""

    @pytest.mark.parametrize("model", [Agent, Pbx, SipTrunk, Call, ProviderCredential])
    def test_tenant_owned_models_are_accepted(self, model: type) -> None:
        assert_tenant_owned(model)  # must not raise

    @pytest.mark.parametrize("model", [Tenant, Provider])
    def test_platform_models_are_refused(self, model: type) -> None:
        """Scoping a platform table by tenant is a category error.

        Failing loudly beats returning an empty list, which would look like
        "this tenant has no providers" rather than "you asked the wrong way".
        """
        with pytest.raises(TenantIsolationError, match="not a tenant-owned table"):
            assert_tenant_owned(model)

    def test_nullable_tenant_models_are_refused(self) -> None:
        """``users`` has a nullable tenant_id, so automatic scoping is unsafe.

        A platform user's row has no tenant, so a blanket equality filter would
        silently exclude them and make "list all users" quietly wrong.
        """
        with pytest.raises(TenantIsolationError, match="nullable tenant_id"):
            assert_tenant_owned(User)

    def test_every_owned_table_name_is_real(self) -> None:
        from app.db.base import Base

        known = set(Base.metadata.tables)
        assert known >= TENANT_OWNED_TABLES

    def test_classifications_are_disjoint_and_complete(self) -> None:
        from app.db.base import Base

        known = set(Base.metadata.tables) | {"alembic_version"}
        classified = TENANT_OWNED_TABLES | TENANT_OPTIONAL_TABLES | PLATFORM_TABLES
        assert known == classified, f"unclassified: {sorted(known - classified)}"


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


class TestScopedReads:
    def test_a_scoped_select_filters_by_tenant(self) -> None:
        rendered = rendered_sql(repo().scoped(Agent))
        assert "agents.tenant_id =" in rendered
        assert TENANT_A.hex in rendered

    def test_two_repositories_produce_different_filters(self) -> None:
        """The tenant is bound per repository, not global state."""
        a = rendered_sql(repo(TENANT_A).scoped(Agent))
        b = rendered_sql(repo(TENANT_B).scoped(Agent))
        assert TENANT_A.hex in a and TENANT_B.hex not in a
        assert TENANT_B.hex in b and TENANT_A.hex not in b

    @pytest.mark.parametrize("model", [Agent, Pbx, SipTrunk, Call, ProviderCredential])
    def test_every_tenant_owned_model_can_be_scoped(self, model: type) -> None:
        rendered = rendered_sql(repo().scoped(model))
        assert TENANT_A.hex in rendered
        assert f"{model.__tablename__}.tenant_id =" in rendered

    def test_a_platform_model_cannot_be_scoped(self) -> None:
        with pytest.raises(TenantIsolationError):
            repo().scoped(Provider)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


class TestScopedWrites:
    def test_a_new_row_is_stamped_with_the_repository_tenant(self) -> None:
        repository = repo(TENANT_A)
        agent = Agent(name="Support")
        repository.add(agent)
        assert agent.tenant_id == TENANT_A

    def test_a_row_for_another_tenant_is_refused_not_rewritten(self) -> None:
        """Silently correcting the tenant would hide the bug that produced it.

        The symptom would appear much later, as data in the wrong place, with
        nothing in the code looking wrong.
        """
        repository = repo(TENANT_A)
        agent = Agent(name="Support", tenant_id=TENANT_B)
        with pytest.raises(TenantIsolationError, match="refusing to write"):
            repository.add(agent)
        assert agent not in repository.session.added  # type: ignore[attr-defined]

    def test_a_matching_tenant_is_accepted(self) -> None:
        repository = repo(TENANT_A)
        agent = Agent(name="Support", tenant_id=TENANT_A)
        repository.add(agent)
        assert agent.tenant_id == TENANT_A

    def test_a_platform_model_cannot_be_written_through_a_repository(self) -> None:
        with pytest.raises(TenantIsolationError):
            repo().add(Provider(kind="LLM", slug="x", display_name="X"))  # type: ignore[arg-type]


class TestOwnershipGuard:
    def test_a_row_from_this_tenant_passes(self) -> None:
        repo(TENANT_A).assert_owned(Agent(name="a", tenant_id=TENANT_A))

    def test_a_row_from_another_tenant_fails(self) -> None:
        """For rows loaded via a join or relationship, where scoping was not
        applied by construction."""
        with pytest.raises(TenantIsolationError, match="belongs to tenant"):
            repo(TENANT_A).assert_owned(Agent(name="a", tenant_id=TENANT_B))

    def test_a_row_with_no_tenant_fails(self) -> None:
        with pytest.raises(TenantIsolationError):
            repo(TENANT_A).assert_owned(Agent(name="a"))


# --------------------------------------------------------------------------- #
# Every resource named in spec 7
# --------------------------------------------------------------------------- #


class TestSpec7ResourceCoverage:
    """Spec 7 names the resources Tenant A must never reach in Tenant B.

    Asserted against the classification so that a resource added later cannot
    quietly land outside the isolation guarantee.
    """

    #: The spec-7 list mapped onto the tables that hold each resource.
    SPEC_7_RESOURCES: ClassVar[dict[str, str]] = {
        "users": "users",
        "PBXs": "pbxs",
        "SIP trunks": "sip_trunks",
        "DIDs": "phone_numbers",
        "AI agents": "agents",
        "prompts": "agent_versions",
        "provider credentials": "provider_credentials",
        "tools": "tools",
        "knowledge bases": "knowledge_bases",
        "calls": "calls",
        "recordings": "call_recordings",
        "transcripts": "call_transcripts",
        "analytics": "call_events",
        "usage": "usage",
        "billing information": "billing",
    }

    @pytest.mark.parametrize(("resource", "table"), sorted(SPEC_7_RESOURCES.items()))
    def test_resource_is_covered_by_the_isolation_model(self, resource: str, table: str) -> None:
        assert table in TENANT_OWNED_TABLES or table in TENANT_OPTIONAL_TABLES, (
            f"spec 7 requires {resource!r} to be tenant-isolated, but {table!r} "
            "is not classified as tenant-owned or tenant-optional"
        )

    def test_prompts_live_on_the_agent_version_and_are_isolated(self) -> None:
        """Spec 7 lists prompts separately, and they are a column rather than a
        table, so the containing table is what must be isolated."""
        from app.db.base import Base

        assert "system_prompt" in Base.metadata.tables["agent_versions"].columns
        assert "agent_versions" in TENANT_OWNED_TABLES
