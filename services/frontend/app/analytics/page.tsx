"use client";

/**
 * Call analytics (spec 57).
 *
 * Business metrics only — counts, durations, outcomes. Per-turn voice latency
 * is deliberately absent: it lives in Prometheus, and computing percentiles
 * here from a second source would produce a number that disagrees with the
 * Grafana dashboard. The API says the same thing in ``latency_note``.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice } from "@/components/ui";
import { api, type Analytics, type CallState } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

const WINDOWS = [7, 30, 90];

const STATE_TONE: Partial<Record<CallState, "ok" | "warn" | "err" | "info" | "neutral">> = {
  COMPLETED: "ok",
  IN_PROGRESS: "info",
  AI_CONNECTED: "info",
  HUMAN_AGENT: "info",
  TRANSFERRING: "warn",
  FAILED: "err",
  TIMEOUT: "err",
  BUSY: "warn",
  NO_ANSWER: "warn",
  CANCELLED: "neutral",
};

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default function AnalyticsPage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [days, setDays] = useState(30);
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (window: number) => {
    setBusy(true);
    try {
      setData(await api.analytics(window));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load analytics");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { if (principal) void load(days); }, [principal, days, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const peak = Math.max(1, ...(data?.by_day ?? []).map((d) => d.calls));

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Analytics</h1>
          <p className="page-subtitle">
            Call volume and outcomes over the selected window, computed from
            call history.
          </p>
        </div>
        <div className="row">
          {WINDOWS.map((window) => (
            <Button
              key={window}
              size="sm"
              variant={window === days ? "primary" : "default"}
              onClick={() => setDays(window)}
            >
              {window} days
            </Button>
          ))}
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {data === null ? (
        <Loading label="Loading analytics…" />
      ) : data.total_calls === 0 ? (
        <Notice tone="info">
          No calls in the last {data.window_days} days, so there is nothing to
          summarise. Place a test call and this fills in.
        </Notice>
      ) : (
        <div className="stack">
          <div className="card-grid">
            <div className="card">
              <div className="stat-label">Calls</div>
              <div className="stat-value">{data.total_calls}</div>
              <div className="stat-note">in {data.window_days} days</div>
            </div>
            <div className="card">
              <div className="stat-label">Answered</div>
              <div className="stat-value">{data.answered_calls}</div>
              <div className="stat-note">{percent(data.answer_rate)} answer rate</div>
            </div>
            <div className="card">
              <div className="stat-label">Failed</div>
              <div className="stat-value">{data.failed_calls}</div>
              <div className="stat-note">
                {data.failed_calls === 0 ? "none" : "failed, timed out, busy or unanswered"}
              </div>
            </div>
            <div className="card">
              <div className="stat-label">Transferred</div>
              <div className="stat-value">{data.transferred_calls}</div>
              <div className="stat-note">bridged to a person</div>
            </div>
            <div className="card">
              <div className="stat-label">Talk time</div>
              <div className="stat-value">{duration(data.total_seconds)}</div>
              <div className="stat-note">
                {data.average_duration_seconds === null
                  ? "no completed calls"
                  : `${duration(data.average_duration_seconds)} average`}
              </div>
            </div>
          </div>

          {data.by_day.length > 0 && (
            <div className="card">
              <div className="row-between" style={{ marginBottom: 12 }}>
                <h2 style={{ margin: 0, fontSize: 15 }}>Calls per day</h2>
                <span className="subtle small">peak {peak}</span>
              </div>
              <div className="spark" aria-hidden>
                {data.by_day.map((day) => (
                  <div
                    key={day.date}
                    style={{ height: `${Math.max(2, (day.calls / peak) * 100)}%` }}
                    title={`${day.date}: ${day.calls} calls, ${day.answered} answered`}
                  />
                ))}
              </div>
              <div className="row-between subtle small" style={{ marginTop: 6 }}>
                <span>{data.by_day[0]?.date}</span>
                <span>{data.by_day[data.by_day.length - 1]?.date}</span>
              </div>
              {/* The bars carry no accessible meaning, so the same data is in a
                  table for anyone who cannot see them. */}
              <details style={{ marginTop: 10 }}>
                <summary className="small muted" style={{ cursor: "pointer" }}>
                  Show the daily figures
                </summary>
                <table style={{ marginTop: 8 }}>
                  <thead>
                    <tr><th>Date</th><th>Calls</th><th>Answered</th><th>Failed</th></tr>
                  </thead>
                  <tbody>
                    {[...data.by_day].reverse().map((day) => (
                      <tr key={day.date}>
                        <td className="mono small">{day.date}</td>
                        <td className="mono small">{day.calls}</td>
                        <td className="mono small">{day.answered}</td>
                        <td className="mono small">{day.failed}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            </div>
          )}

          <div className="card-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
            <div className="card">
              <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>By outcome</h2>
              <table>
                <thead><tr><th>State</th><th>Calls</th><th>Share</th></tr></thead>
                <tbody>
                  {Object.entries(data.by_state)
                    .sort(([, a], [, b]) => b - a)
                    .map(([state, count]) => (
                      <tr key={state}>
                        <td>
                          <Badge tone={STATE_TONE[state as CallState] ?? "neutral"}>
                            {state.toLowerCase().replace(/_/g, " ")}
                          </Badge>
                        </td>
                        <td className="mono small">{count}</td>
                        <td style={{ width: 110 }}>
                          <div className="bar">
                            <span style={{ width: `${(count / data.total_calls) * 100}%` }} />
                          </div>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>

            <div className="card">
              <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>By agent</h2>
              <table>
                <thead><tr><th>Agent</th><th>Calls</th><th>Talk time</th></tr></thead>
                <tbody>
                  {data.by_agent.map((entry) => (
                    <tr key={entry.agent}>
                      <td className="small">
                        {entry.agent === "unassigned"
                          ? <span className="subtle">no agent recorded</span>
                          : entry.agent}
                      </td>
                      <td className="mono small">{entry.calls}</td>
                      <td className="mono small">{duration(entry.seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <Notice tone="info">
            {data.latency_note}{" "}
            <a href="http://localhost:3201" target="_blank" rel="noreferrer">
              Open Grafana
            </a>
            .
          </Notice>
        </div>
      )}

      {busy && data !== null && <div className="subtle small">Refreshing…</div>}
    </Shell>
  );
}
