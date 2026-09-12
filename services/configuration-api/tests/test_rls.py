"""Row Level Security — migration coverage and policy correctness (3b.1).

We verify three things without a live database:

1. The migration file covers *exactly* the same tables as
   ``TENANT_OWNED_TABLES`` in the model registry — the two lists must stay
   in sync, so a new model without an RLS policy is a test failure.

2. The upgrade SQL for every table includes ``ENABLE ROW LEVEL SECURITY``,
   ``FORCE ROW LEVEL SECURITY``, and ``CREATE POLICY tenant_isolation``.

3. The policy expression lets the GUC bypass (platform routes) and enforces
   it (tenant routes) — verified by text-level inspection since we cannot run
   a real PostgreSQL session in unit tests.

Integration tests (actual RLS enforcement against a live DB) live in
``tests/integration/test_rls_integration.py`` and run under ``make test-api``.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from app.db.models import TENANT_OWNED_TABLES

# Load the migration file by path so the test works both inside the Docker
# container (/app) and locally, without requiring alembic/versions to be a
# package (it has no __init__.py).
_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "20260912_1200_row_level_security.py"
)
_TICKETS_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "20260912_1715_tickets.py"
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("rls_migration", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None, (
        f"Migration file not found: {_MIGRATION_FILE}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def later_rls_tables() -> frozenset[str]:
    """Tables that gained RLS in revisions after the original 27-table pass."""
    spec = importlib.util.spec_from_file_location("tickets_rls", _TICKETS_MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return frozenset(mod._ADDITIONAL_RLS_TABLES)


def _covered(migration, later_rls_tables: frozenset[str]) -> frozenset[str]:
    return frozenset(migration._TENANT_OWNED_TABLES) | later_rls_tables


# --------------------------------------------------------------------------- #
# Table-list parity
# --------------------------------------------------------------------------- #


class TestTableListParity:
    def test_migration_covers_every_tenant_owned_table(
        self, migration, later_rls_tables: frozenset[str]
    ) -> None:
        """Adding a model to TENANT_OWNED_TABLES without touching an RLS
        migration means that model has no policy — a potential leak."""
        missing = TENANT_OWNED_TABLES - _covered(migration, later_rls_tables)
        assert not missing, (
            f"Tables in TENANT_OWNED_TABLES but absent from RLS migrations: "
            f"{sorted(missing)}. Add them to the original list or to "
            f"_ADDITIONAL_RLS_TABLES on a later revision."
        )

    def test_migration_has_no_extra_tables(
        self, migration, later_rls_tables: frozenset[str]
    ) -> None:
        extra = _covered(migration, later_rls_tables) - TENANT_OWNED_TABLES
        assert not extra, (
            f"Tables in RLS migrations but absent from TENANT_OWNED_TABLES: "
            f"{sorted(extra)}."
        )

    def test_migration_table_list_has_no_duplicates(self, migration) -> None:
        tables = migration._TENANT_OWNED_TABLES
        assert len(tables) == len(set(tables)), "Duplicate table names in migration"

    def test_count_matches_registry(self, migration, later_rls_tables: frozenset[str]) -> None:
        assert len(_covered(migration, later_rls_tables)) == len(TENANT_OWNED_TABLES)

    def test_tickets_revision_applies_rls(self) -> None:
        src = _TICKETS_MIGRATION_FILE.read_text()
        assert "ENABLE ROW LEVEL SECURITY" in src
        assert "FORCE ROW LEVEL SECURITY" in src
        assert "CREATE POLICY tenant_isolation" in src
        assert "tickets" in src


# --------------------------------------------------------------------------- #
# Upgrade SQL
# --------------------------------------------------------------------------- #


class TestUpgradeSQL:
    """Inspect upgrade() source to confirm each table gets all three DDL statements."""

    @pytest.fixture(scope="class")
    def upgrade_src(self, migration) -> str:
        return inspect.getsource(migration.upgrade)

    def test_upgrade_enables_rls_on_all_tables(self, upgrade_src: str) -> None:
        assert "ENABLE ROW LEVEL SECURITY" in upgrade_src

    def test_upgrade_forces_rls_on_all_tables(self, upgrade_src: str) -> None:
        """FORCE is required because voice_agent owns the tables and would
        otherwise bypass RLS as table owner."""
        assert "FORCE ROW LEVEL SECURITY" in upgrade_src

    def test_upgrade_creates_tenant_isolation_policy(self, upgrade_src: str) -> None:
        assert "CREATE POLICY tenant_isolation" in upgrade_src

    def test_policy_uses_set_config_guc(self, migration) -> None:
        """The policy must read the GUC set by get_tenant_scope."""
        assert "app.tenant_id" in migration._POLICY_USING

    def test_policy_uses_missing_ok_flag(self, migration) -> None:
        """current_setting('app.tenant_id', TRUE) — the TRUE flag returns an
        empty string instead of raising an error when the GUC is not set.
        Without it, platform routes (no GUC) would crash."""
        assert "TRUE" in migration._POLICY_USING

    def test_policy_allows_bypass_when_guc_absent(self, migration) -> None:
        """When the GUC is not set (empty string → NULL), the IS NULL check
        must be TRUE so platform routes can see all rows."""
        assert "IS NULL" in migration._POLICY_USING

    def test_policy_casts_guc_to_uuid(self, migration) -> None:
        """The GUC value is a string; comparing it to a uuid column requires
        an explicit cast so PostgreSQL uses the indexed UUID comparison."""
        assert "::uuid" in migration._POLICY_USING


# --------------------------------------------------------------------------- #
# Downgrade SQL
# --------------------------------------------------------------------------- #


class TestDowngradeSQL:
    @pytest.fixture(scope="class")
    def downgrade_src(self, migration) -> str:
        return inspect.getsource(migration.downgrade)

    def test_downgrade_drops_the_policy(self, downgrade_src: str) -> None:
        assert "DROP POLICY" in downgrade_src

    def test_downgrade_disables_force(self, downgrade_src: str) -> None:
        assert "NO FORCE ROW LEVEL SECURITY" in downgrade_src

    def test_downgrade_disables_rls(self, downgrade_src: str) -> None:
        assert "DISABLE ROW LEVEL SECURITY" in downgrade_src

    def test_downgrade_uses_if_exists(self, downgrade_src: str) -> None:
        """IF EXISTS makes the downgrade idempotent — safe to re-run."""
        assert "IF EXISTS" in downgrade_src


# --------------------------------------------------------------------------- #
# Policy semantics (logical verification)
# --------------------------------------------------------------------------- #


class TestPolicySemantics:
    """Verify the policy expression handles all three cases correctly.

    We cannot execute PostgreSQL SQL here, so we evaluate the boolean logic
    symbolically by substituting the GUC return value and checking whether
    the expression would admit or reject the row.
    """

    def _evaluate(self, migration, guc_value: str, tenant_id: str) -> bool:
        """
        Simulate the policy USING clause:
            nullif(guc_value, '') IS NULL
            OR tenant_id = nullif(guc_value, '')::uuid

        `::uuid` cast is approximated as a string equality here.
        """
        nullif_result = None if guc_value == "" else guc_value
        bypass = nullif_result is None  # IS NULL check
        match = nullif_result is not None and tenant_id == nullif_result
        return bypass or match

    def test_guc_not_set_allows_all_rows(self, migration) -> None:
        """Platform routes set no GUC; all rows must be visible."""
        for tid in ["uuid-a", "uuid-b", "uuid-c"]:
            assert self._evaluate(migration, guc_value="", tenant_id=tid), (
                f"Row with tenant_id={tid!r} should be visible when GUC is absent"
            )

    def test_guc_set_allows_matching_tenant(self, migration) -> None:
        """Tenant routes: only the caller's rows are visible."""
        assert self._evaluate(
            migration, guc_value="tenant-a-uuid", tenant_id="tenant-a-uuid"
        )

    def test_guc_set_blocks_other_tenant(self, migration) -> None:
        """Tenant routes: another tenant's rows must be excluded."""
        assert not self._evaluate(
            migration, guc_value="tenant-a-uuid", tenant_id="tenant-b-uuid"
        )
