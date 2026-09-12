"use client";

/**
 * Platform dashboard.
 *
 * A landing page over the sections in the sidebar rather than a second copy of
 * them: the numbers come from the capacity endpoint, and anything worth acting
 * on links to the screen that can act on it.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Loading, Notice } from "@/components/ui";
import { api, type Capacity, type LiveKitOverview } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

export default function PlatformDashboard() {
  const { principal, loading } = useRequireAuth();

  const [capacity, setCapacity] = useState<Capacity | null>(null);
  const [livekit, setLivekit] = useState<LiveKitOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [cap, lk] = await Promise.all([
        api.platform.capacity(),
        api.platform.livekit().catch(() => null),
      ]);
      setCapacity(cap);
      setLivekit(lk);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the platform overview");
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (loading || !principal) return <div className="auth-screen"><Loading /></div>;

  const down = (capacity?.components ?? []).filter((component) => !component.reachable);
  const drifted = livekit?.needs_attention.length ?? 0;
  const configurationDrift = livekit?.configuration_drift_detected ?? drifted > 0;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Platform</h1>
          <p className="page-subtitle">
            Tenants, the AI provider catalog, LiveKit and platform capacity.
          </p>
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {down.length > 0 && (
        <Notice tone="err">
          {down.map((component) => component.name).join(", ")}{" "}
          {down.length === 1 ? "is" : "are"} unreachable.{" "}
          <Link href="/platform/infrastructure">Infrastructure</Link> has the detail.
        </Notice>
      )}

      {configurationDrift && (
        <Notice tone="warn">
          Configuration Drift Detected
          {drifted > 0
            ? ` — ${drifted} mirrored resource${drifted === 1 ? "" : "s"} do not match the database.`
            : " — LiveKit holds resources the database does not name."}{" "}
          <Link href="/platform/livekit">LiveKit</Link> lists which.
        </Notice>
      )}

      {capacity === null ? (
        <Loading label="Loading the platform overview…" />
      ) : (
        <div className="stack">
          <div className="card-grid">
            <Link className="card" href="/platform/tenants" style={{ textDecoration: "none" }}>
              <div className="stat-label">Tenants</div>
              <div className="stat-value">{capacity.tenant_count}</div>
              <div className="stat-note">{capacity.active_tenant_count} active</div>
            </Link>
            <Link className="card" href="/platform/capacity" style={{ textDecoration: "none" }}>
              <div className="stat-label">Calls in flight</div>
              <div className="stat-value">{capacity.active_calls}</div>
              <div className="stat-note">{capacity.calls_last_24h} in the last 24 hours</div>
            </Link>
            <Link className="card" href="/platform/providers" style={{ textDecoration: "none" }}>
              <div className="stat-label">Agents</div>
              <div className="stat-value">{capacity.agent_count}</div>
              <div className="stat-note">
                {capacity.published_agent_count} with a published version
              </div>
            </Link>
            <Link className="card" href="/platform/livekit" style={{ textDecoration: "none" }}>
              <div className="stat-label">LiveKit</div>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {livekit === null ? (
                  <Badge tone="neutral">unknown</Badge>
                ) : livekit.reachable ? (
                  <Badge tone="ok" dot>answering</Badge>
                ) : (
                  <Badge tone="err" dot>unreachable</Badge>
                )}
              </div>
              <div className="stat-note">
                {capacity.livekit_rooms ?? 0} room
                {capacity.livekit_rooms === 1 ? "" : "s"}
              </div>
            </Link>
          </div>

          <div className="card">
            <h2 style={{ marginTop: 0, marginBottom: 8, fontSize: 15 }}>
              Signed in as platform staff
            </h2>
            <p className="muted small" style={{ marginTop: 0 }}>
              This account has no tenant, so every tenant endpoint refuses it.
              That is deliberate: acting inside a tenant is an explicit
              operation rather than something a platform administrator does by
              default.
            </p>
            <dl className="kv">
              <dt>Roles</dt>
              <dd>{principal.roles.join(", ") || "none"}</dd>
              <dt>Permissions</dt>
              <dd>
                {principal.roles.includes("SUPER_ADMIN")
                  ? "all (SUPER_ADMIN)"
                  : principal.permissions.join(", ") || "none granted"}
              </dd>
            </dl>
          </div>
        </div>
      )}
    </Shell>
  );
}
