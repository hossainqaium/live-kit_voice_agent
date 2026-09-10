"""Tests for shared contract enumerations."""

from __future__ import annotations

from itertools import pairwise

import pytest

from shared.models import (
    CALL_STATE_TRANSITIONS,
    TENANT_ROLE_PERMISSIONS,
    TERMINAL_CALL_STATES,
    TRANSFER_FALLBACK_TRIGGERS,
    TRANSFER_STATUS_TRANSITIONS,
    CallState,
    Permission,
    TenantRole,
    TransferStatus,
)


class TestCallStateMachine:
    def test_every_spec_state_exists(self) -> None:
        """Spec 42 fixes the state list; nothing may be missing or renamed."""
        expected = {
            "NEW",
            "RINGING",
            "ANSWERED",
            "AI_CONNECTED",
            "IN_PROGRESS",
            "TRANSFERRING",
            "HUMAN_AGENT",
            "COMPLETED",
            "FAILED",
            "TIMEOUT",
            "CANCELLED",
            "BUSY",
            "NO_ANSWER",
        }
        assert {state.value for state in CallState} == expected

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        for state in TERMINAL_CALL_STATES:
            assert state not in CALL_STATE_TRANSITIONS

    def test_non_terminal_states_all_declare_transitions(self) -> None:
        for state in CallState:
            if state not in TERMINAL_CALL_STATES:
                assert state in CALL_STATE_TRANSITIONS, f"{state} has no transitions"

    def test_transitions_only_target_known_states(self) -> None:
        for source, targets in CALL_STATE_TRANSITIONS.items():
            for target in targets:
                assert isinstance(target, CallState), f"{source} -> {target}"

    def test_happy_path_is_reachable(self) -> None:
        path = [
            CallState.NEW,
            CallState.RINGING,
            CallState.ANSWERED,
            CallState.AI_CONNECTED,
            CallState.IN_PROGRESS,
            CallState.COMPLETED,
        ]
        for current, following in pairwise(path):
            assert following in CALL_STATE_TRANSITIONS[current], f"{current} -> {following}"

    def test_transfer_path_is_reachable(self) -> None:
        path = [
            CallState.IN_PROGRESS,
            CallState.TRANSFERRING,
            CallState.HUMAN_AGENT,
            CallState.COMPLETED,
        ]
        for current, following in pairwise(path):
            assert following in CALL_STATE_TRANSITIONS[current], f"{current} -> {following}"


class TestWarmTransfer:
    """The warm transfer sequence from clarification CR-1."""

    def test_full_warm_transfer_sequence_is_reachable(self) -> None:
        path = [
            TransferStatus.NOT_REQUESTED,
            TransferStatus.REQUESTED,
            TransferStatus.ANNOUNCING,
            TransferStatus.DIALING_AGENT,
            TransferStatus.WHISPERING_SUMMARY,
            TransferStatus.BRIDGED,
        ]
        for current, following in pairwise(path):
            assert following in TRANSFER_STATUS_TRANSITIONS[current], f"{current} -> {following}"

    def test_bridge_requires_the_summary_whisper_first(self) -> None:
        """The agent must hear the summary before the legs are bridged (TR-7)."""
        assert (
            TransferStatus.BRIDGED not in TRANSFER_STATUS_TRANSITIONS[TransferStatus.DIALING_AGENT]
        )
        assert (
            TransferStatus.BRIDGED in TRANSFER_STATUS_TRANSITIONS[TransferStatus.WHISPERING_SUMMARY]
        )

    def test_announcement_precedes_dialing(self) -> None:
        """The caller hears the announcement before the agent leg is dialled (TR-2)."""
        assert (
            TransferStatus.DIALING_AGENT
            not in TRANSFER_STATUS_TRANSITIONS[TransferStatus.REQUESTED]
        )
        assert TransferStatus.ANNOUNCING in TRANSFER_STATUS_TRANSITIONS[TransferStatus.REQUESTED]

    @pytest.mark.parametrize(
        "outcome",
        [
            TransferStatus.AGENT_NO_ANSWER,
            TransferStatus.AGENT_BUSY,
            TransferStatus.AGENT_REJECTED,
            TransferStatus.FAILED,
        ],
    )
    def test_agent_unavailable_outcomes_trigger_fallback(self, outcome: TransferStatus) -> None:
        """TR-10: the caller is never dropped when the agent cannot take the call."""
        assert outcome in TRANSFER_FALLBACK_TRIGGERS

    def test_dialing_can_reach_every_agent_unavailable_outcome(self) -> None:
        reachable = TRANSFER_STATUS_TRANSITIONS[TransferStatus.DIALING_AGENT]
        for outcome in TRANSFER_FALLBACK_TRIGGERS:
            assert outcome in reachable, f"DIALING_AGENT cannot reach {outcome}"

    def test_caller_can_abandon_at_every_stage_before_bridge(self) -> None:
        """A caller who hangs up mid-transfer must be representable."""
        for stage, targets in TRANSFER_STATUS_TRANSITIONS.items():
            if stage is TransferStatus.NOT_REQUESTED:
                continue
            assert TransferStatus.ABANDONED in targets, f"{stage} cannot reach ABANDONED"

    def test_abandoned_and_bridged_are_terminal(self) -> None:
        assert TransferStatus.ABANDONED not in TRANSFER_STATUS_TRANSITIONS
        assert TransferStatus.BRIDGED not in TRANSFER_STATUS_TRANSITIONS


class TestPermissions:
    def test_every_spec_permission_exists(self) -> None:
        """Spec 8 lists these exactly; the strings are an API contract."""
        expected = {
            "agents.read",
            "agents.write",
            "agents.publish",
            "pbxs.read",
            "pbxs.write",
            "sip_trunks.read",
            "sip_trunks.write",
            "calls.read",
            "recordings.read",
            "analytics.read",
            "users.manage",
            "billing.read",
        }
        assert {p.value for p in Permission} == expected

    def test_tenant_admin_holds_every_permission(self) -> None:
        assert TENANT_ROLE_PERMISSIONS[TenantRole.TENANT_ADMIN] == frozenset(Permission)

    def test_every_tenant_role_is_mapped(self) -> None:
        for role in TenantRole:
            assert role in TENANT_ROLE_PERMISSIONS

    def test_viewer_cannot_write_anything(self) -> None:
        viewer = TENANT_ROLE_PERMISSIONS[TenantRole.VIEWER]
        assert not any(p.value.endswith((".write", ".publish", ".manage")) for p in viewer)

    def test_analyst_cannot_touch_configuration(self) -> None:
        analyst = TENANT_ROLE_PERMISSIONS[TenantRole.ANALYST]
        assert Permission.AGENTS_WRITE not in analyst
        assert Permission.PBXS_WRITE not in analyst
        assert Permission.SIP_TRUNKS_WRITE not in analyst

    def test_only_tenant_admin_manages_users_and_billing(self) -> None:
        for role, permissions in TENANT_ROLE_PERMISSIONS.items():
            if role is TenantRole.TENANT_ADMIN:
                continue
            assert Permission.USERS_MANAGE not in permissions, role
            assert Permission.BILLING_READ not in permissions, role
