"use client";

/**
 * PBX management (spec 14).
 *
 * Create, edit, delete, enable, disable and test — the operations spec 14
 * names. Controls the signed-in role cannot use are hidden rather than shown
 * and rejected, but that is presentation only: the API enforces every
 * permission independently, and a hidden button is not a missing one.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  Field,
  Loading,
  Notice,
  RelativeTime,
  ToastStack,
  useToasts,
} from "@/components/ui";
import {
  ApiError,
  PBX_TYPE_LABELS,
  api,
  type Pbx,
  type PbxInput,
  type PbxType,
  type SipTransport,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const EMPTY_FORM: PbxInput = {
  name: "",
  pbx_type: "FREESWITCH",
  host: "",
  port: 5060,
  transport: "UDP",
  description: "",
};

export default function PbxPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<Pbx[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Pbx | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Pbx | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  const canWrite = can("pbxs.write");

  const load = useCallback(async () => {
    try {
      const page = await api.pbxs.list(200, 0);
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load PBXs");
      setRows([]);
    }
  }, []);

  useEffect(() => {
    if (principal) void load();
  }, [principal, load]);

  async function onTest(pbx: Pbx) {
    setTestingId(pbx.id);
    try {
      const outcome = await api.pbxs.test(pbx.id);
      if (outcome.result === "PASSED") {
        toasts.ok(
          `${pbx.name}: ${outcome.detail}` +
            (outcome.latency_ms ? ` (${outcome.latency_ms} ms)` : ""),
        );
      } else {
        toasts.err(`${pbx.name}: ${outcome.detail}`);
      }
      await load();
    } catch (err) {
      toasts.err(err instanceof Error ? err.message : "the test could not be run");
    } finally {
      setTestingId(null);
    }
  }

  async function onToggle(pbx: Pbx) {
    try {
      const next = pbx.status === "ACTIVE" ? "disabled" : "enabled";
      await (pbx.status === "ACTIVE" ? api.pbxs.disable(pbx.id) : api.pbxs.enable(pbx.id));
      toasts.ok(`${pbx.name} ${next}`);
      await load();
    } catch (err) {
      toasts.err(err instanceof Error ? err.message : "could not change the status");
    }
  }

  async function onDelete(pbx: Pbx) {
    try {
      await api.pbxs.remove(pbx.id);
      toasts.ok(`${pbx.name} deleted`);
      setDeleting(null);
      await load();
    } catch (err) {
      // A 409 here is the referential-integrity guard, and its message names
      // the trunks or DIDs still pointing at this PBX — worth showing verbatim
      // rather than replacing with something generic.
      toasts.err(err instanceof Error ? err.message : "could not delete");
      setDeleting(null);
    }
  }

  if (authLoading || !principal) {
    return (
      <div className="auth-screen">
        <Loading />
      </div>
    );
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>PBXs</h1>
          <p className="page-subtitle">
            SIP-compatible systems this tenant operates. Registering one records
            where to send transferred calls and groups the trunks that belong to
            it — the platform does not provision the PBX itself.
          </p>
        </div>
        {canWrite && (
          <Button variant="primary" onClick={() => setCreating(true)}>
            Register a PBX
          </Button>
        )}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {!canWrite && (
        <Notice tone="info">
          Your role grants read access to PBXs. Creating and editing requires{" "}
          <code>pbxs.write</code>.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading PBXs…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No PBXs registered"
            action={
              canWrite ? (
                <Button variant="primary" onClick={() => setCreating(true)}>
                  Register a PBX
                </Button>
              ) : undefined
            }
          >
            A PBX is the SIP system calls arrive from and transfers go back to.
            Register one to begin.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Address</th>
                <th>Status</th>
                <th>Reachability</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((pbx) => (
                <tr key={pbx.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{pbx.name}</div>
                    {pbx.description && (
                      <div className="subtle small truncate" style={{ maxWidth: 260 }}>
                        {pbx.description}
                      </div>
                    )}
                  </td>
                  <td className="small">{PBX_TYPE_LABELS[pbx.pbx_type]}</td>
                  <td className="mono small">
                    {pbx.host}:{pbx.port}
                    <span className="subtle"> / {pbx.transport}</span>
                  </td>
                  <td>
                    {pbx.status === "ACTIVE" ? (
                      <Badge tone="ok" dot>
                        active
                      </Badge>
                    ) : (
                      <Badge tone="neutral">{pbx.status.toLowerCase()}</Badge>
                    )}
                  </td>
                  <td>
                    <Reachability pbx={pbx} />
                  </td>
                  <td>
                    <div className="cell-actions">
                      <Button
                        size="sm"
                        busy={testingId === pbx.id}
                        onClick={() => void onTest(pbx)}
                        title="Send a SIP OPTIONS request and report the reply"
                      >
                        Test
                      </Button>
                      {canWrite && (
                        <>
                          <Button size="sm" onClick={() => setEditing(pbx)}>
                            Edit
                          </Button>
                          <Button size="sm" onClick={() => void onToggle(pbx)}>
                            {pbx.status === "ACTIVE" ? "Disable" : "Enable"}
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            onClick={() => setDeleting(pbx)}
                          >
                            Delete
                          </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <PbxForm
          pbx={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={async (message) => {
            setCreating(false);
            setEditing(null);
            toasts.ok(message);
            await load();
          }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Delete ${deleting.name}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={() => void onDelete(deleting)}>
                Delete
              </Button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            This cannot be undone. Deletion is refused while SIP trunks or phone
            numbers still reference this PBX, so routing cannot be orphaned by
            accident.
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            To take a PBX out of service without deleting it, disable it
            instead — references and history are kept.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function Reachability({ pbx }: { pbx: Pbx }) {
  if (pbx.last_test_result === "PASSED") {
    return (
      <div>
        <Badge tone="ok" dot>
          reachable
        </Badge>
        <div className="subtle small" style={{ marginTop: 2 }}>
          <RelativeTime iso={pbx.last_tested_at} />
        </div>
      </div>
    );
  }
  if (pbx.last_test_result === "FAILED") {
    return (
      <div>
        <Badge tone="err" dot>
          unreachable
        </Badge>
        <div
          className="subtle small truncate"
          style={{ marginTop: 2, maxWidth: 220 }}
          title={pbx.last_test_detail ?? undefined}
        >
          {pbx.last_test_detail}
        </div>
      </div>
    );
  }
  return (
    <div>
      <Badge tone="neutral">untested</Badge>
      <div className="subtle small" style={{ marginTop: 2 }}>
        saved, never probed
      </div>
    </div>
  );
}

function PbxForm({
  pbx,
  onClose,
  onSaved,
}: {
  pbx: Pbx | null;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<PbxInput>(
    pbx
      ? {
          name: pbx.name,
          pbx_type: pbx.pbx_type,
          host: pbx.host,
          port: pbx.port,
          transport: pbx.transport,
          description: pbx.description ?? "",
        }
      : EMPTY_FORM,
  );
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const addressChanged =
    pbx !== null &&
    (form.host !== pbx.host || form.port !== pbx.port || form.transport !== pbx.transport);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload: PbxInput = {
        ...form,
        name: form.name.trim(),
        host: form.host.trim(),
        description: form.description?.trim() || null,
      };
      if (pbx) {
        await api.pbxs.update(pbx.id, payload);
        await onSaved(`${payload.name} updated`);
      } else {
        await api.pbxs.create(payload);
        await onSaved(`${payload.name} registered`);
      }
    } catch (err) {
      // Surfaced in the dialog rather than as a toast: the message usually
      // names the field to fix, and the form is where that is actionable.
      setFormError(
        err instanceof ApiError ? err.message : "could not save — is the API reachable?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={pbx ? `Edit ${pbx.name}` : "Register a PBX"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {pbx ? "Save changes" : "Register"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}

      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required hint="Unique within your tenant.">
          {(id) => (
            <input
              id={id}
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Head Office PBX"
            />
          )}
        </Field>

        <Field label="Type" required>
          {(id) => (
            <select
              id={id}
              value={form.pbx_type}
              onChange={(e) => setForm({ ...form, pbx_type: e.target.value as PbxType })}
            >
              {(Object.keys(PBX_TYPE_LABELS) as PbxType[]).map((type) => (
                <option key={type} value={type}>
                  {PBX_TYPE_LABELS[type]}
                </option>
              ))}
            </select>
          )}
        </Field>

        <div className="field-row">
          <Field
            label="Host"
            required
            hint="Hostname or IP — not a sip: URI."
          >
            {(id) => (
              <input
                id={id}
                required
                value={form.host}
                onChange={(e) => setForm({ ...form, host: e.target.value })}
                placeholder="192.168.0.113"
              />
            )}
          </Field>

          <Field label="Port" required>
            {(id) => (
              <input
                id={id}
                type="number"
                min={1}
                max={65535}
                required
                value={form.port}
                onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
              />
            )}
          </Field>

          <Field label="Transport" required>
            {(id) => (
              <select
                id={id}
                value={form.transport}
                onChange={(e) =>
                  setForm({ ...form, transport: e.target.value as SipTransport })
                }
              >
                <option value="UDP">UDP</option>
                <option value="TCP">TCP</option>
                <option value="TLS">TLS</option>
              </select>
            )}
          </Field>
        </div>

        <Field label="Description" hint="Optional note for your team.">
          {(id) => (
            <textarea
              id={id}
              rows={2}
              value={form.description ?? ""}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          )}
        </Field>

        {addressChanged && (
          <Notice tone="warn">
            Changing the address clears the reachability result. The record
            would otherwise keep reporting a test that was run against a
            different address — re-test after saving.
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
