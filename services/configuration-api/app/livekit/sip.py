"""LiveKit SIP resource management (spec 11, 12, 15, 21).

Translates between the platform's own records and LiveKit's SIP API. Two
things are deliberate:

* PostgreSQL is the source of truth (spec 10). These functions read our model
  and shape LiveKit to match, never the reverse.
* Dispatch rules are long-lived (spec 21). Nothing here is called on the call
  path; a rule is created once when a tenant is configured and reused for
  every subsequent call.
"""

from __future__ import annotations

from dataclasses import dataclass

from livekit.api import (
    SIP_MEDIA_ENCRYPT_DISABLE,
    SIP_MEDIA_ENCRYPT_REQUIRE,
    CreateSIPDispatchRuleRequest,
    CreateSIPInboundTrunkRequest,
    DeleteSIPDispatchRuleRequest,
    DeleteSIPTrunkRequest,
    ListSIPDispatchRuleRequest,
    ListSIPInboundTrunkRequest,
    SIPDispatchRule,
    SIPDispatchRuleIndividual,
    SIPDispatchRuleInfo,
    SIPInboundTrunkInfo,
)
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.room import RoomConfiguration

from app.db.models import LiveKitDispatchRule, SipTrunk
from app.livekit.client import LiveKitAdminClient
from app.livekit.errors import LiveKitResourceMissingError
from shared.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrunkSnapshot:
    """What LiveKit currently holds for an inbound trunk.

    Only the fields drift detection compares (spec 46). Reading everything
    back would make the comparison sensitive to defaults LiveKit fills in.
    """

    livekit_trunk_id: str
    name: str
    numbers: tuple[str, ...]
    allowed_addresses: tuple[str, ...]
    auth_username: str | None


@dataclass(frozen=True, slots=True)
class DispatchRuleSnapshot:
    livekit_rule_id: str
    name: str
    trunk_ids: tuple[str, ...]
    room_prefix: str | None
    agent_names: tuple[str, ...]


class SipResourceManager:
    """Creates and reads the LiveKit SIP resources our records describe."""

    def __init__(self, client: LiveKitAdminClient | None = None) -> None:
        self._client = client or LiveKitAdminClient()

    # ------------------------------------------------------------------ #
    # Inbound trunks (spec 15)
    # ------------------------------------------------------------------ #

    async def create_inbound_trunk(
        self, trunk: SipTrunk, *, numbers: list[str], auth_password: str | None = None
    ) -> TrunkSnapshot:
        """Create the LiveKit inbound trunk for one of our trunk records.

        ``numbers`` are the DIDs this trunk accepts. LiveKit matches inbound
        calls to a trunk by called number, so an empty list makes the trunk
        accept any number — which is why the caller passes it explicitly
        rather than it defaulting.
        """
        info = SIPInboundTrunkInfo(
            name=trunk.name,
            # Our row's UUID travels in metadata so a LiveKit-side resource can
            # be traced back to its owner without a name-matching heuristic.
            metadata=f"tenant={trunk.tenant_id};sip_trunk={trunk.id}",
            numbers=numbers,
            allowed_addresses=list(trunk.allowed_ips or []),
            auth_username=trunk.auth_username or "",
            auth_password=auth_password or "",
            media_encryption=(
                SIP_MEDIA_ENCRYPT_REQUIRE
                if trunk.media_encryption_required
                else SIP_MEDIA_ENCRYPT_DISABLE
            ),
        )

        created = await self._client.call(
            "create_sip_inbound_trunk",
            lambda api: api.sip.create_sip_inbound_trunk(CreateSIPInboundTrunkRequest(trunk=info)),
        )

        logger.info(
            "livekit_trunk_created",
            extra={
                "tenant_id": str(trunk.tenant_id),
                "sip_trunk_id": str(trunk.id),
                "livekit_trunk_id": created.sip_trunk_id,
                "number_count": len(numbers),
            },
        )
        return _trunk_snapshot(created)

    async def get_inbound_trunk(self, livekit_trunk_id: str) -> TrunkSnapshot:
        """Read one trunk back from LiveKit.

        Raises :class:`LiveKitResourceMissingError` when LiveKit does not have
        it, which is the drift signal (spec 46). The SDK lists rather than
        gets, so absence surfaces as an empty result rather than an error.
        """
        trunks = await self._client.call(
            "list_sip_inbound_trunk",
            lambda api: api.sip.list_sip_inbound_trunk(
                ListSIPInboundTrunkRequest(trunk_ids=[livekit_trunk_id])
            ),
        )
        for item in trunks.items:
            if item.sip_trunk_id == livekit_trunk_id:
                return _trunk_snapshot(item)

        raise LiveKitResourceMissingError(
            f"LiveKit has no inbound trunk {livekit_trunk_id}",
            resource_id=livekit_trunk_id,
        )

    async def list_inbound_trunks(self) -> list[TrunkSnapshot]:
        """Every inbound trunk LiveKit knows about.

        Used by drift detection to find resources LiveKit holds that
        PostgreSQL does not — the orphan direction, which a per-row check
        cannot see.
        """
        trunks = await self._client.call(
            "list_sip_inbound_trunk",
            lambda api: api.sip.list_sip_inbound_trunk(ListSIPInboundTrunkRequest()),
        )
        return [_trunk_snapshot(item) for item in trunks.items]

    async def update_inbound_trunk(
        self,
        livekit_trunk_id: str,
        trunk: SipTrunk,
        *,
        numbers: list[str],
        auth_password: str | None = None,
    ) -> TrunkSnapshot:
        """Reshape an existing LiveKit trunk to match our record.

        Needed because PostgreSQL is the source of truth (spec 10): when a
        tenant edits a trunk, LiveKit has to follow. Deleting and recreating
        would work but changes the LiveKit trunk ID, invalidating every
        dispatch rule that references it.
        """
        info = SIPInboundTrunkInfo(
            sip_trunk_id=livekit_trunk_id,
            name=trunk.name,
            metadata=f"tenant={trunk.tenant_id};sip_trunk={trunk.id}",
            numbers=numbers,
            allowed_addresses=list(trunk.allowed_ips or []),
            auth_username=trunk.auth_username or "",
            auth_password=auth_password or "",
            media_encryption=(
                SIP_MEDIA_ENCRYPT_REQUIRE
                if trunk.media_encryption_required
                else SIP_MEDIA_ENCRYPT_DISABLE
            ),
        )

        updated = await self._client.call(
            "update_sip_inbound_trunk",
            lambda api: api.sip.update_sip_inbound_trunk(livekit_trunk_id, info),
        )

        logger.info(
            "livekit_trunk_updated",
            extra={
                "tenant_id": str(trunk.tenant_id),
                "sip_trunk_id": str(trunk.id),
                "livekit_trunk_id": livekit_trunk_id,
            },
        )
        return _trunk_snapshot(updated)

    def trunk_matches(self, snapshot: TrunkSnapshot, trunk: SipTrunk, numbers: list[str]) -> bool:
        """Whether LiveKit already reflects our record.

        Compares only what we set. LiveKit fills in defaults for everything
        else, and comparing those would report drift on every check.
        """
        return (
            snapshot.name == trunk.name
            and set(snapshot.numbers) == set(numbers)
            and set(snapshot.allowed_addresses) == set(trunk.allowed_ips or [])
            and (snapshot.auth_username or None) == (trunk.auth_username or None)
        )

    async def delete_inbound_trunk(self, livekit_trunk_id: str) -> None:
        await self._client.call(
            "delete_sip_trunk",
            lambda api: api.sip.delete_sip_trunk(
                DeleteSIPTrunkRequest(sip_trunk_id=livekit_trunk_id)
            ),
        )
        logger.info("livekit_trunk_deleted", extra={"livekit_trunk_id": livekit_trunk_id})

    # ------------------------------------------------------------------ #
    # Dispatch rules (spec 21)
    # ------------------------------------------------------------------ #

    async def create_dispatch_rule(
        self, rule: LiveKitDispatchRule, *, livekit_trunk_ids: list[str]
    ) -> DispatchRuleSnapshot:
        """Create the long-lived dispatch rule for one of our rule records.

        The room configuration carries the agent dispatch, which is what makes
        LiveKit hand the call to a worker registered under
        ``agent_dispatch_name``. Without it a call would land in a room with
        nobody to answer.
        """
        # INDIVIDUAL gives every call its own room, which is what a
        # one-caller AI conversation needs (spec 22).
        dispatch_rule = SIPDispatchRule(
            dispatch_rule_individual=SIPDispatchRuleIndividual(
                room_prefix=rule.room_prefix or "",
            )
        )

        request = CreateSIPDispatchRuleRequest(
            name=rule.name,
            metadata=f"tenant={rule.tenant_id};dispatch_rule={rule.id}",
            rule=dispatch_rule,
            trunk_ids=list(livekit_trunk_ids),
            inbound_numbers=list(rule.matched_numbers or []),
            room_config=RoomConfiguration(
                agents=[RoomAgentDispatch(agent_name=rule.agent_dispatch_name)],
            ),
        )

        created = await self._client.call(
            "create_sip_dispatch_rule",
            lambda api: api.sip.create_sip_dispatch_rule(request),
        )

        logger.info(
            "livekit_dispatch_rule_created",
            extra={
                "tenant_id": str(rule.tenant_id),
                "dispatch_rule_id": str(rule.id),
                "livekit_rule_id": created.sip_dispatch_rule_id,
                "agent_dispatch_name": rule.agent_dispatch_name,
            },
        )
        return _dispatch_snapshot(created)

    async def get_dispatch_rule(self, livekit_rule_id: str) -> DispatchRuleSnapshot:
        rules = await self._client.call(
            "list_sip_dispatch_rule",
            lambda api: api.sip.list_sip_dispatch_rule(
                ListSIPDispatchRuleRequest(dispatch_rule_ids=[livekit_rule_id])
            ),
        )
        for item in rules.items:
            if item.sip_dispatch_rule_id == livekit_rule_id:
                return _dispatch_snapshot(item)

        raise LiveKitResourceMissingError(
            f"LiveKit has no dispatch rule {livekit_rule_id}",
            resource_id=livekit_rule_id,
        )

    async def list_dispatch_rules(self) -> list[DispatchRuleSnapshot]:
        rules = await self._client.call(
            "list_sip_dispatch_rule",
            lambda api: api.sip.list_sip_dispatch_rule(ListSIPDispatchRuleRequest()),
        )
        return [_dispatch_snapshot(item) for item in rules.items]

    async def delete_dispatch_rule(self, livekit_rule_id: str) -> None:
        await self._client.call(
            "delete_sip_dispatch_rule",
            lambda api: api.sip.delete_sip_dispatch_rule(
                DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=livekit_rule_id)
            ),
        )
        logger.info("livekit_dispatch_rule_deleted", extra={"livekit_rule_id": livekit_rule_id})


# --------------------------------------------------------------------------- #
# Protobuf to snapshot conversion
# --------------------------------------------------------------------------- #


def _trunk_snapshot(info: SIPInboundTrunkInfo) -> TrunkSnapshot:
    return TrunkSnapshot(
        livekit_trunk_id=info.sip_trunk_id,
        name=info.name,
        numbers=tuple(info.numbers),
        allowed_addresses=tuple(info.allowed_addresses),
        auth_username=info.auth_username or None,
    )


def _dispatch_snapshot(info: SIPDispatchRuleInfo) -> DispatchRuleSnapshot:
    rule = getattr(info, "rule", None)
    individual = getattr(rule, "dispatch_rule_individual", None) if rule else None
    room_config = getattr(info, "room_config", None)

    return DispatchRuleSnapshot(
        livekit_rule_id=info.sip_dispatch_rule_id,
        name=info.name,
        trunk_ids=tuple(info.trunk_ids),
        room_prefix=(individual.room_prefix or None) if individual else None,
        agent_names=tuple(agent.agent_name for agent in getattr(room_config, "agents", []) or []),
    )
