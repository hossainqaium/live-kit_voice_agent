"use client";

/**
 * The audit trail (spec 69).
 *
 * Read-only, because the API has no write or delete route for it. An
 * append-only log with an edit button would defeat the point of keeping one.
 *
 * Values are shown as stored: redaction happens on write, so redacting again
 * here would hide the fact that something was *not* redacted on the way in.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice,
} from "@/components/ui";
import { api, type AuditEntry, type TenantSummary } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

const PAGE_SIZE = 50;

function tone(action: string): "ok" | "warn" | "err" | "info" | "neutral" {
  if (action.includes("deleted") || action.includes("revoked")) return "err";
  if (action.includes("created")) return "ok";
  if (action.includes("password") || action.includes("suspend")) return "warn";
  if (action.includes("signed_in")) return "neutral";
  return "info";
}

export default function AuditLogsPage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [forTenant, setForTenant] = useState("");
  const [action, setAction] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [days, setDays] = useState(30);
  const [error, setError] = useState<string | null>(null);
  const [inspecting, setInspecting] = useState<AuditEntry | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await api.platform.auditLogs({
        forTenant: forTenant || undefined,
        action: action.trim() || undefined,
        userEmail: userEmail.trim() || undefined,
        days,
        limit: PAGE_SIZE,
        offset,
      });
      setRows(page.items);
      setTotal(page.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the audit trail");
      setRows([]);
    }
  }, [forTenant, action, userEmail, days, offset]);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  useEffect(() => {
    if (!principal) return;
    void api.platform.tenants
      .list(undefined, 200)
      .then((page) => setTenants(page.items))
      .catch(() => setTenants([]));
  }, [principal]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Audit Logs</h1>
          <p className="page-subtitle">
            Every configuration change, who made it and from where. Append-only
            — there is no endpoint that can edit or remove an entry.
          </p>
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="form-grid">
          <Field label="Tenant">
            {(id) => (
              <select id={id} value={forTenant}
                onChange={(e) => { setOffset(0); setForTenant(e.target.value); }}>
                <option value="">Every tenant</option>
                {tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            )}
          </Field>
          <Field label="Action contains">
            {(id) => <input id={id} className="mono" value={action}
              onChange={(e) => { setOffset(0); setAction(e.target.value); }}
              placeholder="tenant.updated" />}
          </Field>
          <Field label="User email contains">
            {(id) => <input id={id} value={userEmail}
              onChange={(e) => { setOffset(0); setUserEmail(e.target.value); }} />}
          </Field>
          <Field label="Window">
            {(id) => (
              <select id={id} value={days}
                onChange={(e) => { setOffset(0); setDays(Number(e.target.value)); }}>
                <option value={1}>Last 24 hours</option>
                <option value={7}>Last 7 days</option>
                <option value={30}>Last 30 days</option>
                <option value={90}>Last 90 days</option>
                <option value={365}>Last year</option>
              </select>
            )}
          </Field>
        </div>
      </div>

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading the audit trail…" />
        ) : rows.length === 0 ? (
          <EmptyState title="Nothing matched">
            No entries in this window with these filters.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th><th>Action</th><th>Resource</th>
                <th>Who</th><th>Tenant</th><th>From</th>
                <th style={{ textAlign: "right" }}>Detail</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="small mono" title={row.occurred_at}>
                    {new Date(row.occurred_at).toLocaleString()}
                  </td>
                  <td><Badge tone={tone(row.action)}>{row.action}</Badge></td>
                  <td className="small">
                    {row.resource_type}
                    {row.resource_id && (
                      <div className="subtle small mono">{row.resource_id.slice(0, 8)}…</div>
                    )}
                  </td>
                  <td className="small">
                    {row.user_email ?? <span className="subtle">system</span>}
                  </td>
                  <td className="small">
                    {row.tenant_name ?? (row.tenant_id
                      ? <span className="subtle" title="The tenant was deleted after this entry">deleted tenant</span>
                      : <span className="subtle">platform</span>)}
                  </td>
                  <td className="small mono">{row.ip_address ?? "—"}</td>
                  <td>
                    <div className="cell-actions">
                      {(row.old_value || row.new_value) && (
                        <Button size="sm" onClick={() => setInspecting(row)}>View</Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {rows !== null && total > PAGE_SIZE && (
        <div className="row-between" style={{ marginTop: 12 }}>
          <span className="subtle small">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <div className="row">
            <Button size="sm" disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Previous
            </Button>
            <Button size="sm" disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}>
              Next
            </Button>
          </div>
        </div>
      )}

      {inspecting && (
        <Dialog
          title={inspecting.action}
          onClose={() => setInspecting(null)}
          footer={<Button onClick={() => setInspecting(null)}>Close</Button>}
        >
          <dl className="kv">
            <dt>When</dt>
            <dd className="mono small">{inspecting.occurred_at}</dd>
            <dt>Who</dt>
            <dd>{inspecting.user_email ?? "system"}</dd>
            <dt>Resource</dt>
            <dd className="mono small">
              {inspecting.resource_type} {inspecting.resource_id ?? ""}
            </dd>
            <dt>Request</dt>
            <dd className="mono small">{inspecting.request_id ?? "—"}</dd>
          </dl>

          {inspecting.old_value && (
            <>
              <div className="form-section-label">Before</div>
              <pre className="code-block">{JSON.stringify(inspecting.old_value, null, 2)}</pre>
            </>
          )}
          {inspecting.new_value && (
            <>
              <div className="form-section-label">After</div>
              <pre className="code-block">{JSON.stringify(inspecting.new_value, null, 2)}</pre>
            </>
          )}
          <p className="subtle small">
            Secrets are redacted when the entry is written, not when it is
            read — so a value visible here was never treated as a secret, which
            is itself worth knowing.
          </p>
        </Dialog>
      )}
    </Shell>
  );
}
