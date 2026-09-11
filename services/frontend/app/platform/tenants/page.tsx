"use client";

/**
 * Tenants (spec 60).
 *
 * Creating a tenant optionally creates its first administrator in the same
 * request. Doing it in two steps means the second sometimes never happens, and
 * a tenant nobody can sign in to is a support ticket rather than a tenant.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type TenantInput, type TenantStatus, type TenantSummary,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const STATUSES: TenantStatus[] = ["ACTIVE", "SUSPENDED", "ARCHIVED"];

export default function TenantsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<TenantSummary[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<TenantSummary | null>(null);
  const [created, setCreated] = useState<TenantSummary | null>(null);

  // SUPER_ADMIN holds every permission, so this is the right proxy for "may
  // change the platform". The API enforces it either way.
  const canWrite = principal?.roles.includes("SUPER_ADMIN") ?? false;

  const load = useCallback(async (search: string) => {
    try {
      const page = await api.platform.tenants.list(search || undefined);
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load tenants");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(query); }, [principal, query, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Tenants</h1>
          <p className="page-subtitle">
            Every customer on this platform, with what each of them has
            configured and how much traffic they carry.
          </p>
        </div>
        <div className="row">
          <input
            value={query}
            placeholder="Search name or slug"
            aria-label="Search tenants"
            onChange={(event) => setQuery(event.target.value)}
          />
          {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a tenant</Button>}
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}
      {!canWrite && (
        <Notice tone="info">
          Creating and changing tenants needs SUPER_ADMIN. You can see them here.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading tenants…" />
        ) : rows.length === 0 ? (
          <EmptyState title={query ? "No tenant matched" : "No tenants"}>
            {query
              ? "Nothing matched that search."
              : "Create the first tenant and its administrator to get started."}
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Tenant</th><th>Status</th><th>Users</th><th>Agents</th>
                <th>Numbers</th><th>Calls (30d)</th><th>Live</th>
                <th>Concurrency</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{row.name}</div>
                    <div className="subtle small mono">{row.slug} · {row.timezone}</div>
                  </td>
                  <td>
                    {row.status === "ACTIVE"
                      ? <Badge tone="ok" dot>active</Badge>
                      : <Badge tone={row.status === "SUSPENDED" ? "warn" : "neutral"}>
                          {row.status.toLowerCase()}
                        </Badge>}
                  </td>
                  <td className="mono small">{row.user_count}</td>
                  <td className="mono small">{row.agent_count}</td>
                  <td className="mono small">{row.phone_number_count}</td>
                  <td className="mono small">{row.calls_last_30_days}</td>
                  <td className="mono small">
                    {row.active_calls > 0
                      ? <Badge tone="info" dot>{row.active_calls}</Badge>
                      : <span className="subtle">0</span>}
                  </td>
                  <td className="mono small">
                    {row.max_concurrent_calls ?? <span className="subtle">uncapped</span>}
                  </td>
                  <td>
                    <div className="cell-actions">
                      {canWrite && <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <TenantForm
          tenant={editing}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (message, result) => {
            setCreating(false);
            setEditing(null);
            toasts.ok(message);
            if (result?.admin_email) setCreated(result);
            await load(query);
          }}
        />
      )}

      {created && (
        <Dialog
          title={`${created.name} is ready`}
          onClose={() => setCreated(null)}
          footer={<Button variant="primary" onClick={() => setCreated(null)}>Done</Button>}
        >
          <p style={{ marginTop: 0 }}>
            The tenant and its first administrator exist. Give them the address
            below and the password you chose.
          </p>
          <dl className="kv">
            <dt>Administrator</dt>
            <dd className="mono">{created.admin_email}</dd>
            <dt>Tenant slug</dt>
            <dd className="mono">{created.slug}</dd>
          </dl>
          <Notice tone="warn">
            Send the password over a channel that is not this screen, and have
            them change it after the first sign-in. The API never returns it,
            so it cannot be recovered from here.
          </Notice>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function TenantForm({
  tenant, onClose, onSaved,
}: {
  tenant: TenantSummary | null;
  onClose(): void;
  onSaved(message: string, result?: TenantSummary): void | Promise<void>;
}) {
  const [form, setForm] = useState<TenantInput>({
    name: tenant?.name ?? "",
    slug: tenant?.slug ?? "",
    timezone: tenant?.timezone ?? "UTC",
    default_language: tenant?.default_language ?? "en",
    max_concurrent_calls: tenant?.max_concurrent_calls ?? null,
    max_daily_calls: tenant?.max_daily_calls ?? null,
    max_monthly_minutes: tenant?.max_monthly_minutes ?? null,
    notes: tenant?.notes ?? "",
  });
  const [status, setStatus] = useState<TenantStatus>(tenant?.status ?? "ACTIVE");
  const [withAdmin, setWithAdmin] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
  const [adminName, setAdminName] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const suspending = tenant !== null && tenant.status === "ACTIVE" && status !== "ACTIVE";

  /** A slug the API will accept, derived from the name as it is typed. */
  function slugify(value: string): string {
    return value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 100);
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      if (tenant) {
        const saved = await api.platform.tenants.update(tenant.id, {
          name: form.name.trim(),
          status,
          timezone: form.timezone,
          default_language: form.default_language,
          max_concurrent_calls: form.max_concurrent_calls,
          max_daily_calls: form.max_daily_calls,
          max_monthly_minutes: form.max_monthly_minutes,
          notes: form.notes?.trim() || null,
        });
        await onSaved(`${saved.name} updated`);
      } else {
        const payload: TenantInput = {
          ...form,
          name: form.name.trim(),
          slug: form.slug.trim(),
          notes: form.notes?.trim() || null,
        };
        if (withAdmin) {
          payload.admin_email = adminEmail.trim();
          payload.admin_full_name = adminName.trim();
          payload.admin_password = adminPassword;
        }
        const saved = await api.platform.tenants.create(payload);
        await onSaved(`${saved.name} created`, saved);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={tenant ? `Edit ${tenant.name}` : "Add a tenant"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {tenant ? "Save changes" : "Create tenant"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required>
          {(id) => <input id={id} required value={form.name}
            onChange={(e) => {
              const name = e.target.value;
              setForm((current) => ({
                ...current,
                name,
                // Only auto-fill while the slug is untouched, and never for an
                // existing tenant: its slug is in URLs and cannot change.
                slug: tenant || current.slug !== slugify(current.name) ? current.slug : slugify(name),
              }));
            }} />}
        </Field>

        <Field
          label="Slug" required
          hint={tenant
            ? "Fixed after creation — it appears in URLs and metadata."
            : "Lowercase letters, digits and hyphens. Unique across the platform."}
        >
          {(id) => <input id={id} required className="mono" disabled={Boolean(tenant)}
            value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />}
        </Field>

        <div className="form-grid">
          <Field label="Timezone">
            {(id) => <input id={id} className="mono" value={form.timezone ?? "UTC"}
              onChange={(e) => setForm({ ...form, timezone: e.target.value })} />}
          </Field>
          <Field label="Default language">
            {(id) => <input id={id} className="mono" value={form.default_language ?? "en"}
              onChange={(e) => setForm({ ...form, default_language: e.target.value })} />}
          </Field>
        </div>

        {tenant && (
          <Field
            label="Status"
            hint={suspending
              ? "Suspending revokes every session for this tenant immediately."
              : "Suspended and archived tenants cannot sign in."}
          >
            {(id) => (
              <select id={id} value={status} onChange={(e) => setStatus(e.target.value as TenantStatus)}>
                {STATUSES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
              </select>
            )}
          </Field>
        )}

        <div className="form-section-label">Limits</div>
        <p className="subtle small" style={{ marginTop: 0 }}>
          Leave a field empty for no limit. Only concurrency is enforced on the
          call path today; the other two are recorded.
        </p>

        <div className="form-grid">
          <Field label="Concurrent calls">
            {(id) => <input id={id} type="number" min={1} value={form.max_concurrent_calls ?? ""}
              onChange={(e) => setForm({
                ...form,
                max_concurrent_calls: e.target.value ? Number(e.target.value) : null,
              })} />}
          </Field>
          <Field label="Calls per day">
            {(id) => <input id={id} type="number" min={1} value={form.max_daily_calls ?? ""}
              onChange={(e) => setForm({
                ...form,
                max_daily_calls: e.target.value ? Number(e.target.value) : null,
              })} />}
          </Field>
          <Field label="Minutes per month">
            {(id) => <input id={id} type="number" min={1} value={form.max_monthly_minutes ?? ""}
              onChange={(e) => setForm({
                ...form,
                max_monthly_minutes: e.target.value ? Number(e.target.value) : null,
              })} />}
          </Field>
        </div>

        <Field label="Notes">
          {(id) => <textarea id={id} rows={2} value={form.notes ?? ""}
            onChange={(e) => setForm({ ...form, notes: e.target.value })} />}
        </Field>

        {!tenant && (
          <>
            <div className="form-section-label">First administrator</div>
            <label className="row" style={{ gap: 8, marginBottom: 10 }}>
              <input type="checkbox" checked={withAdmin}
                onChange={(e) => setWithAdmin(e.target.checked)} />
              <span className="small">
                Create an administrator account now, in the same transaction
              </span>
            </label>

            {withAdmin ? (
              <>
                <Field label="Email" required>
                  {(id) => <input id={id} type="email" required value={adminEmail}
                    onChange={(e) => setAdminEmail(e.target.value)} />}
                </Field>
                <Field label="Full name" required>
                  {(id) => <input id={id} required value={adminName}
                    onChange={(e) => setAdminName(e.target.value)} />}
                </Field>
                <Field label="Password" required hint="At least 12 characters.">
                  {(id) => <input id={id} type="password" required minLength={12}
                    autoComplete="new-password" value={adminPassword}
                    onChange={(e) => setAdminPassword(e.target.value)} />}
                </Field>
              </>
            ) : (
              <p className="subtle small" style={{ marginTop: 0 }}>
                Without one, nobody can sign in to this tenant until a platform
                administrator adds a user.
              </p>
            )}
          </>
        )}
      </form>
    </Dialog>
  );
}
