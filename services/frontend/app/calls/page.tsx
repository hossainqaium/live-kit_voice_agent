"use client";

/**
 * Call history (spec 41) with transcript and event timeline (spec 40, 42).
 *
 * Read-only: a call record is produced by the worker as the call happens, and
 * an editable call history is not a record.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Loading, Notice, RelativeTime,
} from "@/components/ui";
import { api, type Call, type CallDetail, type CallState } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

const TONE: Record<string, "ok" | "warn" | "err" | "info" | "neutral"> = {
  COMPLETED: "ok", IN_PROGRESS: "info", AI_CONNECTED: "info", ANSWERED: "info",
  RINGING: "warn", NEW: "neutral", TRANSFERRING: "warn", HUMAN_AGENT: "info",
  FAILED: "err", TIMEOUT: "err", CANCELLED: "neutral", BUSY: "warn", NO_ANSWER: "warn",
};

function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export default function CallsPage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [rows, setRows] = useState<Call[] | null>(null);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<CallState | "">("");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<CallDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const load = useCallback(async () => {
    try {
      const page = await api.calls.list(50, 0, filter || undefined);
      setRows(page.items);
      setTotal(page.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load calls");
      setRows([]);
    }
  }, [filter]);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  async function open(call: Call) {
    setLoadingDetail(true);
    try {
      setSelected(await api.calls.get(call.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the call");
    } finally {
      setLoadingDetail(false);
    }
  }

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Calls</h1>
          <p className="page-subtitle">
            Every call the platform handled, with its state machine and
            transcript. Produced by the worker as calls happen — nothing here
            is editable.
          </p>
        </div>
        <div className="row">
          <select value={filter} onChange={(e) => setFilter(e.target.value as CallState | "")}
            style={{ width: 180 }} aria-label="Filter by state">
            <option value="">All states</option>
            {["COMPLETED", "IN_PROGRESS", "FAILED", "NO_ANSWER", "BUSY", "TIMEOUT"].map((s) => (
              <option key={s} value={s}>{s.replace(/_/g, " ").toLowerCase()}</option>
            ))}
          </select>
          <Button onClick={() => void load()}>Refresh</Button>
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading calls…" />
        ) : rows.length === 0 ? (
          <EmptyState title={filter ? "No calls in that state" : "No calls yet"}>
            {filter
              ? "Try a different filter."
              : "Once a PBX sends a call to a configured number, it appears here."}
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th><th>Caller</th><th>Dialled</th><th>Agent</th>
                <th>Duration</th><th>State</th><th>Artefacts</th>
                <th style={{ textAlign: "right" }} />
              </tr>
            </thead>
            <tbody>
              {rows.map((call) => (
                <tr key={call.id}>
                  <td className="small"><RelativeTime iso={call.start_time} /></td>
                  <td className="mono small">{call.caller_number ?? "—"}</td>
                  <td className="mono small">{call.did ?? "—"}</td>
                  <td className="small">
                    {call.agent_name ?? <span className="subtle">—</span>}
                    {call.agent_version_number !== null && (
                      <span className="subtle"> v{call.agent_version_number}</span>
                    )}
                  </td>
                  <td className="small">{duration(call.duration_seconds)}</td>
                  <td>
                    <Badge tone={TONE[call.state] ?? "neutral"} dot>
                      {call.state.replace(/_/g, " ").toLowerCase()}
                    </Badge>
                  </td>
                  <td className="small">
                    <div className="row" style={{ gap: 4 }}>
                      {call.has_transcript && <Badge tone="info">transcript</Badge>}
                      {call.has_recording && <Badge tone="info">audio</Badge>}
                      {!call.has_transcript && !call.has_recording && (
                        <span className="subtle">none</span>
                      )}
                    </div>
                  </td>
                  <td>
                    <div className="cell-actions">
                      <Button size="sm" busy={loadingDetail} onClick={() => void open(call)}>
                        Inspect
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {rows !== null && rows.length > 0 && (
        <p className="subtle small" style={{ marginTop: 10 }}>
          Showing {rows.length} of {total}.
        </p>
      )}

      {selected && <CallDetailDialog call={selected} onClose={() => setSelected(null)} />}
    </Shell>
  );
}

function CallDetailDialog({ call, onClose }: { call: CallDetail; onClose(): void }) {
  return (
    <Dialog
      title={`Call ${call.call_id}`}
      onClose={onClose}
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <div className="card-grid" style={{ marginBottom: 16 }}>
        <div>
          <div className="stat-label">State</div>
          <div style={{ marginTop: 4 }}>
            <Badge tone={TONE[call.state] ?? "neutral"} dot>
              {call.state.replace(/_/g, " ").toLowerCase()}
            </Badge>
          </div>
        </div>
        <div>
          <div className="stat-label">Duration</div>
          <div className="small" style={{ marginTop: 6 }}>{duration(call.duration_seconds)}</div>
        </div>
        <div>
          <div className="stat-label">Hangup</div>
          <div className="small" style={{ marginTop: 6 }}>
            {call.hangup_reason?.replace(/_/g, " ").toLowerCase() ?? "—"}
          </div>
        </div>
        <div>
          <div className="stat-label">Transfer</div>
          <div className="small" style={{ marginTop: 6 }}>
            {call.transfer_status.replace(/_/g, " ").toLowerCase()}
          </div>
        </div>
      </div>

      <div className="small stack" style={{ gap: 3, marginBottom: 16 }}>
        <div><span className="subtle">Caller</span> <span className="mono">{call.caller_number ?? "—"}</span></div>
        <div><span className="subtle">Dialled</span> <span className="mono">{call.did ?? "—"}</span></div>
        <div><span className="subtle">Room</span> <span className="mono">{call.room_id ?? "—"}</span></div>
        <div>
          <span className="subtle">Agent</span>{" "}
          {call.agent_name ?? "—"}
          {call.agent_version_number !== null && ` v${call.agent_version_number}`}
        </div>
      </div>

      {call.failure_detail && <Notice tone="err">{call.failure_detail}</Notice>}

      <h3 style={{ marginBottom: 8 }}>Transcript</h3>
      {call.segments.length === 0 ? (
        <Notice tone="info">
          No transcript. Either transcription was disabled for this agent, or
          nobody spoke — a silent call produces no segments.
        </Notice>
      ) : (
        <div className="stack" style={{ gap: 8, marginBottom: 18 }}>
          {call.segments.map((segment) => (
            <div key={segment.sequence}>
              <div className="row" style={{ gap: 7 }}>
                <Badge tone={
                  segment.speaker === "CALLER" ? "info"
                    : segment.speaker === "AI" ? "neutral" : "warn"
                }>
                  {segment.speaker.replace(/_/g, " ").toLowerCase()}
                </Badge>
                {segment.confidence !== null && segment.confidence > 0 && (
                  <span className="subtle small">{(segment.confidence * 100).toFixed(0)}% confident</span>
                )}
                {segment.is_private_to_agent && <Badge tone="warn">not heard by caller</Badge>}
              </div>
              <div className="small" style={{ marginTop: 3 }}>{segment.text}</div>
            </div>
          ))}
        </div>
      )}

      <h3 style={{ marginBottom: 8 }}>Timeline</h3>
      {call.events.length === 0 ? (
        <p className="subtle small" style={{ margin: 0 }}>No events recorded.</p>
      ) : (
        <div className="stack" style={{ gap: 5 }}>
          {call.events.map((event, index) => (
            <div key={index} className="row small" style={{ gap: 8, alignItems: "flex-start" }}>
              <span className="subtle mono" style={{ minWidth: 62 }}>
                {new Date(event.occurred_at).toLocaleTimeString()}
              </span>
              <span style={{ flex: 1 }}>
                <strong style={{ fontWeight: 500 }}>{event.event_type}</strong>
                {event.from_state && event.to_state && (
                  <span className="subtle"> · {event.from_state} → {event.to_state}</span>
                )}
                {Object.keys(event.payload ?? {}).length > 0 && (
                  <div className="subtle mono" style={{ fontSize: 11.5, marginTop: 2 }}>
                    {Object.entries(event.payload)
                      .filter(([, v]) => v !== null)
                      .map(([k, v]) => `${k}=${v}`)
                      .join("  ")}
                  </div>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
