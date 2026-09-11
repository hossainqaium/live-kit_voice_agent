"use client";

/**
 * Transfer targets (spec 35, clarification CR-1).
 *
 * These are where a warm transfer sends the caller. ``whisper_summary`` is the
 * CR-1 behaviour: the human agent hears the AI's summary first while the
 * caller hears the announcement, and the legs bridge only afterwards. Turning
 * it off makes the transfer blind, which is why it is shown in the table
 * rather than buried in the form.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, TRANSFER_KIND_LABELS,
  type Pbx, type SipTrunk, type TransferDestination, type TransferDestinationInput,
  type TransferDestinationKind,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const KINDS: TransferDestinationKind[] = [
  "PBX_EXTENSION", "PBX_QUEUE", "SIP_EXTENSION", "EXTERNAL_NUMBER", "SIP_URI",
];

const TARGET_HINTS: Record<TransferDestinationKind, string> = {
  PBX_EXTENSION: "The extension to ring, e.g. 1001.",
  PBX_QUEUE: "The queue extension or name as the PBX knows it.",
  SIP_EXTENSION: "A SIP extension reachable through the chosen trunk.",
  EXTERNAL_NUMBER: "A full number in E.164, e.g. +8801700000000.",
  SIP_URI: "A complete URI, e.g. sip:support@pbx.example.com.",
};

export default function TransferDestinationsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<TransferDestination[] | null>(null);
  const [pbxs, setPbxs] = useState<Pbx[]>([]);
  const [trunks, setTrunks] = useState<SipTrunk[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<TransferDestination | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<TransferDestination | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const [page, pbxPage, trunkPage] = await Promise.all([
        api.transferDestinations.list(), api.pbxs.list(200), api.sipTrunks.list(200),
      ]);
      setRows(page.items);
      setPbxs(pbxPage.items);
      setTrunks(trunkPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load transfer targets");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const blindTransfers = (rows ?? []).filter((row) => !row.whisper_summary);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Transfer Targets</h1>
          <p className="page-subtitle">
            Where a call goes when the AI hands it to a person, and where a
            fallback sends it. The caller hears the transfer announcement while
            the target is dialled.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a target</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {blindTransfers.length > 0 && (
        <Notice tone="warn">
          {blindTransfers.length === 1
            ? `${blindTransfers[0].name} does not whisper the summary`
            : `${blindTransfers.length} targets do not whisper the summary`}
          , so the person answering picks up mid-conversation with no context.
          That is a blind transfer, not the warm one the platform performs by
          default.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading targets…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No transfer targets"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Add a target</Button> : undefined}
          >
            An agent with transfer enabled needs somewhere to transfer to. Add
            the extension or queue a human answers on.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Target</th><th>Kind</th><th>Dials</th><th>Through</th>
                <th>Summary first</th><th>Ring timeout</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const pbx = pbxs.find((p) => p.id === row.pbx_id);
                const trunk = trunks.find((t) => t.id === row.sip_trunk_id);
                return (
                  <tr key={row.id}>
                    <td style={{ fontWeight: 500 }}>{row.name}</td>
                    <td className="small">{TRANSFER_KIND_LABELS[row.kind]}</td>
                    <td className="small mono">{row.target}</td>
                    <td className="small">
                      {trunk?.name ?? pbx?.name ?? <span className="subtle">default trunk</span>}
                    </td>
                    <td>
                      {row.whisper_summary
                        ? <Badge tone="ok">warm</Badge>
                        : <Badge tone="warn">blind</Badge>}
                    </td>
                    <td className="small mono">{row.ring_timeout_seconds}s</td>
                    <td>
                      {row.status === "ACTIVE"
                        ? <Badge tone="ok" dot>active</Badge>
                        : <Badge tone="neutral">{row.status.toLowerCase()}</Badge>}
                    </td>
                    <td>
                      <div className="cell-actions">
                        {canWrite && (
                          <>
                            <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>
                            <Button size="sm" variant="danger" onClick={() => setDeleting(row)}>Remove</Button>
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

      {(creating || editing) && (
        <DestinationForm
          destination={editing} pbxs={pbxs} trunks={trunks}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Remove ${deleting.name}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.transferDestinations.remove(deleting.id);
                  toasts.ok(`${deleting.name} removed`);
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
            A routing rule or agent still pointing here would have nowhere to
            send a transfer, so the API refuses while one references it.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function DestinationForm({
  destination, pbxs, trunks, onClose, onSaved,
}: {
  destination: TransferDestination | null;
  pbxs: Pbx[]; trunks: SipTrunk[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<TransferDestinationInput>({
    name: destination?.name ?? "",
    kind: destination?.kind ?? "PBX_EXTENSION",
    target: destination?.target ?? "",
    pbx_id: destination?.pbx_id ?? null,
    sip_trunk_id: destination?.sip_trunk_id ?? null,
    whisper_summary: destination?.whisper_summary ?? true,
    ring_timeout_seconds: destination?.ring_timeout_seconds ?? 30,
  });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload = { ...form, name: form.name.trim(), target: form.target.trim() };
      if (destination) {
        await api.transferDestinations.update(destination.id, payload);
        await onSaved(`${payload.name} updated`);
      } else {
        await api.transferDestinations.create(payload);
        await onSaved(`${payload.name} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={destination ? `Edit ${destination.name}` : "Add a transfer target"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {destination ? "Save changes" : "Add target"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required hint="What this is, in the operator's words.">
          {(id) => <input id={id} required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Front desk" />}
        </Field>

        <Field label="Kind" required>
          {(id) => (
            <select id={id} value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value as TransferDestinationKind })}>
              {KINDS.map((kind) => (
                <option key={kind} value={kind}>{TRANSFER_KIND_LABELS[kind]}</option>
              ))}
            </select>
          )}
        </Field>

        <Field label="Target" required hint={TARGET_HINTS[form.kind]}>
          {(id) => <input id={id} required className="mono" value={form.target}
            onChange={(e) => setForm({ ...form, target: e.target.value })} />}
        </Field>

        <Field label="PBX" hint="Which PBX owns this target. Optional, for the record.">
          {(id) => (
            <select id={id} value={form.pbx_id ?? ""}
              onChange={(e) => setForm({ ...form, pbx_id: e.target.value || null })}>
              <option value="">Not linked</option>
              {pbxs.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
        </Field>

        <Field label="Trunk" hint="The trunk the outbound leg goes out on. Leave empty for the tenant's only trunk.">
          {(id) => (
            <select id={id} value={form.sip_trunk_id ?? ""}
              onChange={(e) => setForm({ ...form, sip_trunk_id: e.target.value || null })}>
              <option value="">Default</option>
              {trunks.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          )}
        </Field>

        <Field label="Ring timeout" hint="Seconds to wait before the fallback applies.">
          {(id) => <input id={id} type="number" min={5} max={300}
            value={form.ring_timeout_seconds ?? 30}
            onChange={(e) => setForm({ ...form, ring_timeout_seconds: Number(e.target.value) })} />}
        </Field>

        <Field label="Summary first">
          {(id) => (
            <label className="row" style={{ gap: 8 }}>
              <input id={id} type="checkbox" checked={form.whisper_summary ?? true}
                onChange={(e) => setForm({ ...form, whisper_summary: e.target.checked })} />
              <span className="small">
                Whisper the AI conversation summary to the person answering
                before bridging the caller
              </span>
            </label>
          )}
        </Field>

        {form.whisper_summary === false && (
          <Notice tone="warn">
            With this off the caller is bridged the moment the target answers,
            so the person picks up mid-conversation with no context.
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
