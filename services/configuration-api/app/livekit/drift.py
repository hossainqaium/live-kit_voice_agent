"""Scheduled LiveKit drift detection (spec 46, Plan 5.7).

PostgreSQL is the source of truth (spec 10, 12). This module *compares* — it
never creates, updates, or deletes a LiveKit resource. Repair stays a
deliberate operator action (``sync-livekit`` / tenant Synchronize).

Two directions, because a per-row GET only sees one of them:

* **Missing / mismatch** — we recorded a LiveKit ID that is gone or differs.
  The row is marked ``DRIFTED`` so Repair can recreate it from PostgreSQL.
* **Orphan** — LiveKit holds a resource no row names. Interrupted
  delete-and-recreate cycles leave these behind. They have no row to mark, so
  they live on the last report until an operator decides what to do.

A LiveKit outage is not drift. If the list call fails, statuses are left
alone and the report says the check could not run.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from shared.logging import get_logger
from shared.models import SyncStatus
from shared.telemetry.metrics import ControlPlaneMetrics
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveKitDispatchRule, PhoneNumber, SipTrunk
from app.livekit.errors import LiveKitError
from app.livekit.sip import DispatchRuleSnapshot, SipResourceManager, TrunkSnapshot

logger = get_logger(__name__)

ResourceKind = Literal["sip_trunk", "dispatch_rule"]


class DriftKind(StrEnum):
    MISSING = "missing_in_livekit"
    MISMATCH = "mismatch"
    ORPHAN = "orphan"


@dataclass(frozen=True, slots=True)
class DriftFinding:
    resource: ResourceKind
    kind: DriftKind
    name: str
    reason: str
    row_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    livekit_resource_id: str | None = None


@dataclass
class DriftReport:
    """Outcome of one compare pass.

    Held in process memory for the overview API. Row-level ``DRIFTED`` status
    is persisted, so a restart still shows disagreement even before the next
    scheduled run. Orphans are only on this report — they have no row.
    """

    checked_at: datetime
    livekit_reachable: bool
    trunks_compared: int
    rules_compared: int
    findings: list[DriftFinding] = field(default_factory=list)
    error: str | None = None

    @property
    def drifted_findings(self) -> list[DriftFinding]:
        return [item for item in self.findings if item.kind is not DriftKind.ORPHAN]

    @property
    def orphans(self) -> list[DriftFinding]:
        return [item for item in self.findings if item.kind is DriftKind.ORPHAN]

    @property
    def configuration_drift_detected(self) -> bool:
        return bool(self.findings)


_LAST_REPORT: DriftReport | None = None
_RUN_LOCK: asyncio.Lock | None = None


def last_report() -> DriftReport | None:
    return _LAST_REPORT


def store_last_report(report: DriftReport) -> None:
    global _LAST_REPORT
    _LAST_REPORT = report


def reset_last_report() -> None:
    """Test helper — the process-wide last report would otherwise leak."""
    global _LAST_REPORT
    _LAST_REPORT = None


def _run_lock() -> asyncio.Lock:
    global _RUN_LOCK
    if _RUN_LOCK is None:
        _RUN_LOCK = asyncio.Lock()
    return _RUN_LOCK


def compare_trunks(
    manager: SipResourceManager,
    rows: list[SipTrunk],
    numbers_by_trunk: dict[uuid.UUID, list[str]],
    snapshots: list[TrunkSnapshot],
) -> list[DriftFinding]:
    """List-and-compare inbound trunks in both directions."""
    live = {item.livekit_trunk_id: item for item in snapshots}
    ours = {
        row.livekit_resource_id: row
        for row in rows
        if row.livekit_resource_id
    }
    findings: list[DriftFinding] = []

    for row in rows:
        if not row.livekit_resource_id:
            continue
        snapshot = live.get(row.livekit_resource_id)
        numbers = numbers_by_trunk.get(row.id, [])
        if snapshot is None:
            findings.append(
                DriftFinding(
                    resource="sip_trunk",
                    kind=DriftKind.MISSING,
                    name=row.name,
                    reason=f"LiveKit has no inbound trunk {row.livekit_resource_id}",
                    row_id=row.id,
                    tenant_id=row.tenant_id,
                    livekit_resource_id=row.livekit_resource_id,
                )
            )
            continue
        if not manager.trunk_matches(snapshot, row, numbers):
            findings.append(
                DriftFinding(
                    resource="sip_trunk",
                    kind=DriftKind.MISMATCH,
                    name=row.name,
                    reason=_trunk_mismatch_reason(snapshot, row, numbers),
                    row_id=row.id,
                    tenant_id=row.tenant_id,
                    livekit_resource_id=row.livekit_resource_id,
                )
            )

    for snapshot in snapshots:
        if snapshot.livekit_trunk_id not in ours:
            findings.append(
                DriftFinding(
                    resource="sip_trunk",
                    kind=DriftKind.ORPHAN,
                    name=snapshot.name,
                    reason=(
                        "LiveKit holds inbound trunk "
                        f"{snapshot.livekit_trunk_id} that no PostgreSQL row names"
                    ),
                    livekit_resource_id=snapshot.livekit_trunk_id,
                )
            )

    return findings


def compare_rules(
    manager: SipResourceManager,
    rows: list[LiveKitDispatchRule],
    trunk_ids_by_rule: dict[uuid.UUID, list[str]],
    snapshots: list[DispatchRuleSnapshot],
) -> list[DriftFinding]:
    """List-and-compare dispatch rules in both directions."""
    live = {item.livekit_rule_id: item for item in snapshots}
    ours = {
        row.livekit_resource_id: row
        for row in rows
        if row.livekit_resource_id
    }
    findings: list[DriftFinding] = []

    for row in rows:
        if not row.livekit_resource_id:
            continue
        snapshot = live.get(row.livekit_resource_id)
        trunk_ids = trunk_ids_by_rule.get(row.id, [])
        if snapshot is None:
            findings.append(
                DriftFinding(
                    resource="dispatch_rule",
                    kind=DriftKind.MISSING,
                    name=row.name,
                    reason=f"LiveKit has no dispatch rule {row.livekit_resource_id}",
                    row_id=row.id,
                    tenant_id=row.tenant_id,
                    livekit_resource_id=row.livekit_resource_id,
                )
            )
            continue
        if not manager.rule_matches(snapshot, row, trunk_ids):
            findings.append(
                DriftFinding(
                    resource="dispatch_rule",
                    kind=DriftKind.MISMATCH,
                    name=row.name,
                    reason=_rule_mismatch_reason(snapshot, row, trunk_ids),
                    row_id=row.id,
                    tenant_id=row.tenant_id,
                    livekit_resource_id=row.livekit_resource_id,
                )
            )

    for snapshot in snapshots:
        if snapshot.livekit_rule_id not in ours:
            findings.append(
                DriftFinding(
                    resource="dispatch_rule",
                    kind=DriftKind.ORPHAN,
                    name=snapshot.name,
                    reason=(
                        "LiveKit holds dispatch rule "
                        f"{snapshot.livekit_rule_id} that no PostgreSQL row names"
                    ),
                    livekit_resource_id=snapshot.livekit_rule_id,
                )
            )

    return findings


def apply_findings(
    rows: list[SipTrunk | LiveKitDispatchRule],
    findings: list[DriftFinding],
    *,
    now: datetime,
) -> None:
    """Write ``DRIFTED`` / recover ``SYNCED`` on rows the compare covered.

    Rows with no LiveKit ID are left alone — they are ``PENDING`` or
    ``FAILED``, which is a sync problem, not drift. Orphans have no row.
    """
    drifted_ids = {
        item.row_id: item
        for item in findings
        if item.row_id is not None and item.kind is not DriftKind.ORPHAN
    }
    for row in rows:
        if not row.livekit_resource_id:
            continue
        finding = drifted_ids.get(row.id)
        if finding is not None:
            row.sync_status = SyncStatus.DRIFTED
            row.sync_error = finding.reason
            continue
        row.sync_status = SyncStatus.SYNCED
        row.sync_error = None
        row.last_synced_at = now


def publish_metrics(report: DriftReport, metrics: ControlPlaneMetrics) -> None:
    """Set the drift gauges to the current counts, including zero.

    A gauge that is only incremented would stay high after Repair. Setting
    every label each run is what makes a healthy platform report 0.
    """
    by_resource = {"sip_trunk": 0, "dispatch_rule": 0}
    orphan_count = 0
    for item in report.findings:
        if item.kind is DriftKind.ORPHAN:
            orphan_count += 1
        else:
            by_resource[item.resource] = by_resource.get(item.resource, 0) + 1
    for resource, count in by_resource.items():
        metrics.livekit_drift_detected.labels(resource=resource).set(count)
    metrics.livekit_drift_detected.labels(resource="orphan").set(orphan_count)


async def detect_drift(
    session: AsyncSession,
    manager: SipResourceManager | None = None,
    *,
    now: datetime | None = None,
    metrics: ControlPlaneMetrics | None = None,
) -> DriftReport:
    """Compare PostgreSQL against LiveKit and persist row-level drift.

    Does not commit — the caller owns the transaction so an API audit row
    and the status writes land together.
    """
    async with _run_lock():
        report = await _detect_unlocked(session, manager, now=now)
        if metrics is not None:
            publish_metrics(report, metrics)
        store_last_report(report)
        return report


async def _detect_unlocked(
    session: AsyncSession,
    manager: SipResourceManager | None,
    *,
    now: datetime | None,
) -> DriftReport:
    checked_at = now or datetime.now(UTC)
    manager = manager or SipResourceManager()

    try:
        live_trunks = await manager.list_inbound_trunks()
        live_rules = await manager.list_dispatch_rules()
    except LiveKitError as exc:
        logger.warning("livekit_drift_check_unreachable", extra={"error": str(exc)})
        return DriftReport(
            checked_at=checked_at,
            livekit_reachable=False,
            trunks_compared=0,
            rules_compared=0,
            error=str(exc),
        )

    trunks = list((await session.execute(select(SipTrunk))).scalars().all())
    rules = list((await session.execute(select(LiveKitDispatchRule))).scalars().all())
    numbers_by_trunk = await _numbers_by_trunk(session)
    trunk_ids_by_rule = _trunk_ids_by_rule(rules, trunks)

    findings = compare_trunks(manager, trunks, numbers_by_trunk, live_trunks)
    findings.extend(compare_rules(manager, rules, trunk_ids_by_rule, live_rules))
    apply_findings([*trunks, *rules], findings, now=checked_at)
    await session.flush()

    report = DriftReport(
        checked_at=checked_at,
        livekit_reachable=True,
        trunks_compared=sum(1 for row in trunks if row.livekit_resource_id),
        rules_compared=sum(1 for row in rules if row.livekit_resource_id),
        findings=findings,
    )
    logger.info(
        "livekit_drift_check_completed",
        extra={
            "trunks_compared": report.trunks_compared,
            "rules_compared": report.rules_compared,
            "drifted": len(report.drifted_findings),
            "orphans": len(report.orphans),
        },
    )
    return report


async def _numbers_by_trunk(session: AsyncSession) -> dict[uuid.UUID, list[str]]:
    rows = (
        await session.execute(
            select(PhoneNumber.sip_trunk_id, PhoneNumber.number).where(
                PhoneNumber.sip_trunk_id.is_not(None)
            )
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for trunk_id, number in rows:
        if trunk_id is None:
            continue
        grouped.setdefault(trunk_id, []).append(number)
    return grouped


def _trunk_ids_by_rule(
    rules: list[LiveKitDispatchRule], trunks: list[SipTrunk]
) -> dict[uuid.UUID, list[str]]:
    by_id = {row.id: row for row in trunks}
    mapped: dict[uuid.UUID, list[str]] = {}
    for rule in rules:
        attached = by_id.get(rule.sip_trunk_id) if rule.sip_trunk_id else None
        if attached is not None and attached.livekit_resource_id:
            mapped[rule.id] = [attached.livekit_resource_id]
        else:
            mapped[rule.id] = []
    return mapped


def _trunk_mismatch_reason(snapshot: TrunkSnapshot, row: SipTrunk, numbers: list[str]) -> str:
    parts: list[str] = []
    if snapshot.name != row.name:
        parts.append(f"name {snapshot.name!r} != {row.name!r}")
    if set(snapshot.numbers) != set(numbers):
        parts.append(f"numbers {list(snapshot.numbers)} != {numbers}")
    if set(snapshot.allowed_addresses) != set(row.allowed_ips or []):
        parts.append(
            f"allowed_addresses {list(snapshot.allowed_addresses)} != {list(row.allowed_ips or [])}"
        )
    if (snapshot.auth_username or None) != (row.auth_username or None):
        parts.append("auth_username differs")
    return "LiveKit inbound trunk differs: " + "; ".join(parts or ["fields differ"])


def _rule_mismatch_reason(
    snapshot: DispatchRuleSnapshot, row: LiveKitDispatchRule, trunk_ids: list[str]
) -> str:
    parts: list[str] = []
    if snapshot.name != row.name:
        parts.append(f"name {snapshot.name!r} != {row.name!r}")
    if set(snapshot.trunk_ids) != set(trunk_ids):
        parts.append(f"trunk_ids {list(snapshot.trunk_ids)} != {trunk_ids}")
    if (snapshot.room_prefix or None) != (row.room_prefix or None):
        parts.append(f"room_prefix {snapshot.room_prefix!r} != {row.room_prefix!r}")
    if set(snapshot.agent_names) != {row.agent_dispatch_name}:
        parts.append(f"agents {list(snapshot.agent_names)} != [{row.agent_dispatch_name}]")
    if set(snapshot.inbound_numbers) != set(row.matched_numbers or []):
        parts.append(
            f"inbound_numbers {list(snapshot.inbound_numbers)} != {list(row.matched_numbers or [])}"
        )
    return "LiveKit dispatch rule differs: " + "; ".join(parts or ["fields differ"])
