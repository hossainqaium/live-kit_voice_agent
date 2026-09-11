"use client";

/**
 * Platform staff (spec 8, 60).
 *
 * Platform accounts have no tenant, so the tenant console refuses every
 * request they make. That is deliberate — acting inside a tenant is an
 * explicit operation, not something a platform administrator does by default.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, RelativeTime,
  ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, PLATFORM_ROLE_LABELS,
  type PlatformRole, type PlatformUser,
} from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

const ROLES: PlatformRole[] = ["SUPER_ADMIN", "PLATFORM_OPERATOR"];

const ROLE_NOTES: Record<PlatformRole, string> = {
  SUPER_ADMIN: "Everything: tenants, the AI catalog, platform accounts.",
  PLATFORM_OPERATOR: "Read the platform and act on incidents. Cannot create tenants or staff.",
};

export default function SystemUsersPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<PlatformUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<PlatformUser | null>(null);

  const canWrite = principal?.roles.includes("SUPER_ADMIN") ?? false;

  const load = useCallback(async () => {
    try {
      const page = await api.platform.users.list();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load platform accounts");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const admins = (rows ?? []).filter(
    (row) => row.is_active && row.roles.includes("SUPER_ADMIN"),
  );

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>System Users</h1>
          <p className="page-subtitle">
            Accounts that administer the platform itself. Tenant users are
            managed inside their own tenant.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add an account</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {!canWrite && (
        <Notice tone="info">
          Creating and changing platform accounts needs SUPER_ADMIN — an
          operator able to mint a super administrator would make the role
          distinction decorative.
        </Notice>
      )}

      {rows !== null && admins.length === 1 && (
        <Notice tone="warn">
          {admins[0].email} is the only active super administrator. If that
          account is lost, nothing in the platform console can create another
          one — the CLI's create-user command is the only way back.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading accounts…" />
        ) : rows.length === 0 ? (
          <EmptyState title="No platform accounts">
            That should not be possible while you are signed in as one. If this
            is empty, the account you are using may not be flagged as platform
            staff.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Account</th><th>Role</th><th>Last signed in</th>
                <th>Status</th><th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const isSelf = row.id === principal.user_id;
                return (
                  <tr key={row.id}>
                    <td>
                      <div style={{ fontWeight: 500 }}>
                        {row.full_name}
                        {isSelf && <span className="subtle small"> — you</span>}
                      </div>
                      <div className="subtle small">{row.email}</div>
                    </td>
                    <td>
                      {row.roles.length === 0 ? (
                        <Badge tone="warn">no role</Badge>
                      ) : (
                        row.roles.map((role) => (
                          <Badge key={role} tone={role === "SUPER_ADMIN" ? "info" : "neutral"}>
                            {PLATFORM_ROLE_LABELS[role as PlatformRole] ?? role}
                          </Badge>
                        ))
                      )}
                    </td>
                    <td className="small">
                      {row.last_login_at
                        ? <RelativeTime iso={row.last_login_at} />
                        : <span className="subtle">never</span>}
                    </td>
                    <td>
                      {row.is_active
                        ? <Badge tone="ok" dot>active</Badge>
                        : <Badge tone="neutral">disabled</Badge>}
                    </td>
                    <td>
                      <div className="cell-actions">
                        {canWrite && <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 style={{ marginTop: 0, marginBottom: 10, fontSize: 15 }}>The two platform roles</h2>
        <dl className="kv">
          {ROLES.map((role) => (
            <div key={role} style={{ display: "contents" }}>
              <dt>{PLATFORM_ROLE_LABELS[role]}</dt>
              <dd>{ROLE_NOTES[role]}</dd>
            </div>
          ))}
        </dl>
      </div>

      {(creating || editing) && (
        <StaffForm
          user={editing}
          isSelf={editing?.id === principal.user_id}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function StaffForm({
  user, isSelf, onClose, onSaved,
}: {
  user: PlatformUser | null;
  isSelf: boolean;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [email, setEmail] = useState(user?.email ?? "");
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [role, setRole] = useState<PlatformRole>(
    (user?.roles[0] as PlatformRole | undefined) ?? "PLATFORM_OPERATOR",
  );
  const [password, setPassword] = useState("");
  const [isActive, setIsActive] = useState(user?.is_active ?? true);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const demotingSelf = isSelf && role !== "SUPER_ADMIN";

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      if (user) {
        await api.platform.users.update(user.id, {
          full_name: fullName.trim(),
          role,
          is_active: isActive,
          // Omitted rather than sent empty, so leaving the field blank keeps
          // the existing password instead of clearing it.
          ...(password.trim() ? { password } : {}),
        });
        await onSaved(`${user.email} updated`);
      } else {
        await api.platform.users.create({
          email: email.trim(),
          full_name: fullName.trim(),
          password,
          role,
        });
        await onSaved(`${email.trim()} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={user ? `Edit ${user.email}` : "Add a platform account"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {user ? "Save changes" : "Add account"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field
          label="Email" required
          hint={user ? "The address cannot be changed." : "Used to sign in. Platform accounts have no tenant."}
        >
          {(id) => <input id={id} type="email" required disabled={Boolean(user)}
            value={email} onChange={(e) => setEmail(e.target.value)} />}
        </Field>

        <Field label="Full name" required>
          {(id) => <input id={id} required value={fullName}
            onChange={(e) => setFullName(e.target.value)} />}
        </Field>

        <Field label="Role" required hint={ROLE_NOTES[role]}>
          {(id) => (
            <select id={id} value={role} disabled={isSelf}
              onChange={(e) => setRole(e.target.value as PlatformRole)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>{PLATFORM_ROLE_LABELS[r]}</option>
              ))}
            </select>
          )}
        </Field>

        <Field
          label={user ? "New password" : "Password"}
          required={!user}
          hint={user
            ? "Leave empty to keep the current one. Setting it revokes the account's sessions."
            : "At least 12 characters."}
        >
          {(id) => <input id={id} type="password" required={!user} minLength={12}
            autoComplete="new-password" value={password}
            onChange={(e) => setPassword(e.target.value)} />}
        </Field>

        {user && (
          <Field
            label="Account"
            hint={isSelf
              ? "You cannot disable your own account."
              : "Disabling revokes existing sessions immediately."}
          >
            {(id) => (
              <label className="row" style={{ gap: 8 }}>
                <input id={id} type="checkbox" checked={isActive} disabled={isSelf}
                  onChange={(e) => setIsActive(e.target.checked)} />
                <span className="small">Allowed to sign in</span>
              </label>
            )}
          </Field>
        )}

        {demotingSelf && (
          <Notice tone="err">
            The API refuses to remove your own SUPER_ADMIN role: doing so could
            leave the platform with nobody able to grant it back.
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
