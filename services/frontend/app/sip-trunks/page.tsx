"use client";

/**
 * SIP trunk management (spec 15) with LiveKit synchronisation state (spec 12, 46).
 *
 * The sync column is the point of this screen: a trunk row can be perfectly
 * valid while its LiveKit counterpart is missing or stale, and that difference
 * is invisible everywhere else.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice,
  RelativeTime, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type Pbx, type SipTransport, type SipTrunk, type SipTrunkInput, type SyncStatus,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const CODECS = ["PCMU", "PCMA", "OPUS", "G722"] as const;
const DTMF_MODES = ["RFC2833", "SIP_INFO", "INBAND"] as const;

const EMPTY: SipTrunkInput = {
  name: "", pbx_id: null, direction: "INBOUND", sip_host: "",
  port: 5060, transport: "UDP", allowed_ips: [], auth_username: "",
  auth_password: "", media_encryption_required: false,
  codecs: ["PCMU", "PCMA", "OPUS"], dtmf_mode: "RFC2833",
};

export default function SipTrunksPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<SipTrunk[] | null>(null);
  const [pbxs, setPbxs] = useState<Pbx[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<SipTrunk | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<SipTrunk | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const canWrite = can("sip_trunks.write");

  const load = useCallback(async () => {
    try {
      const [trunks, pbxPage] = await Promise.all([api.sipTrunks.list(), api.pbxs.list(200)]);
      setRows(trunks.items);
      setPbxs(pbxPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load trunks");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  async function act(
    trunk: SipTrunk,
    what: "test" | "synchronize" | "retry" | "repair" | "toggle",
  ) {
    setBusyId(trunk.id);
    try {
      if (what === "test") {
        const outcome = await api.sipTrunks.test(trunk.id);
        (outcome.result === "PASSED" ? toasts.ok : toasts.err)(
          `${trunk.name}: ${outcome.detail}` +
            (outcome.latency_ms ? ` (${outcome.latency_ms} ms)` : ""),
        );
      } else if (what === "synchronize" || what === "retry" || what === "repair") {
        const updated = await (
          what === "repair"
            ? api.sipTrunks.repair(trunk.id)
            : what === "retry"
              ? api.sipTrunks.retry(trunk.id)
              : api.sipTrunks.sync(trunk.id)
        );
        toasts.info(
          `${trunk.name}: ${what} queued (${updated.sync_status.toLowerCase()})`,
        );
      } else {
        await (trunk.status === "ACTIVE"
          ? api.sipTrunks.disable(trunk.id)
          : api.sipTrunks.enable(trunk.id));
        toasts.ok(`${trunk.name} ${trunk.status === "ACTIVE" ? "disabled" : "enabled"}`);
      }
      await load();
    } catch (err) {
      toasts.err(err instanceof Error ? err.message : "the action failed");
    } finally {
      setBusyId(null);
    }
  }

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>SIP Trunks</h1>
          <p className="page-subtitle">
            Each trunk is mirrored into LiveKit, which is what actually accepts
            calls. The row here is the source of truth; the sync column shows
            whether LiveKit agrees.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Create a trunk</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}
      {!canWrite && (
        <Notice tone="info">
          Your role grants read access. Creating and editing requires <code>sip_trunks.write</code>.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading trunks…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No SIP trunks"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Create a trunk</Button> : undefined}
          >
            A trunk is how calls reach the platform from your PBX. Create one,
            then assign phone numbers to it.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Address</th><th>Accepts</th><th>Auth</th>
                <th>LiveKit</th><th>Status</th><th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((trunk) => (
                <tr key={trunk.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{trunk.name}</div>
                    <div className="subtle small">{trunk.direction.toLowerCase()}</div>
                  </td>
                  <td className="mono small">
                    {trunk.sip_host}:{trunk.port}
                    <span className="subtle"> / {trunk.transport}</span>
                  </td>
                  <td className="mono small">
                    {trunk.accepted_numbers.length === 0 ? (
                      <span className="subtle">any number</span>
                    ) : (
                      trunk.accepted_numbers.join(", ")
                    )}
                  </td>
                  <td className="small">
                    {trunk.has_credential ? (
                      <Badge tone="ok">configured</Badge>
                    ) : (
                      <Badge tone="neutral">none</Badge>
                    )}
                  </td>
                  <td><SyncBadge trunk={trunk} /></td>
                  <td>
                    {trunk.status === "ACTIVE"
                      ? <Badge tone="ok" dot>active</Badge>
                      : <Badge tone="neutral">{trunk.status.toLowerCase()}</Badge>}
                  </td>
                  <td>
                    <div className="cell-actions">
                      <Button size="sm" busy={busyId === trunk.id} onClick={() => void act(trunk, "test")}>Test</Button>
                      {canWrite && (
                        <>
                          <SyncActions
                            status={trunk.sync_status}
                            busy={busyId === trunk.id}
                            onAct={(action) => void act(trunk, action)}
                          />
                          <Button size="sm" onClick={() => setEditing(trunk)}>Edit</Button>
                          <Button size="sm" onClick={() => void act(trunk, "toggle")}>
                            {trunk.status === "ACTIVE" ? "Disable" : "Enable"}
                          </Button>
                          <Button size="sm" variant="danger" onClick={() => setDeleting(trunk)}>Delete</Button>
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
        <TrunkForm
          trunk={editing}
          pbxs={pbxs}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Delete ${deleting.name}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.sipTrunks.remove(deleting.id);
                  toasts.ok(`${deleting.name} deleted`);
                } catch (err) {
                  toasts.err(err instanceof Error ? err.message : "could not delete");
                }
                setDeleting(null);
                await load();
              }}>Delete</Button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            The LiveKit resource is removed first. If that fails the trunk is kept,
            because an orphaned LiveKit trunk would keep accepting calls for a
            configuration that no longer exists.
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            Deletion is refused while phone numbers are still assigned to this trunk.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function SyncActions({
  status, busy, onAct,
}: {
  status: SyncStatus;
  busy: boolean;
  onAct(action: "synchronize" | "retry" | "repair"): void;
}) {
  return (
    <>
      <Button size="sm" busy={busy} onClick={() => onAct("synchronize")}>Synchronize</Button>
      {(status === "FAILED" || status === "PENDING") && (
        <Button size="sm" busy={busy} onClick={() => onAct("retry")}>Retry</Button>
      )}
      {(status === "DRIFTED" || status === "FAILED") && (
        <Button size="sm" busy={busy} onClick={() => onAct("repair")}>Repair</Button>
      )}
    </>
  );
}

function SyncBadge({ trunk }: { trunk: SipTrunk }) {
  const tone: Record<SyncStatus, "ok" | "warn" | "err" | "info"> = {
    SYNCED: "ok", PENDING: "warn", FAILED: "err", DRIFTED: "err",
  };
  return (
    <div>
      <Badge tone={tone[trunk.sync_status]} dot>{trunk.sync_status.toLowerCase()}</Badge>
      <div className="subtle small truncate" style={{ marginTop: 2, maxWidth: 190 }}
           title={trunk.sync_error ?? trunk.livekit_resource_id ?? undefined}>
        {trunk.sync_error
          ? trunk.sync_error
          : trunk.last_synced_at
            ? <RelativeTime iso={trunk.last_synced_at} />
            : "never synced"}
      </div>
    </div>
  );
}

function TrunkForm({
  trunk, pbxs, onClose, onSaved,
}: {
  trunk: SipTrunk | null;
  pbxs: Pbx[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<SipTrunkInput>(
    trunk
      ? {
          name: trunk.name, pbx_id: trunk.pbx_id, direction: trunk.direction,
          sip_host: trunk.sip_host, port: trunk.port, transport: trunk.transport,
          allowed_ips: trunk.allowed_ips, auth_username: trunk.auth_username ?? "",
          auth_password: "", media_encryption_required: trunk.media_encryption_required,
          codecs: trunk.codecs, dtmf_mode: trunk.dtmf_mode,
        }
      : EMPTY,
  );
  const [allowedText, setAllowedText] = useState((trunk?.allowed_ips ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload: SipTrunkInput = {
        ...form,
        name: form.name.trim(),
        sip_host: form.sip_host.trim(),
        auth_username: form.auth_username?.trim() || null,
        allowed_ips: allowedText.split(",").map((s) => s.trim()).filter(Boolean),
      };
      // An empty password on edit means "leave it alone", not "clear it".
      if (!payload.auth_password) delete payload.auth_password;

      if (trunk) {
        await api.sipTrunks.update(trunk.id, payload);
        await onSaved(`${payload.name} saved — LiveKit sync is running in the background`);
      } else {
        const created = await api.sipTrunks.create(payload);
        await onSaved(
          `${created.name} saved — LiveKit sync is running in the background (${created.sync_status.toLowerCase()})`,
        );
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={trunk ? `Edit ${trunk.name}` : "Create a SIP trunk"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {trunk ? "Save" : "Create"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required hint="Unique within your tenant.">
          {(id) => <input id={id} required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Head Office Trunk" />}
        </Field>

        <Field label="PBX" hint="Optional — groups this trunk with a PBX record.">
          {(id) => (
            <select id={id} value={form.pbx_id ?? ""}
              onChange={(e) => setForm({ ...form, pbx_id: e.target.value || null })}>
              <option value="">Not linked</option>
              {pbxs.map((pbx) => <option key={pbx.id} value={pbx.id}>{pbx.name}</option>)}
            </select>
          )}
        </Field>

        <Field label="Direction" required hint="LiveKit inbound trunks accept PBX→platform calls.">
          {(id) => (
            <select id={id} value={form.direction}
              onChange={(e) => setForm({ ...form, direction: e.target.value as typeof form.direction })}>
              <option value="INBOUND">Inbound</option>
              <option value="OUTBOUND">Outbound</option>
              <option value="BIDIRECTIONAL">Bidirectional</option>
            </select>
          )}
        </Field>

        <div className="field-row">
          <Field label="SIP host" required>
            {(id) => <input id={id} required value={form.sip_host}
              onChange={(e) => setForm({ ...form, sip_host: e.target.value })} placeholder="192.168.0.113" />}
          </Field>
          <Field label="Port" required>
            {(id) => <input id={id} type="number" min={1} max={65535} required value={form.port}
              onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} />}
          </Field>
          <Field label="Transport" required>
            {(id) => (
              <select id={id} value={form.transport}
                onChange={(e) => setForm({ ...form, transport: e.target.value as SipTransport })}>
                <option value="UDP">UDP</option><option value="TCP">TCP</option><option value="TLS">TLS</option>
              </select>
            )}
          </Field>
        </div>

        <div className="field-row">
          <Field label="Auth username" hint="For LiveKit's digest challenge.">
            {(id) => <input id={id} value={form.auth_username ?? ""}
              onChange={(e) => setForm({ ...form, auth_username: e.target.value })} autoComplete="off" />}
          </Field>
          <Field
            label="Auth password"
            hint={trunk?.has_credential ? "Configured — leave blank to keep it." : "Stored encrypted; never shown again."}
          >
            {(id) => <input id={id} type="password" value={form.auth_password ?? ""}
              onChange={(e) => setForm({ ...form, auth_password: e.target.value })} autoComplete="new-password" />}
          </Field>
        </div>

        <Field label="Allowed source IPs" hint="Comma-separated. Empty means no IP restriction.">
          {(id) => <input id={id} value={allowedText} onChange={(e) => setAllowedText(e.target.value)}
            placeholder="192.168.0.113, 10.0.0.0/24" />}
        </Field>

        <Field label="Codecs" hint="Offer at least PCMU for telephony.">
          {() => (
            <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
              {CODECS.map((codec) => (
                <label key={codec} className="row" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={(form.codecs ?? []).includes(codec)}
                    onChange={() => {
                      const current = form.codecs ?? [];
                      setForm({
                        ...form,
                        codecs: current.includes(codec)
                          ? current.filter((item) => item !== codec)
                          : [...current, codec],
                      });
                    }}
                  />
                  <span className="mono small">{codec}</span>
                </label>
              ))}
            </div>
          )}
        </Field>

        <div className="field-row">
          <Field label="DTMF">
            {(id) => (
              <select id={id} value={form.dtmf_mode ?? ""}
                onChange={(e) => setForm({ ...form, dtmf_mode: e.target.value || null })}>
                <option value="">Provider default</option>
                {DTMF_MODES.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
              </select>
            )}
          </Field>
          <Field label="Media encryption">
            {(id) => (
              <label className="row" style={{ gap: 8 }}>
                <input id={id} type="checkbox"
                  checked={form.media_encryption_required ?? false}
                  onChange={(e) => setForm({ ...form, media_encryption_required: e.target.checked })}
                />
                <span className="small">Require SRTP</span>
              </label>
            )}
          </Field>
        </div>

        <Notice tone="info">
          The numbers a trunk accepts come from the phone numbers assigned to it,
          not from this form — so the two can never disagree. Assign them on the
          Phone Numbers page.
        </Notice>
      </form>
    </Dialog>
  );
}
