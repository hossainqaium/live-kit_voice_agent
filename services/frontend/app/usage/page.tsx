"use client";

/**
 * Usage against this tenant's limits (spec 47).
 *
 * The screen distinguishes a limit that is enforced from one that is only
 * recorded. Only ``max_concurrent_calls`` is checked before a call is
 * accepted; the daily and monthly limits read a rollup nothing writes yet, so
 * showing all three as if they were live would tell the operator that traffic
 * is being capped when it is not.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice } from "@/components/ui";
import { api, type UsageSummary } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

function ratio(used: number, limit: number | null): number | null {
  if (limit === null || limit <= 0) return null;
  return Math.min(1, used / limit);
}

function barTone(fraction: number | null): string {
  if (fraction === null) return "bar";
  if (fraction >= 0.9) return "bar bar-err";
  if (fraction >= 0.7) return "bar bar-warn";
  return "bar";
}

function LimitCard({
  label, used, limit, unit, enforced, note,
}: {
  label: string;
  used: number;
  limit: number | null;
  unit: string;
  enforced: boolean;
  note: string;
}) {
  const fraction = ratio(used, limit);
  return (
    <div className="card">
      <div className="row-between">
        <div className="stat-label">{label}</div>
        {enforced
          ? <Badge tone="ok">enforced</Badge>
          : <Badge tone="warn">recorded only</Badge>}
      </div>
      <div className="stat-value">
        {used.toLocaleString()}
        <span className="subtle" style={{ fontSize: 15, fontWeight: 400 }}>
          {limit === null ? ` ${unit} · no limit` : ` / ${limit.toLocaleString()} ${unit}`}
        </span>
      </div>
      {fraction !== null && (
        <div className={barTone(fraction)} style={{ marginTop: 8 }}>
          <span style={{ width: `${fraction * 100}%` }} />
        </div>
      )}
      <div className="stat-note">{note}</div>
    </div>
  );
}

export default function UsagePage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.usage(30));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load usage");
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const unenforced = data
    ? [
        !data.daily_limit_enforced && data.max_daily_calls !== null && "daily calls",
        !data.monthly_limit_enforced && data.max_monthly_minutes !== null && "monthly minutes",
      ].filter(Boolean)
    : [];

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Usage</h1>
          <p className="page-subtitle">
            Where this tenant stands against the limits the platform set. The
            limits themselves are commercial terms and are not editable here.
          </p>
        </div>
        <Button size="sm" onClick={() => void load()}>Refresh</Button>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {data === null ? (
        <Loading label="Loading usage…" />
      ) : (
        <div className="stack">
          {unenforced.length > 0 && (
            <Notice tone="warn">
              The {unenforced.join(" and ")} limit{unenforced.length > 1 ? "s are" : " is"}{" "}
              recorded but not yet enforced: nothing writes the daily rollup the
              check reads, so traffic is not actually capped by{" "}
              {unenforced.length > 1 ? "them" : "it"}. Concurrency is enforced
              on every inbound call.
            </Notice>
          )}

          <div className="card-grid">
            <LimitCard
              label="Calls in progress"
              used={data.active_calls}
              limit={data.max_concurrent_calls}
              unit="calls"
              enforced={data.concurrent_limit_enforced}
              note="Checked before an inbound call is accepted."
            />
            <LimitCard
              label="Calls today"
              used={data.calls_today}
              limit={data.max_daily_calls}
              unit="calls"
              enforced={data.daily_limit_enforced}
              note="Counted from call history, in UTC."
            />
            <LimitCard
              label="Minutes this month"
              used={data.minutes_this_month}
              limit={data.max_monthly_minutes}
              unit="min"
              enforced={data.monthly_limit_enforced}
              note="Summed call duration since the first of the month."
            />
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th><th>Calls</th><th>Answered</th><th>Failed</th>
                  <th>Transferred</th><th>Total</th><th>AI time</th>
                </tr>
              </thead>
              <tbody>
                {data.days.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="muted small" style={{ padding: 16 }}>
                      The daily rollup is empty. It is written by a job that is
                      not yet running, which is also why the daily and monthly
                      limits above are unenforced — the figures in the cards
                      come from call history instead.
                    </td>
                  </tr>
                ) : (
                  data.days.map((day) => (
                    <tr key={day.usage_date}>
                      <td className="mono small">{day.usage_date}</td>
                      <td className="mono small">{day.call_count}</td>
                      <td className="mono small">{day.answered_count}</td>
                      <td className="mono small">{day.failed_count}</td>
                      <td className="mono small">{day.transferred_count}</td>
                      <td className="mono small">{Math.round(day.total_seconds / 60)}m</td>
                      <td className="mono small">{Math.round(day.ai_seconds / 60)}m</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Shell>
  );
}
