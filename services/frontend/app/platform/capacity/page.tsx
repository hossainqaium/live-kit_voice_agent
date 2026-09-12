"use client";

/**
 * Platform capacity (spec 48).
 *
 * Inventory is counted in PostgreSQL. Fleet figures are probed from worker
 * /ready and Prometheus metrics. A probe that does not answer shows a dash
 * rather than a guessed percentage.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice } from "@/components/ui";
import { api, type Capacity, type ResourceUsage } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

export default function CapacityPage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [data, setData] = useState<Capacity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setData(await api.platform.capacity());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load capacity");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const used = data?.total_capacity
    ? Math.min(1, data.active_calls / data.total_capacity)
    : data?.licensed_concurrent_calls
      ? Math.min(1, data.active_calls / data.licensed_concurrent_calls)
      : null;

  /**
   * LiveKit's room count and the database's in-flight call count should agree.
   * When they do not, the usual cause is a call that ended without the worker
   * writing its terminal state, which is exactly the drift worth surfacing.
   */
  const mismatch =
    data !== null &&
    data.livekit_rooms !== null &&
    data.livekit_rooms !== data.active_calls;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Capacity</h1>
          <p className="page-subtitle">
            Total and available concurrency, LiveKit / SIP / worker nodes, and
            provider health — measured when this page loads.
          </p>
        </div>
        <Button size="sm" busy={busy} onClick={() => void load()}>Refresh</Button>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {data === null ? (
        <Loading label="Measuring…" />
      ) : (
        <div className="stack">
          {mismatch && (
            <Notice tone="warn">
              LiveKit reports {data.livekit_rooms} room
              {data.livekit_rooms === 1 ? "" : "s"} while the database shows{" "}
              {data.active_calls} call{data.active_calls === 1 ? "" : "s"} in
              flight. They should agree. The usual cause is a call that ended
              without the worker writing its terminal state, so the row is
              holding a concurrency slot it no longer needs.
            </Notice>
          )}

          <div className="card-grid">
            <div className="card">
              <div className="stat-label">Total Capacity</div>
              <div className="stat-value">{data.total_capacity ?? "—"}</div>
              <div className="stat-note">
                {data.licensed_concurrent_calls === null
                  ? "worker /ready capacity (a tenant is uncapped)"
                  : "sum of tenant licensed concurrent calls"}
              </div>
              {used !== null && (
                <div className={used >= 0.9 ? "bar bar-err" : used >= 0.7 ? "bar bar-warn" : "bar"}
                  style={{ marginTop: 8 }}>
                  <span style={{ width: `${used * 100}%` }} />
                </div>
              )}
            </div>
            <div className="card">
              <div className="stat-label">Available Capacity</div>
              <div className="stat-value">{data.available_capacity ?? "—"}</div>
              <div className="stat-note">{data.active_calls} calls in flight</div>
            </div>
            <div className="card">
              <div className="stat-label">LiveKit Nodes</div>
              <div className="stat-value">{data.livekit_nodes ?? "—"}</div>
              <div className="stat-note">
                {data.livekit_rooms === null ? "LiveKit did not answer" : `${data.livekit_rooms} rooms`}
              </div>
            </div>
            <div className="card">
              <div className="stat-label">SIP Nodes</div>
              <div className="stat-value">{data.sip_nodes ?? "—"}</div>
              <div className="stat-note">scraped metrics, or one configured SIP URI</div>
            </div>
            <div className="card">
              <div className="stat-label">AI Workers</div>
              <div className="stat-value">{data.ai_workers ?? "—"}</div>
              <div className="stat-note">workers that answered /ready</div>
            </div>
            <div className="card">
              <div className="stat-label">Worker Utilization</div>
              <div className="stat-value">
                {data.worker_utilization === null
                  ? "—"
                  : `${Math.round(data.worker_utilization * 100)}%`}
              </div>
              <div className="stat-note">active calls ÷ worker capacity</div>
            </div>
            <UsageCard label="CPU" sample={data.cpu} />
            <UsageCard label="Memory" sample={data.memory} />
            <UsageCard label="Network" sample={data.network} />
            <div className="card">
              <div className="stat-label">Calls today</div>
              <div className="stat-value">{data.calls_last_24h}</div>
              <div className="stat-note">last 24 hours, every tenant</div>
            </div>
            <div className="card">
              <div className="stat-label">Tenants</div>
              <div className="stat-value">{data.tenant_count}</div>
              <div className="stat-note">{data.active_tenant_count} active</div>
            </div>
            <div className="card">
              <div className="stat-label">Agents</div>
              <div className="stat-value">{data.agent_count}</div>
              <div className="stat-note">{data.published_agent_count} with a published version</div>
            </div>
          </div>

          <div className="card">
            <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>Provider Health</h2>
            {data.providers.length === 0 ? (
              <p className="subtle small" style={{ margin: 0 }}>No providers in the catalog.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Provider</th><th>Kind</th><th>State</th><th>Credentials</th><th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {data.providers.map((provider) => (
                    <tr key={`${provider.kind}-${provider.slug}`}>
                      <td>{provider.display_name}</td>
                      <td className="mono small">{provider.kind}</td>
                      <td><ProviderBadge status={provider.status} /></td>
                      <td className="mono small">{provider.credentialed_tenants}</td>
                      <td className="small subtle">{provider.detail ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="card">
            <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>Dependencies</h2>
            <table>
              <thead><tr><th>Component</th><th>State</th><th>Round trip</th><th>Detail</th></tr></thead>
              <tbody>
                {data.components.map((component) => (
                  <tr key={component.name}>
                    <td className="mono small">{component.name}</td>
                    <td>
                      {component.reachable
                        ? <Badge tone="ok" dot>reachable</Badge>
                        : <Badge tone="err" dot>unreachable</Badge>}
                    </td>
                    <td className="mono small">
                      {component.latency_ms === null ? "—" : `${component.latency_ms} ms`}
                    </td>
                    <td className="small subtle" style={{ overflowWrap: "anywhere" }}>
                      {component.detail ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="subtle small" style={{ marginBottom: 0, marginTop: 10 }}>
              Each of these is probed when this page loads, not read from a
              cache. LiveKit being down does not fail the API&apos;s readiness
              probe — configuration still works during a media-layer incident,
              and failing readiness would take the Control Plane out of
              rotation for no reason.
            </p>
          </div>
        </div>
      )}
    </Shell>
  );
}

function UsageCard({ label, sample }: { label: string; sample: ResourceUsage | null }) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        {sample?.value == null ? "—" : formatUsage(sample)}
      </div>
      <div className="stat-note">{sample?.detail ?? "not scraped"}</div>
    </div>
  );
}

function formatUsage(sample: ResourceUsage): string {
  if (sample.value == null) return "—";
  if (sample.unit === "bytes") {
    if (sample.value >= 1_073_741_824) return `${(sample.value / 1_073_741_824).toFixed(1)} GiB`;
    if (sample.value >= 1_048_576) return `${(sample.value / 1_048_576).toFixed(0)} MiB`;
    return `${Math.round(sample.value)} B`;
  }
  if (sample.unit === "ratio") return `${Math.round(sample.value * 100)}%`;
  return `${sample.value} ${sample.unit}`;
}

function ProviderBadge({ status }: { status: string }) {
  const tone =
    status === "healthy" ? "ok" :
    status === "circuit_open" || status === "missing_credential" ? "err" :
    "neutral";
  return <Badge tone={tone} dot>{status.replaceAll("_", " ")}</Badge>;
}
