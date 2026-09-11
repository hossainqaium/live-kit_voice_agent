"""Call-time routing engine (spec 20, 37, 38).

Evaluates the tenant's routing rules against the inbound call's attributes and
returns the agent that should answer, or raises an exception when the call
should not be answered at all.

Rule evaluation order
---------------------
1. If the DID has a pinned ``routing_rule_id``, only that rule is evaluated.
2. Otherwise all ACTIVE rules for the tenant are evaluated in ascending
   ``priority`` order.  Within the same priority, the most specific rule (most
   non-null conditions) wins — a broad catch-all cannot shadow a precise rule.
3. The first matching rule determines the outcome:
   a. If the rule references ``business_hours_id``, the schedule is evaluated
      against the current wall-clock time in the schedule's timezone.
      - OPEN  → use ``agent_id`` as the primary target.
      - CLOSED → apply ``closed_action`` (hangup, or secondary agent via
        ``fallback_agent_id`` when the action is SECONDARY_AGENT).
   b. No schedule → always open; use ``agent_id``.
4. If no rule matches, ``None`` is returned so ``CallConfigLoader`` falls back
   to the DID's own ``inbound_agent_id`` (backward-compatible default for
   DIDs that were configured before routing rules existed).

Fallback chain
--------------
When the matched rule's primary agent has no published version the caller may
try a secondary.  ``RoutingDecision.fallback_agent_id`` and
``.fallback_action`` are surfaced to ``CallConfigLoader`` so it can attempt
the fallback without a second database round-trip.

PBX_QUEUE / VOICEMAIL actions at *call setup* time require SIP transfer
infrastructure that is not yet available (Phase 6).  Until then they are
treated identically to HANGUP and logged at warning level so the gap is
visible in the operator's logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.logging import get_logger
from shared.models import DayOfWeek, FallbackAction

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Exception hierarchy
# --------------------------------------------------------------------------- #


class RoutingError(Exception):
    """Base for all routing-engine refusals."""

    def __init__(self, message: str, *, rule_name: str) -> None:
        super().__init__(message)
        self.rule_name = rule_name


class RoutingClosedError(RoutingError):
    """The schedule says the business is closed and the action is to hang up."""


class RoutingHangupError(RoutingError):
    """A routing rule explicitly hangs up this call (HANGUP fallback action)."""


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The agent the engine resolved, plus fallback context.

    ``fallback_agent_id`` and ``fallback_action`` are populated when the
    matched rule defines a secondary agent, so ``CallConfigLoader`` can try it
    without another round-trip if the primary has no published version.
    """

    agent_id: uuid.UUID
    matched_rule_id: uuid.UUID | None = None
    rule_name: str | None = None
    fallback_agent_id: uuid.UUID | None = None
    fallback_action: FallbackAction | None = None


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

_ALL_RULES_SQL = text(
    """
    SELECT
        r.id,
        r.name,
        r.priority,
        r.conditions,
        r.agent_id,
        r.business_hours_id,
        r.fallback_action,
        r.fallback_agent_id,
        r.closed_action
    FROM routing_rules r
    WHERE r.tenant_id = :tenant_id
      AND r.status    = 'ACTIVE'
    ORDER BY r.priority ASC, r.name ASC
    """
)

_PINNED_RULE_SQL = text(
    """
    SELECT
        r.id,
        r.name,
        r.priority,
        r.conditions,
        r.agent_id,
        r.business_hours_id,
        r.fallback_action,
        r.fallback_agent_id,
        r.closed_action
    FROM routing_rules r
    WHERE r.id        = :rule_id
      AND r.tenant_id = :tenant_id
      AND r.status    = 'ACTIVE'
    LIMIT 1
    """
)

_HOURS_SQL = text(
    """
    SELECT
        bh.id        AS id,
        bh.timezone  AS timezone,
        bh.holidays  AS holidays,
        bhi.day_of_week,
        bhi.opens_at,
        bhi.closes_at
    FROM business_hours bh
    LEFT JOIN business_hours_intervals bhi
           ON bhi.business_hours_id = bh.id
    WHERE bh.id = ANY(:ids)
    ORDER BY bh.id, bhi.day_of_week, bhi.opens_at
    """
)


# --------------------------------------------------------------------------- #
# Business-hours evaluation
# --------------------------------------------------------------------------- #

# Ordered Monday-first so ``now.weekday()`` indexes directly into this list.
_WEEKDAY_ORDER: list[DayOfWeek] = [
    DayOfWeek.MONDAY,
    DayOfWeek.TUESDAY,
    DayOfWeek.WEDNESDAY,
    DayOfWeek.THURSDAY,
    DayOfWeek.FRIDAY,
    DayOfWeek.SATURDAY,
    DayOfWeek.SUNDAY,
]


def _check_open_now(
    intervals: list[tuple[str, time, time]],
    timezone_name: str,
    holidays: list,
) -> bool | None:
    """Whether the schedule says open right now, in the schedule's timezone.

    ``intervals`` is a list of ``(day_of_week_value, opens_at, closes_at)``
    tuples as returned from SQL.

    Returns ``None`` when the timezone string is unrecognisable — this is
    treated as "open" by the caller so that a typo in a timezone name does not
    silently close a business.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("business_hours_unknown_timezone", extra={"timezone": timezone_name})
        return None

    now = datetime.now(UTC).astimezone(zone)
    today = now.date().isoformat()

    for holiday in holidays or []:
        if isinstance(holiday, dict) and holiday.get("date") == today:
            return not bool(holiday.get("closed", True))

    weekday = _WEEKDAY_ORDER[now.weekday()]
    current = now.time()
    return any(
        dow == weekday.value and opens <= current < closes
        for dow, opens, closes in intervals
    )


async def _load_hours_map(
    session: AsyncSession,
    hours_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[list[tuple[str, time, time]], str, list]]:
    """Fetch business-hours records in one query.

    Returns ``{business_hours.id → (intervals, timezone, holidays)}``.
    """
    if not hours_ids:
        return {}

    rows = (await session.execute(_HOURS_SQL, {"ids": hours_ids})).mappings().all()

    result: dict[uuid.UUID, tuple[list[tuple[str, time, time]], str, list]] = {}
    for row in rows:
        hid = row["id"]
        if hid not in result:
            result[hid] = ([], row["timezone"] or "UTC", list(row["holidays"] or []))
        if row["day_of_week"] is not None:
            result[hid][0].append((row["day_of_week"], row["opens_at"], row["closes_at"]))
    return result


# --------------------------------------------------------------------------- #
# Condition matching
# --------------------------------------------------------------------------- #


def _specificity(conditions: dict) -> int:
    """Count non-null conditions; used for tie-breaking within the same priority."""
    return sum(1 for v in conditions.values() if v is not None)


def _matches(
    conditions: dict,
    *,
    did: str,
    caller_number: str | None,
    pbx_id: uuid.UUID | None,
    sip_trunk_id: uuid.UUID | None,
) -> bool:
    """Return True iff all non-null conditions in the rule match this call.

    UUID comparisons use string form because JSONB serialises UUIDs as strings.
    """
    if (cond := conditions.get("pbx_id")) is not None:
        if pbx_id is None or str(pbx_id) != cond:
            return False
    if (cond := conditions.get("sip_trunk_id")) is not None:
        if sip_trunk_id is None or str(sip_trunk_id) != cond:
            return False
    if (cond := conditions.get("did")) is not None:
        if did != cond:
            return False
    if (cond := conditions.get("destination_number")) is not None:
        if did != cond:
            return False
    if (cond := conditions.get("caller_number")) is not None:
        if caller_number != cond:
            return False
    if (cond := conditions.get("caller_number_prefix")) is not None:
        if not (caller_number or "").startswith(cond):
            return False
    # campaign: sourced from SIP headers not yet plumbed through; skipped silently.
    return True


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def resolve_route(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tenant_timezone: str,
    did: str,
    caller_number: str | None,
    pbx_id: uuid.UUID | None,
    sip_trunk_id: uuid.UUID | None,
    pinned_rule_id: uuid.UUID | None,
) -> RoutingDecision | None:
    """Evaluate routing rules and return the agent that should answer.

    Parameters
    ----------
    session:
        Active async SQLAlchemy session (read-only within this function).
    tenant_id:
        The tenant whose rules are evaluated.
    tenant_timezone:
        IANA timezone name from the tenant row; used as a fallback when a
        business-hours schedule does not carry its own timezone.
    did:
        The E.164 number that was called (from SIP attributes).
    caller_number:
        The originating number, or None when unavailable (anonymous callers).
    pbx_id:
        The PBX the DID is associated with, for condition matching.
    sip_trunk_id:
        The SIP trunk the call arrived on, for condition matching.
    pinned_rule_id:
        When the DID has an explicit ``routing_rule_id``, only that rule is
        evaluated.  When None, all tenant rules are evaluated in priority order.

    Returns
    -------
    RoutingDecision
        The resolved agent.
    None
        No routing rules are configured; the caller should fall back to the
        DID's ``inbound_agent_id`` for backward compatibility.

    Raises
    ------
    RoutingClosedError
        The business is closed and the ``closed_action`` is HANGUP (or a
        transfer action not yet supported at call-setup time).
    RoutingHangupError
        A matching rule's ``fallback_action`` is HANGUP and its primary agent
        is unavailable — but this is only raised here when the rule has no
        published agent at all; the actual unavailability check happens in
        ``CallConfigLoader`` which can attempt the fallback agent first.
    """
    if pinned_rule_id is not None:
        rules = (
            (
                await session.execute(
                    _PINNED_RULE_SQL,
                    {"rule_id": pinned_rule_id, "tenant_id": tenant_id},
                )
            )
            .mappings()
            .all()
        )
        if not rules:
            logger.warning(
                "pinned_routing_rule_inactive_or_missing",
                extra={"rule_id": str(pinned_rule_id), "did": did},
            )
            return None
    else:
        rules = (
            (await session.execute(_ALL_RULES_SQL, {"tenant_id": tenant_id})).mappings().all()
        )
        if not rules:
            # No rules at all — preserve legacy direct-assignment behaviour.
            return None

    # Fetch all referenced business-hours schedules in one query.
    hours_ids = list({r["business_hours_id"] for r in rules if r["business_hours_id"] is not None})
    hours_map = await _load_hours_map(session, hours_ids)

    # Re-sort in Python to enforce the specificity tie-break within each
    # priority bucket.  The DB ORDER BY gives us the primary sort (priority
    # ASC, name ASC) cheaply; we refine it here without touching the index.
    sorted_rules = sorted(
        rules,
        key=lambda r: (r["priority"], -_specificity(r["conditions"] or {})),
    )

    for rule in sorted_rules:
        conditions: dict = rule["conditions"] or {}

        if not _matches(
            conditions,
            did=did,
            caller_number=caller_number,
            pbx_id=pbx_id,
            sip_trunk_id=sip_trunk_id,
        ):
            continue

        rule_id: uuid.UUID = rule["id"]
        rule_name: str = rule["name"]
        fallback_action = (
            FallbackAction(rule["fallback_action"]) if rule["fallback_action"] else None
        )
        fallback_agent_id: uuid.UUID | None = rule["fallback_agent_id"]
        closed_action = (
            FallbackAction(rule["closed_action"]) if rule["closed_action"] else None
        )

        # ---- Business-hours gate ---- #
        hours_id: uuid.UUID | None = rule["business_hours_id"]
        if hours_id is not None:
            if hours_id in hours_map:
                intervals, tz, holidays = hours_map[hours_id]
                open_now = _check_open_now(intervals, tz or tenant_timezone, holidays)
            else:
                open_now = None  # schedule not found — treat as open

            if open_now is False:
                _log_closed(rule_name, did, closed_action)

                if closed_action == FallbackAction.SECONDARY_AGENT and fallback_agent_id:
                    # Route to the secondary agent during closed hours.
                    logger.info(
                        "routing_closed_secondary_agent",
                        extra={"rule": rule_name, "did": did},
                    )
                    return RoutingDecision(
                        agent_id=fallback_agent_id,
                        matched_rule_id=rule_id,
                        rule_name=rule_name,
                        # The secondary IS now the primary; expose no further fallback.
                    )

                # HANGUP, PBX_QUEUE, VOICEMAIL, or no closed_action → refuse the call.
                if closed_action in (
                    FallbackAction.PBX_QUEUE,
                    FallbackAction.VOICEMAIL,
                ):
                    logger.warning(
                        "routing_closed_transfer_not_yet_supported",
                        extra={
                            "rule": rule_name,
                            "closed_action": closed_action.value,
                            "note": "treating as HANGUP until Phase 6 transfer is available",
                        },
                    )
                raise RoutingClosedError(
                    f"business is closed per schedule on rule {rule_name!r} "
                    f"(closed_action={closed_action.value if closed_action else 'HANGUP'})",
                    rule_name=rule_name,
                )
        # ---- Rule matched, business open ---- #

        if rule["agent_id"] is None:
            # Misconfigured rule — skip rather than crash; the next rule may
            # be correctly configured.
            logger.warning(
                "routing_rule_skipped_no_agent",
                extra={"rule": rule_name, "priority": rule["priority"]},
            )
            continue

        logger.info(
            "routing_rule_matched",
            extra={
                "rule": rule_name,
                "priority": rule["priority"],
                "did": did,
                "agent_id": str(rule["agent_id"]),
            },
        )
        return RoutingDecision(
            agent_id=rule["agent_id"],
            matched_rule_id=rule_id,
            rule_name=rule_name,
            fallback_agent_id=fallback_agent_id,
            fallback_action=fallback_action,
        )

    # No rule matched.
    logger.debug("routing_no_rule_matched", extra={"did": did, "tenant_id": str(tenant_id)})
    return None


def _log_closed(rule_name: str, did: str, closed_action: FallbackAction | None) -> None:
    logger.info(
        "routing_closed",
        extra={
            "rule": rule_name,
            "did": did,
            "closed_action": closed_action.value if closed_action else "none",
        },
    )
