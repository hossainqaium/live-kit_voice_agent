"use client";

/**
 * Platform capacity (spec 48).
 *
 * Every figure is measured. There is deliberately no "utilisation percentage"
 * or headroom estimate: the autoscaling signal is Phase 5, and a number nobody
 * can act on is worse than an honest count.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice } from "@/components/ui";
import { api, type Capacity } from "@/lib/api";
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

  const used = data?.licensed_concurrent_calls
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
            What this platform is carrying right now, and what it is configured
            to allow.
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
              <div className="stat-label">Calls in flight</div>
              <div className="stat-value">{data.active_calls}</div>
              <div className="stat-note">
                {data.licensed_concurrent_calls === null
                  ? "at least one tenant is uncapped"
                  : `of ${data.licensed_concurrent_calls} licensed`}
              </div>
              {used !== null && (
                <div className={used >= 0.9 ? "bar bar-err" : used >= 0.7 ? "bar bar-warn" : "bar"}
                  style={{ marginTop: 8 }}>
                  <span style={{ width: `${used * 100}%` }} />
                </div>
              )}
            </div>
            <div className="card">
              <div className="stat-label">Calls today</div>
              <div className="stat-value">{data.calls_last_24h}</div>
              <div className="stat-note">last 24 hours, every tenant</div>
            </div>
            <div className="card">
              <div className="stat-label">LiveKit rooms</div>
              <div className="stat-value">{data.livekit_rooms ?? "—"}</div>
              <div className="stat-note">
                {data.livekit_rooms === null ? "LiveKit did not answer" : "live, from LiveKit itself"}
              </div>
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
            <div className="card">
              <div className="stat-label">Telephony</div>
              <div className="stat-value">{data.phone_number_count}</div>
              <div className="stat-note">
                numbers across {data.sip_trunk_count} trunk
                {data.sip_trunk_count === 1 ? "" : "s"}
              </div>
            </div>
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
              cache. LiveKit being down does not fail the API's readiness
              probe — configuration still works during a media-layer incident,
              and failing readiness would take the Control Plane out of
              rotation for no reason.
            </p>
          </div>

          <Notice tone="info">
            There is no utilisation estimate here on purpose. Autoscaling
            signals are Phase 5, and a percentage nobody can act on would be
            worse than these counts.
          </Notice>
        </div>
      )}
    </Shell>
  );
}
