"""Platform fleet probes for the capacity dashboard (spec 48).

Every figure is scraped or counted. Missing probes stay null — inventing a
CPU percentage the process does not export would be worse than a dash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from shared.logging import get_logger
from shared.models import ResourceStatus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import Provider, ProviderCredential

logger = get_logger(__name__)

_PROBE_TIMEOUT_SECONDS = 1.5

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"')


@dataclass(frozen=True, slots=True)
class PrometheusSample:
    name: str
    labels: dict[str, str]
    value: float


@dataclass(slots=True)
class ResourceSample:
    source: str
    value: float | None
    unit: str
    detail: str | None = None


@dataclass(slots=True)
class ProviderHealth:
    slug: str
    kind: str
    display_name: str
    status: str
    credentialed_tenants: int
    detail: str | None = None


@dataclass(slots=True)
class FleetSnapshot:
    livekit_nodes: int | None = None
    sip_nodes: int | None = None
    ai_workers: int | None = None
    worker_utilization: float | None = None
    worker_capacity: int | None = None
    worker_active_calls: int | None = None
    cpu: ResourceSample | None = None
    memory: ResourceSample | None = None
    network: ResourceSample | None = None
    providers: list[ProviderHealth] = field(default_factory=list)
    detail: str | None = None


def parse_prometheus_text(body: str) -> list[PrometheusSample]:
    """Parse Prometheus text exposition. Comments and TYPE lines are ignored."""
    samples: list[PrometheusSample] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        labels: dict[str, str] = {}
        blob = match.group("labels") or ""
        for key, value in _LABEL_RE.findall(blob):
            labels[key] = value.replace(r"\"", '"')
        samples.append(
            PrometheusSample(
                name=match.group("name"),
                labels=labels,
                value=float(match.group("value")),
            )
        )
    return samples


def samples_named(samples: list[PrometheusSample], name: str) -> list[PrometheusSample]:
    return [item for item in samples if item.name == name]


def unique_label_count(samples: list[PrometheusSample], name: str, label: str) -> int:
    values = {item.labels.get(label) for item in samples_named(samples, name) if label in item.labels}
    return len(values)


def gauge_value(samples: list[PrometheusSample], name: str) -> float | None:
    found = samples_named(samples, name)
    if not found:
        return None
    return found[0].value


async def collect_fleet(
    session: AsyncSession, *, livekit_reachable: bool
) -> FleetSnapshot:
    settings = get_settings()
    snapshot = FleetSnapshot()

    worker_metrics: list[PrometheusSample] = []
    livekit_metrics: list[PrometheusSample] = []
    sip_metrics: list[PrometheusSample] = []

    worker_ready = await _probe_workers(settings.worker_health_urls)
    snapshot.ai_workers = worker_ready.responding
    snapshot.worker_capacity = worker_ready.capacity
    snapshot.worker_active_calls = worker_ready.active_calls
    snapshot.worker_utilization = worker_ready.utilization

    for url in settings.worker_health_urls:
        worker_metrics.extend(await _scrape_metrics(f"{url.rstrip('/')}/metrics"))

    if settings.livekit_metrics_url:
        livekit_metrics = await _scrape_metrics(settings.livekit_metrics_url)
    if settings.sip_metrics_url:
        sip_metrics = await _scrape_metrics(settings.sip_metrics_url)

    snapshot.livekit_nodes = _count_nodes(
        livekit_metrics, reachable=livekit_reachable, fallback=1 if livekit_reachable else 0
    )
    snapshot.sip_nodes = _sip_node_count(sip_metrics, sip_uri=settings.livekit_sip_uri)

    utilization = gauge_value(worker_metrics, "voice_worker_utilization_ratio")
    if utilization is not None:
        snapshot.worker_utilization = utilization

    snapshot.memory = _memory_sample(worker_metrics, livekit_metrics)
    snapshot.cpu = _cpu_sample(worker_metrics, livekit_metrics)
    snapshot.network = _network_sample(worker_metrics, livekit_metrics)
    snapshot.providers = await _provider_health(session, worker_metrics)
    return snapshot


@dataclass(slots=True)
class _WorkerReady:
    responding: int = 0
    active_calls: int | None = None
    capacity: int | None = None
    utilization: float | None = None


async def _probe_workers(urls: list[str]) -> _WorkerReady:
    result = _WorkerReady()
    if not urls:
        return result
    active = 0
    capacity = 0
    saw_numbers = False
    for url in urls:
        payload = await _get_json(f"{url.rstrip('/')}/ready")
        if payload is None:
            payload = await _get_json(f"{url.rstrip('/')}/health")
            if payload is None:
                continue
        result.responding += 1
        if "active_calls" in payload and "capacity" in payload:
            try:
                active += int(payload["active_calls"])
                capacity += int(payload["capacity"])
                saw_numbers = True
            except (TypeError, ValueError):
                continue
    if saw_numbers:
        result.active_calls = active
        result.capacity = capacity
        result.utilization = (active / capacity) if capacity > 0 else 0.0
    return result


def _count_nodes(
    samples: list[PrometheusSample], *, reachable: bool, fallback: int
) -> int | None:
    for metric, label in (
        ("livekit_node_available", "node_id"),
        ("livekit_node_cpu", "node_id"),
        ("livekit_sys_node_cpu", "node_id"),
    ):
        count = unique_label_count(samples, metric, label)
        if count:
            return count
    any_node = {item.labels.get("node_id") for item in samples if "node_id" in item.labels}
    any_node.discard(None)
    if any_node:
        return len(any_node)
    if samples:
        return 1
    return fallback


def _sip_node_count(samples: list[PrometheusSample], *, sip_uri: str) -> int | None:
    if samples:
        count = unique_label_count(samples, samples[0].name, "node_id")
        if count:
            return count
        return 1
    if sip_uri.strip():
        return 1
    return 0


def _memory_sample(
    worker: list[PrometheusSample], livekit: list[PrometheusSample]
) -> ResourceSample:
    total = 0.0
    sources: list[str] = []
    for name, samples, label in (
        ("worker", worker, "process_resident_memory_bytes"),
        ("livekit", livekit, "process_resident_memory_bytes"),
    ):
        value = gauge_value(samples, label)
        if value is not None:
            total += value
            sources.append(name)
    if sources:
        return ResourceSample(
            source="+".join(sources),
            value=total,
            unit="bytes",
            detail="process_resident_memory_bytes",
        )
    return ResourceSample(
        source="none",
        value=None,
        unit="bytes",
        detail="process_resident_memory_bytes was not scraped",
    )


def _cpu_sample(
    worker: list[PrometheusSample], livekit: list[PrometheusSample]
) -> ResourceSample:
    for samples, metric in (
        (worker, "process_cpu_seconds_total"),
        (livekit, "process_cpu_seconds_total"),
    ):
        value = gauge_value(samples, metric)
        if value is not None:
            return ResourceSample(
                source="prometheus",
                value=None,
                unit="ratio",
                detail=(
                    f"{metric} is a counter ({value:.1f}s cumulative), not an "
                    "instant utilisation gauge"
                ),
            )
    return ResourceSample(
        source="none",
        value=None,
        unit="ratio",
        detail="no instant CPU gauge was scraped",
    )


def _network_sample(
    worker: list[PrometheusSample], livekit: list[PrometheusSample]
) -> ResourceSample:
    for samples, metric in (
        (livekit, "livekit_packet_rx"),
        (livekit, "livekit_sys_packet_rx"),
        (worker, "process_network_receive_bytes_total"),
    ):
        value = gauge_value(samples, metric)
        if value is not None:
            return ResourceSample(source="prometheus", value=value, unit="bytes", detail=metric)
    return ResourceSample(
        source="none",
        value=None,
        unit="bytes",
        detail="no network gauge was scraped",
    )


async def _provider_health(
    session: AsyncSession, worker_metrics: list[PrometheusSample]
) -> list[ProviderHealth]:
    rows = (
        await session.execute(
            select(
                Provider,
                func.count(ProviderCredential.id),
            )
            .outerjoin(ProviderCredential, ProviderCredential.provider_id == Provider.id)
            .group_by(Provider.id)
            .order_by(Provider.kind, Provider.slug)
        )
    ).all()

    circuits: dict[tuple[str, str], float] = {}
    for sample in samples_named(worker_metrics, "voice_provider_circuit_state"):
        kind = sample.labels.get("kind", "")
        provider = sample.labels.get("provider", "")
        circuits[(kind, provider)] = sample.value

    out: list[ProviderHealth] = []
    for provider, credential_count in rows:
        count = int(credential_count)
        circuit = circuits.get((provider.kind.value, provider.slug))
        if circuit == 2:
            status = "circuit_open"
            detail = "worker circuit breaker is open"
        elif provider.status is not ResourceStatus.ACTIVE:
            status = "disabled"
            detail = None
        elif provider.requires_credential and count == 0:
            status = "missing_credential"
            detail = "no tenant has stored a key"
        else:
            status = "healthy"
            detail = None
        out.append(
            ProviderHealth(
                slug=provider.slug,
                kind=provider.kind.value,
                display_name=provider.display_name,
                status=status,
                credentialed_tenants=count,
                detail=detail,
            )
        )
    return out


async def _get_json(url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("fleet_probe_failed", extra={"url": url, "reason": str(exc)[:200]})
        return None
    if response.status_code >= 500 and response.status_code != 503:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def _scrape_metrics(url: str) -> list[PrometheusSample]:
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("fleet_metrics_failed", extra={"url": url, "reason": str(exc)[:200]})
        return []
    return parse_prometheus_text(response.text)
