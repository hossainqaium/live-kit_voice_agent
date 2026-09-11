"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Loading, Notice } from "@/components/ui";
import { api, fetchApiHealth, type Pbx } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

interface Overview {
  pbxCount: number;
  pbxUntested: number;
  pbxFailing: number;
  recent: Pbx[];
}

export default function TenantDashboard() {
  const { principal, loading } = useRequireAuth();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [apiHealthy, setApiHealthy] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!principal) return;
    let cancelled = false;

    async function load() {
      try {
        const page = await api.pbxs.list(200, 0);
        if (cancelled) return;
        setOverview({
          pbxCount: page.total,
          pbxUntested: page.items.filter((p) => p.last_test_result === "UNTESTED").length,
          pbxFailing: page.items.filter((p) => p.last_test_result === "FAILED").length,
          recent: [...page.items]
            .sort((a, b) => b.created_at.localeCompare(a.created_at))
            .slice(0, 5),
        });
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "could not load");
      }
    }

    void load();
    fetchApiHealth()
      .then(() => !cancelled && setApiHealthy(true))
      .catch(() => !cancelled && setApiHealthy(false));

    return () => {
      cancelled = true;
    };
  }, [principal]);

  if (loading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p className="page-subtitle">
            Telephony and agent configuration for your tenant. Calls, analytics
            and the agent builder arrive later in Phase 4.
          </p>
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="card-grid" style={{ marginBottom: 20 }}>
        <div className="card">
          <div className="stat-label">PBXs</div>
          <div className="stat-value">{overview?.pbxCount ?? "—"}</div>
          <div className="stat-note">
            <Link href="/pbxs">Manage →</Link>
          </div>
        </div>

        <div className="card">
          <div className="stat-label">Untested</div>
          <div className="stat-value">{overview?.pbxUntested ?? "—"}</div>
          <div className="stat-note">
            {overview?.pbxUntested
              ? "A saved PBX is not a reachable one"
              : "All PBXs have been probed"}
          </div>
        </div>

        <div className="card">
          <div className="stat-label">Failing</div>
          <div className="stat-value" style={{ color: overview?.pbxFailing ? "var(--err)" : undefined }}>
            {overview?.pbxFailing ?? "—"}
          </div>
          <div className="stat-note">Last connection test failed</div>
        </div>

        <div className="card">
          <div className="stat-label">Configuration API</div>
          <div className="stat-value" style={{ fontSize: 17, paddingTop: 6 }}>
            {apiHealthy === null ? (
              "checking…"
            ) : apiHealthy ? (
              <Badge tone="ok" dot>reachable</Badge>
            ) : (
              <Badge tone="err" dot>unreachable</Badge>
            )}
          </div>
          <div className="stat-note">Control plane</div>
        </div>
      </div>

      <div className="card">
        <div className="row-between" style={{ marginBottom: 12 }}>
          <h2>Recently added PBXs</h2>
          <Link href="/pbxs" className="small">View all</Link>
        </div>

        {overview === null ? (
          <Loading />
        ) : overview.recent.length === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            No PBXs yet. <Link href="/pbxs">Register one</Link> to begin routing
            calls to an AI agent.
          </p>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {overview.recent.map((pbx) => (
              <div key={pbx.id} className="row-between">
                <div className="truncate">
                  <strong style={{ fontWeight: 500 }}>{pbx.name}</strong>{" "}
                  <span className="subtle mono small">
                    {pbx.host}:{pbx.port}
                  </span>
                </div>
                <TestBadge result={pbx.last_test_result} />
              </div>
            ))}
          </div>
        )}
      </div>
    </Shell>
  );
}

function TestBadge({ result }: { result: Pbx["last_test_result"] }) {
  if (result === "PASSED") return <Badge tone="ok" dot>reachable</Badge>;
  if (result === "FAILED") return <Badge tone="err" dot>unreachable</Badge>;
  return <Badge tone="neutral">untested</Badge>;
}
