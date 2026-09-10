"""Schema invariants (spec 6, 7, 41, 68).

These are structural tests. They do not exercise behaviour — they assert that
the schema itself cannot express a tenant-isolation violation. Adding a
tenant-owned model without ``tenant_id`` should fail CI, not ship.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from app.db.base import Base
from app.db.models import (
    PLATFORM_TABLES,
    TENANT_OPTIONAL_TABLES,
    TENANT_OWNED_TABLES,
)

ALL_TABLES = set(Base.metadata.tables) | {"alembic_version"}


class TestTenantIsolation:
    """Spec 6: every tenant-owned object carries ``tenant_id``."""

    def test_every_table_is_classified(self) -> None:
        """A new model must be declared tenant-owned, tenant-optional or platform.

        Without this, adding a model is enough to create a table nobody has
        decided the isolation rules for — and the default (no tenant_id) is
        the unsafe one.
        """
        classified = TENANT_OWNED_TABLES | TENANT_OPTIONAL_TABLES | PLATFORM_TABLES
        unclassified = ALL_TABLES - classified
        assert not unclassified, (
            f"unclassified table(s): {sorted(unclassified)}. Add each to "
            "TENANT_OWNED_TABLES, TENANT_OPTIONAL_TABLES or PLATFORM_TABLES "
            "in app/db/models/__init__.py with a reason."
        )

    def test_classification_references_only_real_tables(self) -> None:
        classified = TENANT_OWNED_TABLES | TENANT_OPTIONAL_TABLES | PLATFORM_TABLES
        assert not (
            classified - ALL_TABLES
        ), f"classification names non-existent table(s): {sorted(classified - ALL_TABLES)}"

    def test_classifications_do_not_overlap(self) -> None:
        assert not (TENANT_OWNED_TABLES & TENANT_OPTIONAL_TABLES)
        assert not (TENANT_OWNED_TABLES & PLATFORM_TABLES)
        assert not (TENANT_OPTIONAL_TABLES & PLATFORM_TABLES)

    @pytest.mark.parametrize("table_name", sorted(TENANT_OWNED_TABLES))
    def test_tenant_owned_table_has_non_nullable_tenant_id(self, table_name: str) -> None:
        table = Base.metadata.tables[table_name]
        assert "tenant_id" in table.columns, f"{table_name} has no tenant_id"
        column = table.columns["tenant_id"]
        assert not column.nullable, (
            f"{table_name}.tenant_id is nullable. A null tenant makes every "
            "isolation filter ambiguous."
        )

    @pytest.mark.parametrize("table_name", sorted(TENANT_OWNED_TABLES))
    def test_tenant_id_is_indexed(self, table_name: str) -> None:
        """Every tenant-scoped query filters on tenant_id, so it must be indexed."""
        table = Base.metadata.tables[table_name]
        column = table.columns["tenant_id"]
        indexed = column.index or any(
            "tenant_id" in [c.name for c in index.columns] for index in table.indexes
        )
        assert indexed, f"{table_name}.tenant_id is not indexed"

    @pytest.mark.parametrize("table_name", sorted(TENANT_OWNED_TABLES))
    def test_tenant_id_cascades_on_tenant_delete(self, table_name: str) -> None:
        """Deleting a tenant must leave nothing another tenant could reach."""
        table = Base.metadata.tables[table_name]
        fks = [fk for fk in table.foreign_keys if fk.column.table.name == "tenants"]
        assert fks, f"{table_name}.tenant_id has no foreign key to tenants"
        assert any(
            fk.ondelete == "CASCADE" for fk in fks
        ), f"{table_name}.tenant_id does not cascade on tenant delete"

    @pytest.mark.parametrize("table_name", sorted(PLATFORM_TABLES - {"alembic_version"}))
    def test_platform_table_has_no_tenant_id(self, table_name: str) -> None:
        """A platform table with a tenant column invites accidental scoping."""
        table = Base.metadata.tables[table_name]
        assert (
            "tenant_id" not in table.columns
        ), f"{table_name} is classified platform-level but has a tenant_id"


class TestCallRecord:
    """Spec 41: the minimum call record."""

    #: Named explicitly in spec 41. Renaming one of these breaks a documented
    #: contract, so the list is asserted rather than assumed.
    REQUIRED_COLUMNS = (
        "call_id",
        "tenant_id",
        "agent_id",
        "agent_version_id",
        "pbx_id",
        "sip_trunk_id",
        "did",
        "room_id",
        "caller_number",
        "destination_number",
        "direction",
        "start_time",
        "answer_time",
        "end_time",
        "duration_seconds",
        "state",
        "hangup_reason",
        "recording_id",
        "transcript_id",
        "transfer_status",
    )

    @pytest.mark.parametrize("column", REQUIRED_COLUMNS)
    def test_required_column_exists(self, column: str) -> None:
        assert (
            column in Base.metadata.tables["calls"].columns
        ), f"calls.{column} is required by spec 41"

    def test_call_id_is_unique(self) -> None:
        """Spec 43: the correlation ID joins logs, recordings and transcripts.

        Two calls sharing one would make every trace ambiguous.
        """
        table = Base.metadata.tables["calls"]
        unique_constraints = [
            c
            for c in table.constraints
            if c.__class__.__name__ == "UniqueConstraint"
            and [col.name for col in c.columns] == ["call_id"]
        ]
        assert unique_constraints, "calls.call_id is not unique"

    def test_agent_version_is_recorded_separately_from_agent(self) -> None:
        """Spec 19: a call must record which version executed it.

        Without this column, publishing v2 would retroactively reattribute
        every v1 call.
        """
        columns = Base.metadata.tables["calls"].columns
        assert "agent_id" in columns
        assert "agent_version_id" in columns

    def test_agent_deletion_preserves_call_history(self) -> None:
        """Deleting an agent must not delete the record of its calls."""
        table = Base.metadata.tables["calls"]
        for fk in table.foreign_keys:
            if fk.parent.name in ("agent_id", "agent_version_id"):
                assert (
                    fk.ondelete == "SET NULL"
                ), f"calls.{fk.parent.name} should SET NULL, not {fk.ondelete}"


class TestWarmTransfer:
    """Clarification CR-1: the warm transfer must be reconstructable afterwards."""

    @pytest.mark.parametrize(
        "column",
        [
            "transfer_status",
            "transfer_destination_id",
            "transfer_announcement_started_at",
            "transfer_agent_dialed_at",
            "transfer_agent_answered_at",
            "transfer_whisper_seconds",
            "transfer_bridged_at",
            "transfer_fallback_taken",
            "transfer_summary",
        ],
    )
    def test_transfer_trail_column_exists(self, column: str) -> None:
        assert column in Base.metadata.tables["calls"].columns

    def test_agent_version_carries_the_announcement_and_whisper_settings(self) -> None:
        """TR-3, TR-9, TR-12, TS-1: all configurable per agent."""
        columns = Base.metadata.tables["agent_versions"].columns
        for column in (
            "transfer_announcement_text",
            "hold_media_object_key",
            "transfer_summary_template",
            "transfer_summary_max_seconds",
            "transfer_skip_dtmf",
        ):
            assert column in columns, f"agent_versions.{column} is required by CR-1"

    def test_a_whisper_segment_can_be_marked_inaudible_to_the_caller(self) -> None:
        """TR-6: the whisper is part of the audit trail, not the conversation."""
        assert "is_private_to_agent" in Base.metadata.tables["call_transcript_segments"].columns


class TestRecordingStorage:
    """Spec 3 and 39: recordings never live in PostgreSQL."""

    def test_recording_table_stores_a_pointer_not_audio(self) -> None:
        columns = Base.metadata.tables["call_recordings"].columns
        assert "bucket" in columns
        assert "object_key" in columns

    def test_no_table_stores_binary_audio(self) -> None:
        """A LargeBinary column is how audio ends up in the database by accident."""
        offenders = []
        for table in Base.metadata.tables.values():
            for column in table.columns:
                type_name = column.type.__class__.__name__.upper()
                if type_name in ("LARGEBINARY", "BLOB"):
                    offenders.append(f"{table.name}.{column.name}")
        # BYTEA is permitted for encrypted credentials, which are small and
        # must be stored; audio-sized blobs are not.
        assert not offenders, f"binary column(s) that could hold media: {offenders}"


class TestLiveKitSync:
    """Spec 12 and 46: mirrored resources must be reconcilable."""

    #: Tables whose rows correspond to a resource that also exists in LiveKit.
    MIRRORED = ("sip_trunks", "livekit_dispatch_rules")

    @pytest.mark.parametrize("table_name", MIRRORED)
    def test_mirrored_table_records_the_livekit_id_and_sync_state(self, table_name: str) -> None:
        """Without the stored ID, finding an orphaned LiveKit resource means guessing by name."""
        columns = Base.metadata.tables[table_name].columns
        for column in ("livekit_resource_id", "sync_status", "last_synced_at", "sync_error"):
            assert column in columns, f"{table_name}.{column} is required by spec 12"


class TestCredentialStorage:
    """Spec 53 and 54: credentials are encrypted at rest and never plaintext."""

    #: (table, ciphertext column) pairs.
    ENCRYPTED = (
        ("provider_credentials", "api_key_ciphertext"),
        ("sip_credentials", "password_ciphertext"),
        ("tools", "auth_secret_ciphertext"),
    )

    @pytest.mark.parametrize(("table_name", "column"), ENCRYPTED)
    def test_secret_is_stored_as_ciphertext(self, table_name: str, column: str) -> None:
        assert column in Base.metadata.tables[table_name].columns

    @pytest.mark.parametrize(("table_name", "column"), ENCRYPTED)
    def test_encrypted_column_records_its_key_version(self, table_name: str, column: str) -> None:
        """Key rotation needs to know which key encrypted each value."""
        assert "encryption_key_version" in Base.metadata.tables[table_name].columns

    def test_no_column_is_named_like_a_plaintext_secret(self) -> None:
        """Catches a column added as `api_key` or `password` rather than ciphertext.

        Only text and binary columns can hold a secret, so the check is typed:
        `tokens_valid_from` is a timestamp and matching it on the substring
        "token" alone would be a false positive.
        """
        offenders = []
        allowed = {"password_hash"}  # a one-way hash is not a recoverable secret
        secret_capable = ("VARCHAR", "TEXT", "STRING", "BYTEA", "LARGEBINARY", "CITEXT", "JSONB")

        for table in Base.metadata.tables.values():
            for column in table.columns:
                name = column.name
                if name in allowed or name.endswith(("_ciphertext", "_hint", "_version")):
                    continue
                if not any(t in column.type.__class__.__name__.upper() for t in secret_capable):
                    continue
                if any(token in name for token in ("password", "api_key", "secret", "token")):
                    offenders.append(f"{table.name}.{name}")

        assert not offenders, f"possibly plaintext secret column(s): {offenders}"


class TestModelRegistration:
    def test_every_model_is_importable_from_the_package(self) -> None:
        """An unimported model produces an empty autogenerate diff, and then a
        later revision that tries to drop its table."""
        import app.db.models as models

        mapped = {mapper.class_.__name__ for mapper in Base.registry.mappers}
        exported = set(models.__all__)
        assert mapped <= exported, f"model(s) missing from __all__: {sorted(mapped - exported)}"

    def test_all_tables_have_a_primary_key(self) -> None:
        for table in Base.metadata.tables.values():
            assert table.primary_key.columns, f"{table.name} has no primary key"

    def test_inspection_works_on_every_mapper(self) -> None:
        """Catches a broken relationship or an unresolvable foreign key."""
        for mapper in Base.registry.mappers:
            inspect(mapper.class_)
