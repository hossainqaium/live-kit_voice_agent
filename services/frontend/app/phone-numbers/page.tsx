"use client";

/**
 * Phone number (DID) management (spec 17).
 *
 * Assigning a number to a trunk is what changes which calls that trunk
 * accepts, so saving here re-syncs nothing directly — the trunk derives its
 * accepted list from these rows.
 */

import { useCallback, useEffect, useState } from "react";

import { BrowserCall } from "@/components/BrowserCall";
import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, PhoneIcon,
  ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, FALLBACK_ACTION_LABELS,
  type Agent, type BusinessHours, type Pbx, type PhoneNumber, type PhoneNumberInput,
  type DispatchRule, type RoutingRule, type SipTrunk,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

export default function PhoneNumbersPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<PhoneNumber[] | null>(null);
  const [pbxs, setPbxs] = useState<Pbx[]>([]);
  const [trunks, setTrunks] = useState<SipTrunk[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [rules, setRules] = useState<RoutingRule[]>([]);
  const [hours, setHours] = useState<BusinessHours[]>([]);
  const [dispatchRules, setDispatchRules] = useState<DispatchRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<PhoneNumber | null>(null);
  const [calling, setCalling] = useState<{ did: string; agent: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<PhoneNumber | null>(null);

  const canWrite = can("sip_trunks.write");

  const load = useCallback(async () => {
    try {
      const [numbers, pbxPage, trunkPage, agentPage, rulePage, hoursPage, dispatchPage] = await Promise.all([
        api.phoneNumbers.list(),
        api.pbxs.list(200),
        api.sipTrunks.list(200),
        api.agents.list(200),
        api.routingRules.list(200),
        api.businessHours.list(200),
        api.dispatchRules.list(200),
      ]);
      setRows(numbers.items);
      setPbxs(pbxPage.items);
      setTrunks(trunkPage.items);
      setAgents(agentPage.items);
      setRules(rulePage.items);
      setHours(hoursPage.items);
      setDispatchRules(dispatchPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load numbers");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Phone Numbers</h1>
          <p className="page-subtitle">
            The DIDs callers dial. A number's trunk decides how the call reaches
            the platform; its agent decides who answers.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a number</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading numbers…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No phone numbers"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Add a number</Button> : undefined}
          >
            Add the DID your PBX will send to the platform, then point it at an agent.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Number</th><th>Trunk</th><th>Answered by</th>
                <th>Routing</th><th>Hours</th><th>PBX</th>
                <th>Status</th><th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div className="mono" style={{ fontWeight: 500 }}>{row.number}</div>
                    {row.label && <div className="subtle small">{row.label}</div>}
                  </td>
                  <td className="small">
                    {row.sip_trunk_name ?? <span className="subtle">unassigned</span>}
                  </td>
                  <td className="small">
                    {row.agent_name ?? (
                      <Badge tone="warn">no agent</Badge>
                    )}
                  </td>
                  <td className="small">
                    {row.routing_rule_name ?? <span className="subtle">any matching rule</span>}
                  </td>
                  <td className="small">
                    {row.business_hours_name ?? <span className="subtle">—</span>}
                  </td>
                  <td className="small">{row.pbx_name ?? <span className="subtle">—</span>}</td>
                  <td>
                    {row.status === "ACTIVE"
                      ? <Badge tone="ok" dot>active</Badge>
                      : <Badge tone="neutral">{row.status.toLowerCase()}</Badge>}
                  </td>
                  <td>
                    <div className="cell-actions">
                      {/* Only offered where it can work: a live number with an
                          agent behind it. Offering it otherwise produces a call
                          that connects to silence — the very failure this
                          button exists to help diagnose. */}
                      {row.status === "ACTIVE" && row.inbound_agent_id && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            setCalling({ did: row.number, agent: row.agent_name ?? "the agent" })
                          }
                        >
                          <PhoneIcon />
                          Call Test
                        </Button>
                      )}
                      {canWrite && (
                        <>
                          <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>
                          <Button size="sm" onClick={async () => {
                            try {
                              await (row.status === "ACTIVE"
                                ? api.phoneNumbers.disable(row.id)
                                : api.phoneNumbers.enable(row.id));
                              toasts.ok(`${row.number} ${row.status === "ACTIVE" ? "disabled" : "enabled"}`);
                              await load();
                            } catch (err) {
                              toasts.err(err instanceof Error ? err.message : "failed");
                            }
                          }}>
                            {row.status === "ACTIVE" ? "Disable" : "Enable"}
                          </Button>
                          <Button size="sm" variant="danger" onClick={() => setDeleting(row)}>Remove</Button>
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

      {calling && (
        <BrowserCall
          did={calling.did}
          agentName={calling.agent}
          onClose={() => setCalling(null)}
        />
      )}

      {(creating || editing) && (
        <NumberForm
          number={editing} pbxs={pbxs} trunks={trunks} agents={agents}
          rules={rules} hours={hours} dispatchRules={dispatchRules}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Remove ${deleting.number}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.phoneNumbers.remove(deleting.id);
                  toasts.ok(`${deleting.number} removed`);
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
            Calls to this number will no longer reach the platform. Its trunk
            will stop accepting it on the next sync.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function NumberForm({
  number, pbxs, trunks, agents, rules, hours, dispatchRules, onClose, onSaved,
}: {
  number: PhoneNumber | null;
  pbxs: Pbx[]; trunks: SipTrunk[]; agents: Agent[];
  rules: RoutingRule[]; hours: BusinessHours[];
  dispatchRules: DispatchRule[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<PhoneNumberInput>({
    number: number?.number ?? "",
    label: number?.label ?? "",
    pbx_id: number?.pbx_id ?? null,
    sip_trunk_id: number?.sip_trunk_id ?? null,
    inbound_agent_id: number?.inbound_agent_id ?? null,
    routing_rule_id: number?.routing_rule_id ?? null,
    business_hours_id: number?.business_hours_id ?? null,
  });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const chosenAgent = agents.find((a) => a.id === form.inbound_agent_id);
  const agentUnpublished = chosenAgent && chosenAgent.published_version_number === null;
  const chosenRule = rules.find((r) => r.id === form.routing_rule_id);
  const dispatchForTrunk = dispatchRules.filter((r) => r.sip_trunk_id === form.sip_trunk_id);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload = { ...form, number: form.number.trim(), label: form.label?.trim() || null };
      if (number) {
        const { number: _omit, ...rest } = payload;
        await api.phoneNumbers.update(number.id, rest);
        await onSaved(`${number.number} updated`);
      } else {
        await api.phoneNumbers.create(payload);
        await onSaved(`${payload.number} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={number ? `Edit ${number.number}` : "Add a phone number"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {number ? "Save changes" : "Add"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field
          label="Number" required
          hint={number ? "The number itself cannot be changed — remove and re-add instead."
                       : "E.164 or a PBX extension. Normalised on save."}
        >
          {(id) => <input id={id} required disabled={Boolean(number)} value={form.number}
            onChange={(e) => setForm({ ...form, number: e.target.value })} placeholder="+8801700000000 or 1801" />}
        </Field>

        <Field label="Label" hint="Optional note, e.g. 'Reservations line'.">
          {(id) => <input id={id} value={form.label ?? ""}
            onChange={(e) => setForm({ ...form, label: e.target.value })} />}
        </Field>

        <Field label="SIP trunk" hint="How calls to this number reach the platform.">
          {(id) => (
            <select id={id} value={form.sip_trunk_id ?? ""}
              onChange={(e) => setForm({ ...form, sip_trunk_id: e.target.value || null })}>
              <option value="">Unassigned</option>
              {trunks.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          )}
        </Field>

        {form.sip_trunk_id && (
          <Notice tone="info">
            {dispatchForTrunk.length === 0
              ? "This trunk has no dispatch rule — LiveKit will not invite a worker. Create one under Dispatch Rules."
              : `LiveKit will dispatch workers named ${dispatchForTrunk.map((r) => r.agent_dispatch_name).join(", ")}. The inbound agent below is who speaks.`}
          </Notice>
        )}

        <Field label="Answered by" hint="The AI agent that takes calls to this number.">
          {(id) => (
            <select id={id} value={form.inbound_agent_id ?? ""}
              onChange={(e) => setForm({ ...form, inbound_agent_id: e.target.value || null })}>
              <option value="">No agent</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}{a.published_version_number === null ? " (unpublished)" : ` (v${a.published_version_number})`}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field label="PBX" hint="Optional — records which PBX owns this number.">
          {(id) => (
            <select id={id} value={form.pbx_id ?? ""}
              onChange={(e) => setForm({ ...form, pbx_id: e.target.value || null })}>
              <option value="">Not linked</option>
              {pbxs.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
        </Field>

        <Field
          label="Routing rule"
          hint="Optional. Pin this DID to one rule. Without a pin, every matching rule is evaluated in priority order."
        >
          {(id) => (
            <select id={id} value={form.routing_rule_id ?? ""}
              onChange={(e) => setForm({ ...form, routing_rule_id: e.target.value || null })}>
              <option value="">Evaluate all matching rules</option>
              {rules.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}{r.status !== "ACTIVE" ? ` (${r.status.toLowerCase()})` : ""}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field
          label="Business hours"
          hint="Optional schedule attached to this number. Hours on a pinned routing rule take precedence at call setup."
        >
          {(id) => (
            <select id={id} value={form.business_hours_id ?? ""}
              onChange={(e) => setForm({ ...form, business_hours_id: e.target.value || null })}>
              <option value="">None</option>
              {hours.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}{h.open_now === true ? " (open now)" : h.open_now === false ? " (closed now)" : ""}
                </option>
              ))}
            </select>
          )}
        </Field>

        {chosenRule && (
          <Notice tone="info">
            Fallback for <strong>{chosenRule.name}</strong>:{" "}
            {chosenRule.fallback_action
              ? FALLBACK_ACTION_LABELS[chosenRule.fallback_action]
              : "none — a failed handoff drops the caller."}
            {chosenRule.closed_action
              ? ` Outside hours: ${FALLBACK_ACTION_LABELS[chosenRule.closed_action]}.`
              : ""}
            {" "}Edit the chain on the Routing page.
          </Notice>
        )}

        {agentUnpublished && (
          <Notice tone="warn">
            {chosenAgent?.name} has no published version, so it cannot answer a
            call yet. Publish it before routing traffic here.
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
