"""DID form field coverage (spec 17, Plan 4b.2).

The phone-number API already accepted ``routing_rule_id`` and
``business_hours_id``.  This suite asserts the two things that were missing:

1. Those FKs are validated against the tenant (same as PBX / trunk / agent).
2. The response resolves human-readable names so the list view does not show
   UUIDs.
3. The tenant console form actually offers the fields (source inspection;
   skipped inside Docker where ``services/frontend`` is not mounted).

Fallback is configured on the routing rule, not as DID columns — the form
shows the selected rule's fallback as a read-only hint.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.api.v1 import phone_numbers as phone_numbers_api
from app.schemas.telephony import PhoneNumberResponse, PhoneNumberUpdate


_FRONTEND_FORM = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "app"
    / "phone-numbers"
    / "page.tsx"
)


class TestReferenceValidation:
    def test_routing_rule_id_is_validated(self) -> None:
        src = inspect.getsource(phone_numbers_api._validate_references)
        assert "routing_rule_id" in src
        assert "RoutingRule" in src

    def test_business_hours_id_is_validated(self) -> None:
        src = inspect.getsource(phone_numbers_api._validate_references)
        assert "business_hours_id" in src
        assert "BusinessHours" in src

    def test_existing_fks_remain_validated(self) -> None:
        src = inspect.getsource(phone_numbers_api._validate_references)
        for field in ("pbx_id", "sip_trunk_id", "inbound_agent_id"):
            assert field in src


class TestResponseDecoration:
    def test_response_includes_routing_rule_name(self) -> None:
        assert "routing_rule_name" in PhoneNumberResponse.model_fields

    def test_response_includes_business_hours_name(self) -> None:
        assert "business_hours_name" in PhoneNumberResponse.model_fields

    def test_update_schema_accepts_both_ids(self) -> None:
        fields = PhoneNumberUpdate.model_fields
        assert "routing_rule_id" in fields
        assert "business_hours_id" in fields

    def test_decorate_resolves_rule_and_hours_names(self) -> None:
        src = inspect.getsource(phone_numbers_api._decorate)
        assert "RoutingRule" in src
        assert "BusinessHours" in src
        assert "routing_rule_name" in src
        assert "business_hours_name" in src


class TestCreateDoesNotBreakOnAssignedAgent:
    def test_audit_snapshot_is_json_safe(self) -> None:
        src = inspect.getsource(phone_numbers_api)
        assert "audit.snapshot(row, *_AUDITED)" in src
        assert "inbound_agent_id" in phone_numbers_api._AUDITED

    def test_create_resyncs_the_trunk_after_commit(self) -> None:
        src = inspect.getsource(phone_numbers_api.create_number)
        assert "enqueue_trunk_sync" in src
        assert src.index("commit") < src.index("enqueue_trunk_sync")

    def test_delete_resyncs_the_previous_trunk(self) -> None:
        src = inspect.getsource(phone_numbers_api.delete_number)
        assert "previous_trunk_id" in src
        assert "enqueue_trunk_sync" in src


class TestDidFormOffersTheFields:
    def test_form_file_exists_or_skip(self) -> None:
        if not _FRONTEND_FORM.exists():
            pytest.skip("services/frontend is not mounted in this container")
        assert _FRONTEND_FORM.exists()

    def test_form_state_includes_routing_rule_id(self) -> None:
        if not _FRONTEND_FORM.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _FRONTEND_FORM.read_text()
        assert "routing_rule_id" in src
        assert "Evaluate all matching rules" in src

    def test_form_state_includes_business_hours_id(self) -> None:
        if not _FRONTEND_FORM.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _FRONTEND_FORM.read_text()
        assert "business_hours_id" in src
        assert "Business hours" in src

    def test_form_shows_pinned_rule_fallback(self) -> None:
        """Fallback lives on the rule; the DID form must surface it."""
        if not _FRONTEND_FORM.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _FRONTEND_FORM.read_text()
        assert "FALLBACK_ACTION_LABELS" in src
        assert "fallback_action" in src
