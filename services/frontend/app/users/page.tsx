"use client";

/**
 * Tenant users and roles (spec 8).
 *
 * Only tenant roles are offered here. A tenant administrator granting a
 * platform role would be an escalation out of their own tenant, which the API
 * refuses — so the form does not present it as a possibility.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, RelativeTime,
  ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, TENANT_ROLE_LABELS,
  type ConsoleUser, type TenantRole,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const ROLES: TenantRole[] = ["TENANT_ADMIN", "MANAGER", "AGENT_MANAGER", "ANALYST", "VIEWER"];

const ROLE_NOTES: Record<TenantRole, string> = {
  TENANT_ADMIN: "Everything within this tenant, including users and billing.",
  MANAGER: "Agents, telephony and calls. No user management.",
  AGENT_MANAGER: "Agents and their versions, plus call history.",
  ANALYST: "Read-only: calls, recordings and analytics.",
  VIEWER: "Read-only configuration and call history.",
};

export default function UsersPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<ConsoleUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ConsoleUser | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<ConsoleUser | null>(null);
  const [resetting, setResetting] = useState<ConsoleUser | null>(null);

  const canManage = can("users.manage");

  const load = useCallback(async () => {
    try {
      const page = await api.users.list();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load users");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const admins = (rows ?? []).filter(
    (row) => row.is_active && row.roles.includes("TENANT_ADMIN"),
  );

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Users</h1>
          <p className="page-subtitle">
            Who can sign in to this tenant, and what each of them may do.
            Permissions are enforced by the API, so a role here is the real
            boundary and not just what the console shows.
          </p>
        </div>
        {canManage && <Button variant="primary" onClick={() => setCreating(true)}>Add a user</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {rows !== null && admins.length === 1 && (
        <Notice tone="warn">
          {admins[0].email} is the only active administrator. If that account is
          locked out, nobody can manage this tenant's users without platform
          help — consider a second administrator.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading users…" />
        ) : rows.length === 0 ? (
          <EmptyState title="No users" action={canManage ? <Button variant="primary" onClick={() => setCreating(true)}>Add a user</Button> : undefined}>
            Only you can reach this tenant. Add colleagues with the narrowest
            role that lets them do their job.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>User</th><th>Role</th><th>Last signed in</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
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
                    <td className="small">
                      {row.roles.length === 0 ? (
                        <Badge tone="warn">no role</Badge>
                      ) : (
                        row.roles.map((role) => (
                          <Badge key={role} tone={role === "TENANT_ADMIN" ? "info" : "neutral"}>
                            {TENANT_ROLE_LABELS[role as TenantRole] ?? role}
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
                        {canManage && (
                          <>
                            <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>
                            <Button size="sm" onClick={() => setResetting(row)}>Password</Button>
                            {!isSelf && (
                              <Button size="sm" variant="danger" onClick={() => setDeleting(row)}>Remove</Button>
                            )}
                          </>
                        )}
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
        <h2 style={{ marginTop: 0, marginBottom: 10, fontSize: 15 }}>What each role can do</h2>
        <dl className="kv">
          {ROLES.map((role) => (
            <div key={role} style={{ display: "contents" }}>
              <dt>{TENANT_ROLE_LABELS[role]}</dt>
              <dd>{ROLE_NOTES[role]}</dd>
            </div>
          ))}
        </dl>
      </div>

      {(creating || editing) && (
        <UserForm
          user={editing}
          isSelf={editing?.id === principal.user_id}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      {resetting && (
        <PasswordForm
          user={resetting}
          onClose={() => setResetting(null)}
          onSaved={async (msg) => { setResetting(null); toasts.ok(msg); await load(); }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Remove ${deleting.email}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.users.remove(deleting.id);
                  toasts.ok(`${deleting.email} removed`);
                } catch (err) {
                  toasts.err(err instanceof Error ? err.message : "could not remove");
                }
                setDeleting(null);
                await load();
              }}>Remove</Button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            The account is deleted and its sessions stop working. Audit entries
            recording what it did are kept — deleting a user must not erase the
            trail.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function UserForm({
  user, isSelf, onClose, onSaved,
}: {
  user: ConsoleUser | null;
  isSelf: boolean;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const currentRole = (user?.roles[0] as TenantRole | undefined) ?? "VIEWER";
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<TenantRole>(currentRole);
  const [isActive, setIsActive] = useState(user?.is_active ?? true);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      if (user) {
        await api.users.update(user.id, {
          full_name: fullName.trim(),
          role,
          is_active: isActive,
        });
        await onSaved(`${user.email} updated`);
      } else {
        await api.users.create({
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
      title={user ? `Edit ${user.email}` : "Add a user"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {user ? "Save changes" : "Add user"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field
          label="Email" required
          hint={user ? "The address cannot be changed — remove and re-add instead." : "Used to sign in."}
        >
          {(id) => <input id={id} type="email" required disabled={Boolean(user)}
            value={email} onChange={(e) => setEmail(e.target.value)} />}
        </Field>

        <Field label="Full name" required>
          {(id) => <input id={id} required value={fullName}
            onChange={(e) => setFullName(e.target.value)} />}
        </Field>

        {!user && (
          <Field
            label="Password" required
            hint="At least 12 characters. Hand it over out of band and have them change it."
          >
            {(id) => <input id={id} type="password" required minLength={12} autoComplete="new-password"
              value={password} onChange={(e) => setPassword(e.target.value)} />}
          </Field>
        )}

        <Field label="Role" required hint={ROLE_NOTES[role]}>
          {(id) => (
            <select id={id} value={role} onChange={(e) => setRole(e.target.value as TenantRole)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>{TENANT_ROLE_LABELS[r]}</option>
              ))}
            </select>
          )}
        </Field>

        {user && (
          <Field
            label="Account"
            hint={
              isSelf
                ? "You cannot disable your own account — the API refuses it."
                : "Disabling revokes existing sessions immediately rather than waiting for the token to expire."
            }
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
      </form>
    </Dialog>
  );
}

function PasswordForm({
  user, onClose, onSaved,
}: {
  user: ConsoleUser;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  return (
    <Dialog
      title={`Set a password for ${user.email}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={async () => {
            setBusy(true);
            setFormError(null);
            try {
              await api.users.setPassword(user.id, password);
              await onSaved(`password set for ${user.email}`);
            } catch (err) {
              setFormError(err instanceof ApiError ? err.message : "could not set the password");
            } finally {
              setBusy(false);
            }
          }}>
            Set password
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <Field label="New password" required hint="At least 12 characters.">
        {(id) => <input id={id} type="password" required minLength={12} autoComplete="new-password"
          value={password} onChange={(e) => setPassword(e.target.value)} />}
      </Field>
      <Notice tone="info">
        Every existing session for this account stops working immediately. A
        reset that left old tokens valid would not be a reset.
      </Notice>
    </Dialog>
  );
}
