"use client";

/**
 * Long-lived LiveKit dispatch rules (spec 21, 12).
 *
 * A rule is created once and reused. Creating one per call is the defect
 * this screen exists to prevent — there is no "create for this call" action.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice,
  RelativeTime, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type DispatchRule, type DispatchRuleInput, type SipTrunk, type SyncStatus,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const EMPTY: DispatchRuleInput = {
  name: "",
  sip_trunk_id: null,
  room_strategy: "INDIVIDUAL",
  room_prefix: "",
  agent_dispatch_name: "voice-agent",
  matched_numbers: [],
};

export default function DispatchRulesPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<DispatchRule[] | null>(null);
  const [trunks, setTrunks] = useState<SipTrunk[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<DispatchRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<DispatchRule | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const canWrite = can("sip_trunks.write");

  const load = useCallback(async () => {
    try {
      const [rules, trunkPage] = await Promise.all([
        api.dispatchRules.list(),
        api.sipTrunks.list(200),
      ]);
      setRows(rules.items);
      setTrunks(trunkPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load dispatch rules");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Dispatch Rules</h1>
          <p className="page-subtitle">
            Long-lived LiveKit rules. Each one names the worker that joins a
            call on a trunk. They are never created per call.
          </p>
        </div>
        {canWrite && (
          <Button variant="primary" onClick={() => setCreating(true)}>Create a rule</Button>
        )}
      </div>

      {error && <Notice tone="err">{error}</Notice>}
      <Notice tone="info">
        The worker must register as the rule&apos;s agent dispatch name
        (<code>WORKER_AGENT_NAME</code>). Which AI agent speaks is the DID&apos;s
        inbound agent — this page only decides which worker LiveKit invites.
      </Notice>

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading dispatch rules…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No dispatch rules"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Create a rule</Button> : undefined}
          >
            Without a rule, LiveKit will answer SIP and put the call in a room
            with nobody to speak.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Trunk / DIDs</th><th>Agent dispatch</th>
                <th>LiveKit</th><th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((rule) => (
                <tr key={rule.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{rule.name}</div>
                    <div className="subtle small mono">{rule.room_prefix || "no prefix"}</div>
                  </td>
                  <td className="small">
                    <div>{rule.sip_trunk_name ?? "no trunk"}</div>
                    <div className="subtle mono">
                      {rule.trunk_dids.length ? rule.trunk_dids.join(", ") : "no DIDs on trunk"}
                    </div>
                  </td>
                  <td className="mono small">{rule.agent_dispatch_name}</td>
                  <td><SyncBadge status={rule.sync_status} error={rule.sync_error} at={rule.last_synced_at} id={rule.livekit_resource_id} /></td>
                  <td>
                    <div className="cell-actions">
                      {canWrite && (
                        <>
                          <SyncActions
                            status={rule.sync_status}
                            busy={busyId === rule.id}
                            onAct={async (action) => {
                              setBusyId(rule.id);
                              try {
                                const updated = await (
                                  action === "repair"
                                    ? api.dispatchRules.repair(rule.id)
                                    : action === "retry"
                                      ? api.dispatchRules.retry(rule.id)
                                      : api.dispatchRules.sync(rule.id)
                                );
                                toasts.info(
                                  `${rule.name}: ${action} queued (${updated.sync_status.toLowerCase()})`,
                                );
                                await load();
                              } catch (err) {
                                toasts.err(err instanceof Error ? err.message : "the action failed");
                              } finally {
                                setBusyId(null);
                              }
                            }}
                          />
                          <Button size="sm" onClick={() => setEditing(rule)}>Edit</Button>
                          <Button size="sm" variant="danger" onClick={() => setDeleting(rule)}>Delete</Button>
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
        <RuleForm
          rule={editing}
          trunks={trunks}
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
                  await api.dispatchRules.remove(deleting.id);
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
            The LiveKit rule is removed first. If that fails this row stays,
            because an orphaned dispatch rule would keep matching calls.
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
  onAct(action: "synchronize" | "retry" | "repair"): void | Promise<void>;
}) {
  return (
    <>
      <Button size="sm" busy={busy} onClick={() => void onAct("synchronize")}>Synchronize</Button>
      {(status === "FAILED" || status === "PENDING") && (
        <Button size="sm" busy={busy} onClick={() => void onAct("retry")}>Retry</Button>
      )}
      {(status === "DRIFTED" || status === "FAILED") && (
        <Button size="sm" busy={busy} onClick={() => void onAct("repair")}>Repair</Button>
      )}
    </>
  );
}

function SyncBadge({
  status, error, at, id,
}: {
  status: SyncStatus;
  error: string | null;
  at: string | null;
  id: string | null;
}) {
  const tone: Record<SyncStatus, "ok" | "warn" | "err"> = {
    SYNCED: "ok", PENDING: "warn", FAILED: "err", DRIFTED: "err",
  };
  return (
    <div>
      <Badge tone={tone[status]} dot>{status.toLowerCase()}</Badge>
      <div className="subtle small truncate" style={{ marginTop: 2, maxWidth: 190 }}
           title={error ?? id ?? undefined}>
        {error ? error : at ? <RelativeTime iso={at} /> : "never synced"}
      </div>
    </div>
  );
}

function RuleForm({
  rule, trunks, onClose, onSaved,
}: {
  rule: DispatchRule | null;
  trunks: SipTrunk[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<DispatchRuleInput>(
    rule
      ? {
          name: rule.name,
          sip_trunk_id: rule.sip_trunk_id,
          room_strategy: "INDIVIDUAL",
          room_prefix: rule.room_prefix ?? "",
          agent_dispatch_name: rule.agent_dispatch_name,
          matched_numbers: rule.matched_numbers,
        }
      : EMPTY,
  );
  const [callerText, setCallerText] = useState((rule?.matched_numbers ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const selectedTrunk = trunks.find((trunk) => trunk.id === form.sip_trunk_id);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload: DispatchRuleInput = {
        ...form,
        name: form.name.trim(),
        agent_dispatch_name: form.agent_dispatch_name.trim(),
        room_prefix: form.room_prefix?.trim() || null,
        matched_numbers: callerText.split(",").map((s) => s.trim()).filter(Boolean),
        room_strategy: "INDIVIDUAL",
      };
      if (rule) {
        await api.dispatchRules.update(rule.id, payload);
        await onSaved(`${payload.name} saved — LiveKit sync is running in the background`);
      } else {
        const created = await api.dispatchRules.create(payload);
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
      title={rule ? `Edit ${rule.name}` : "Create a dispatch rule"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {rule ? "Save" : "Create"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required hint="Unique within your tenant. Long-lived — not per call.">
          {(id) => <input id={id} required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Inbound AI" />}
        </Field>

        <Field label="SIP trunk" required hint="The LiveKit rule attaches to this trunk's resource ID.">
          {(id) => (
            <select id={id} required value={form.sip_trunk_id ?? ""}
              onChange={(e) => setForm({ ...form, sip_trunk_id: e.target.value || null })}>
              <option value="">Select a trunk</option>
              {trunks.map((trunk) => (
                <option key={trunk.id} value={trunk.id}>{trunk.name}</option>
              ))}
            </select>
          )}
        </Field>

        {selectedTrunk && (
          <Notice tone="info">
            Calls to {selectedTrunk.accepted_numbers.length
              ? selectedTrunk.accepted_numbers.join(", ")
              : "any number on this trunk"} are dispatched to workers named below.
            The DID&apos;s inbound agent is who speaks.
          </Notice>
        )}

        <Field
          label="Agent dispatch name"
          required
          hint="Must match the worker's WORKER_AGENT_NAME."
        >
          {(id) => <input id={id} required value={form.agent_dispatch_name}
            onChange={(e) => setForm({ ...form, agent_dispatch_name: e.target.value })}
            className="mono" placeholder="voice-agent" />}
        </Field>

        <Field label="Room prefix" hint="INDIVIDUAL rooms only — one caller per room (spec 22).">
          {(id) => <input id={id} value={form.room_prefix ?? ""}
            onChange={(e) => setForm({ ...form, room_prefix: e.target.value })}
            className="mono" placeholder="acme-call-" />}
        </Field>

        <Field
          label="Caller number allow-list"
          hint="Optional. LiveKit matches the CALLER, not the dialled DID. Leave empty for any caller. Putting a DID here is how inbound calls get 486 flood."
        >
          {(id) => <input id={id} value={callerText} onChange={(e) => setCallerText(e.target.value)}
            placeholder="leave empty" />}
        </Field>
      </form>
    </Dialog>
  );
}
