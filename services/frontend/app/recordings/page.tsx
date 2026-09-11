"use client";

/**
 * Call recordings (spec 39).
 *
 * Metadata only. The audio lives in object storage and PostgreSQL holds a
 * pointer, so this screen shows the pointer and where the object is — not an
 * audio player, which would need a signed URL the API does not issue yet.
 *
 * The list is genuinely live. It is empty because the worker does not start
 * egress yet (Plan 2b.3), and the screen says so rather than implying the
 * feature is missing from the console.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice, RelativeTime } from "@/components/ui";
import { api, type Recording } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

function bytes(value: number | null): string {
  if (value === null) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

export default function RecordingsPage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [rows, setRows] = useState<Recording[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await api.recordings();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load recordings");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const withoutRetention = (rows ?? []).filter((row) => row.delete_after === null);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Recordings</h1>
          <p className="page-subtitle">
            Where each recording is stored, how long it runs, and when
            retention deletes it. The audio itself is in object storage, never
            in the database.
          </p>
        </div>
        <Button size="sm" onClick={() => void load()}>Refresh</Button>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {rows !== null && rows.length === 0 && (
        <Notice tone="info">
          No recordings yet. An agent version can already have recording
          switched on, but the worker does not start a LiveKit egress job for
          it — that is Plan 2b.3. Until it lands, enabling recording on an
          agent records the intent and produces no audio. This list is live, so
          anything egress writes appears here.
        </Notice>
      )}

      {withoutRetention.length > 0 && (
        <Notice tone="warn">
          {withoutRetention.length === 1 ? "One recording has" : `${withoutRetention.length} recordings have`}{" "}
          no retention date, so nothing will delete{" "}
          {withoutRetention.length === 1 ? "it" : "them"}. Retention is a choice
          the tenant has to make rather than a default the platform should
          assume.
        </Notice>
      )}

      {rows !== null && rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Started</th><th>Call</th><th>Duration</th><th>Size</th>
                <th>Location</th><th>Retention</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="small"><RelativeTime iso={row.started_at} /></td>
                  <td className="mono small">{row.call_id.slice(0, 8)}…</td>
                  <td className="mono small">{duration(row.duration_seconds)}</td>
                  <td className="mono small">{bytes(row.byte_size)}</td>
                  <td className="small">
                    <span className="mono">{row.bucket}</span>
                    <div className="subtle small mono" style={{ overflowWrap: "anywhere" }}>
                      {row.object_key}
                    </div>
                  </td>
                  <td className="small">
                    {row.delete_after
                      ? <RelativeTime iso={row.delete_after} />
                      : <Badge tone="warn">kept indefinitely</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows === null && <Loading label="Loading recordings…" />}
    </Shell>
  );
}
