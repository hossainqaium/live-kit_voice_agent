"use client";

/**
 * LiveKit administration (spec 12, 46).
 *
 * PostgreSQL is written first and LiveKit second, so LiveKit is a mirror and
 * not a second source of truth. What this screen shows is the mirror's
 * agreement with the database: a healthy platform has an empty list, and
 * anything in it is a row a re-sync has to fix.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice, RelativeTime, ToastStack, useToasts } from "@/components/ui";
import { api, type LiveKitOverview, type SyncStatus } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

const SYNC_TONE: Record<SyncStatus, "ok" | "warn" | "err" | "neutral"> = {
  SYNCED: "ok",
  PENDING: "warn",
  DRIFTED: "warn",
  FAILED: "err",
};

function SyncCounts({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  return (
    <div className="card">
      <div className="stat-label">{title}</div>
      {entries.length === 0 ? (
        <div className="stat-value subtle" style={{ fontSize: 18 }}>none</div>
      ) : (
        <div className="row" style={{ marginTop: 8, flexWrap: "wrap", gap: 6 }}>
          {entries.map(([state, count]) => (
            <Badge key={state} tone={SYNC_TONE[state as SyncStatus] ?? "neutral"}>
              {count} {state.toLowerCase()}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

export default function LiveKitPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const toasts = useToasts();

  const [data, setData] = useState<LiveKitOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setData(await api.platform.livekit());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not reach the LiveKit overview");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>LiveKit</h1>
          <p className="page-subtitle">
            Whether LiveKit is answering, and whether the resources mirrored
            into it still match the database.
          </p>
        </div>
        <Button size="sm" busy={busy} onClick={() => void load()}>Refresh</Button>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {data === null ? (
        <Loading label="Asking LiveKit…" />
      ) : (
        <div className="stack">
          {!data.reachable && (
            <Notice tone="err">
              LiveKit is not answering the admin API{data.detail ? `: ${data.detail}` : ""}.
              New calls cannot be set up, and configuration changes will queue
              as PENDING until it returns — the database write still succeeds,
              which is why nothing is lost.
            </Notice>
          )}

          <div className="card-grid">
            <div className="card">
              <div className="stat-label">Admin API</div>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {data.reachable
                  ? <Badge tone="ok" dot>answering</Badge>
                  : <Badge tone="err" dot>unreachable</Badge>}
              </div>
              <div className="stat-note mono" style={{ overflowWrap: "anywhere" }}>{data.url}</div>
            </div>
            <div className="card">
              <div className="stat-label">Rooms</div>
              <div className="stat-value">{data.room_count ?? "—"}</div>
              <div className="stat-note">one per call in progress</div>
            </div>
            <div className="card">
              <div className="stat-label">SIP endpoint</div>
              <div className="stat-value mono" style={{ fontSize: 15, overflowWrap: "anywhere" }}>
                {data.sip_uri}
              </div>
              <div className="stat-note">where a PBX sends its calls</div>
            </div>
            <SyncCounts title="Trunks" counts={data.trunk_sync} />
            <SyncCounts title="Dispatch rules" counts={data.dispatch_rule_sync} />
          </div>

          {data.needs_attention.length === 0 ? (
            <Notice tone="ok">
              Every mirrored resource is SYNCED, so LiveKit's view matches the
              database.
            </Notice>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Resource</th><th>Tenant</th><th>Kind</th><th>State</th>
                    <th>Attempts</th><th>Last synced</th><th>Error</th>
                  </tr>
                </thead>
                <tbody>
                  {data.needs_attention.map((row) => (
                    <tr key={`${row.kind}-${row.id}`}>
                      <td>
                        <div style={{ fontWeight: 500 }}>{row.name}</div>
                        {row.livekit_resource_id && (
                          <div className="subtle small mono">{row.livekit_resource_id}</div>
                        )}
                      </td>
                      <td className="small">{row.tenant_name ?? "—"}</td>
                      <td className="small mono">{row.kind}</td>
                      <td>
                        <Badge tone={SYNC_TONE[row.sync_status] ?? "neutral"}>
                          {row.sync_status.toLowerCase()}
                        </Badge>
                      </td>
                      <td className="mono small">{row.sync_attempts}</td>
                      <td className="small"><RelativeTime iso={row.last_synced_at} /></td>
                      <td className="small subtle" style={{ overflowWrap: "anywhere", maxWidth: "34ch" }}>
                        {row.sync_error ?? ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <Notice tone="info">
            Re-syncing a trunk is done from the tenant's own SIP Trunks screen,
            which is where the credential lives. A platform-wide re-sync button
            here would act across tenants with no way to review what it
            changed first.
          </Notice>
        </div>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}
