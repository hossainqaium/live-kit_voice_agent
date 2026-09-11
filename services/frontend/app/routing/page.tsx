"use client";

/**
 * Routing rules (spec 20) and their fallback chain (spec 38).
 *
 * Rules are evaluated in priority order and the first match wins, so the list
 * is shown in that order with the priority visible — a rule's position is the
 * behaviour, not decoration.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, FALLBACK_ACTION_LABELS,
  type Agent, type BusinessHours, type FallbackAction, type RoutingRule,
  type RoutingConditions, type RoutingRuleInput, type TransferDestination,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const FALLBACK_ACTIONS: FallbackAction[] = [
  "SECONDARY_AGENT", "PBX_QUEUE", "VOICEMAIL", "HANGUP",
];

/** The conditions a rule actually set, for the table. */
function conditionSummary(rule: RoutingRule): string {
  const parts = Object.entries(rule.conditions)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key.replace(/_/g, " ")} = ${String(value)}`);
  return parts.length ? parts.join(", ") : "any call";
}

export default function RoutingPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<RoutingRule[] | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [hours, setHours] = useState<BusinessHours[]>([]);
  const [destinations, setDestinations] = useState<TransferDestination[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<RoutingRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<RoutingRule | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const [rules, agentPage, hoursPage, destPage] = await Promise.all([
        api.routingRules.list(),
        api.agents.list(200),
        api.businessHours.list(200),
        api.transferDestinations.list(200),
      ]);
      setRows(rules.items);
      setAgents(agentPage.items);
      setHours(hoursPage.items);
      setDestinations(destPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load routing rules");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Routing</h1>
          <p className="page-subtitle">
            Which agent answers which call. Rules are tried in priority order
            and the first match wins, so the order below is the behaviour.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a rule</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading rules…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No routing rules"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Add a rule</Button> : undefined}
          >
            Without a rule, a call is answered by the agent assigned directly to
            the number it arrived on. Add a rule when that is not enough — to
            route by caller, trunk, or opening hours.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th style={{ width: 70 }}>Priority</th>
                <th>Rule</th><th>Matches</th><th>Agent</th><th>Hours</th>
                <th>If unavailable</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.priority}</td>
                  <td>
                    <div style={{ fontWeight: 500 }}>{row.name}</div>
                    {row.description && <div className="subtle small">{row.description}</div>}
                  </td>
                  <td className="small">{conditionSummary(row)}</td>
                  <td className="small">
                    {row.agent_name ?? <Badge tone="warn">none</Badge>}
                  </td>
                  <td className="small">
                    {row.business_hours_name ?? <span className="subtle">always open</span>}
                  </td>
                  <td className="small">
                    {row.fallback_action
                      ? FALLBACK_ACTION_LABELS[row.fallback_action]
                      : <span className="subtle">nothing</span>}
                  </td>
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
              ))}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <RuleForm
          rule={editing} agents={agents} hours={hours} destinations={destinations}
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
                  await api.routingRules.remove(deleting.id);
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
            Calls this rule was catching will fall through to the next matching
            rule, or to the agent assigned to the number.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function RuleForm({
  rule, agents, hours, destinations, onClose, onSaved,
}: {
  rule: RoutingRule | null;
  agents: Agent[]; hours: BusinessHours[]; destinations: TransferDestination[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<RoutingRuleInput>({
    name: rule?.name ?? "",
    description: rule?.description ?? "",
    priority: rule?.priority ?? 100,
    conditions: rule?.conditions ?? {},
    agent_id: rule?.agent_id ?? null,
    business_hours_id: rule?.business_hours_id ?? null,
    fallback_action: rule?.fallback_action ?? null,
    fallback_agent_id: rule?.fallback_agent_id ?? null,
    fallback_transfer_destination_id: rule?.fallback_transfer_destination_id ?? null,
    closed_action: rule?.closed_action ?? null,
    closed_transfer_destination_id: rule?.closed_transfer_destination_id ?? null,
  });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // The API refuses a fallback that names no target, so the form asks for the
  // right one rather than letting the save fail.
  const needsFallbackAgent = form.fallback_action === "SECONDARY_AGENT";
  const needsFallbackDestination =
    form.fallback_action === "PBX_QUEUE" || form.fallback_action === "VOICEMAIL";
  const needsClosedDestination =
    form.closed_action === "PBX_QUEUE" || form.closed_action === "VOICEMAIL";

  function setCondition(key: keyof RoutingConditions, value: string) {
    setForm({
      ...form,
      conditions: { ...(form.conditions ?? {}), [key]: value.trim() || null },
    });
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload: RoutingRuleInput = {
        ...form,
        name: form.name.trim(),
        description: form.description?.trim() || null,
      };
      if (rule) {
        await api.routingRules.update(rule.id, payload);
        await onSaved(`${payload.name} updated`);
      } else {
        await api.routingRules.create(payload);
        await onSaved(`${payload.name} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  const conditions = form.conditions ?? {};

  return (
    <Dialog
      title={rule ? `Edit ${rule.name}` : "Add a routing rule"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {rule ? "Save changes" : "Add rule"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required>
          {(id) => <input id={id} required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Reservations, out of hours" />}
        </Field>

        <Field label="Description">
          {(id) => <input id={id} value={form.description ?? ""}
            onChange={(e) => setForm({ ...form, description: e.target.value })} />}
        </Field>

        <Field label="Priority" hint="Lower runs first. Leave gaps so a rule can be inserted between two others later.">
          {(id) => <input id={id} type="number" min={0} value={form.priority ?? 100}
            onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })} />}
        </Field>

        <div className="form-section-label">Match when</div>
        <p className="subtle small" style={{ marginTop: 0 }}>
          Leave every field empty to match any call. Fields that are set must
          all match.
        </p>

        <div className="form-grid">
          <Field label="Dialled number (DID)">
            {(id) => <input id={id} value={conditions.did ?? ""}
              onChange={(e) => setCondition("did", e.target.value)} placeholder="1801" />}
          </Field>
          <Field label="Caller number">
            {(id) => <input id={id} value={conditions.caller_number ?? ""}
              onChange={(e) => setCondition("caller_number", e.target.value)} />}
          </Field>
          <Field label="Caller number starts with">
            {(id) => <input id={id} value={conditions.caller_number_prefix ?? ""}
              onChange={(e) => setCondition("caller_number_prefix", e.target.value)}
              placeholder="+8801" />}
          </Field>
          <Field label="Campaign">
            {(id) => <input id={id} value={conditions.campaign ?? ""}
              onChange={(e) => setCondition("campaign", e.target.value)} />}
          </Field>
        </div>

        <div className="form-section-label">Then</div>

        <Field label="Answered by" hint="The agent this rule routes to.">
          {(id) => (
            <select id={id} value={form.agent_id ?? ""}
              onChange={(e) => setForm({ ...form, agent_id: e.target.value || null })}>
              <option value="">No agent</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                  {a.published_version_number === null ? " (unpublished)" : ` (v${a.published_version_number})`}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field label="Business hours" hint="Optional. Outside these hours the closed action below applies.">
          {(id) => (
            <select id={id} value={form.business_hours_id ?? ""}
              onChange={(e) => setForm({ ...form, business_hours_id: e.target.value || null })}>
              <option value="">Always open</option>
              {hours.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
            </select>
          )}
        </Field>

        <div className="form-section-label">If the agent cannot take the call</div>

        <Field label="Fallback" hint="Spec 38. Without one, a failed handoff drops the caller.">
          {(id) => (
            <select id={id} value={form.fallback_action ?? ""}
              onChange={(e) => setForm({
                ...form,
                fallback_action: (e.target.value || null) as FallbackAction | null,
              })}>
              <option value="">Nothing</option>
              {FALLBACK_ACTIONS.map((a) => (
                <option key={a} value={a}>{FALLBACK_ACTION_LABELS[a]}</option>
              ))}
            </select>
          )}
        </Field>

        {needsFallbackAgent && (
          <Field label="Fallback agent" required>
            {(id) => (
              <select id={id} required value={form.fallback_agent_id ?? ""}
                onChange={(e) => setForm({ ...form, fallback_agent_id: e.target.value || null })}>
                <option value="">Choose an agent</option>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            )}
          </Field>
        )}

        {needsFallbackDestination && (
          <Field label="Fallback destination" required
            hint={destinations.length === 0 ? "No transfer targets exist yet — add one under Transfer Targets first." : undefined}>
            {(id) => (
              <select id={id} required value={form.fallback_transfer_destination_id ?? ""}
                onChange={(e) => setForm({
                  ...form,
                  fallback_transfer_destination_id: e.target.value || null,
                })}>
                <option value="">Choose a destination</option>
                {destinations.map((d) => (
                  <option key={d.id} value={d.id}>{d.name} ({d.target})</option>
                ))}
              </select>
            )}
          </Field>
        )}

        <div className="form-section-label">Outside business hours</div>

        <Field label="Closed action">
          {(id) => (
            <select id={id} value={form.closed_action ?? ""}
              onChange={(e) => setForm({
                ...form,
                closed_action: (e.target.value || null) as FallbackAction | null,
              })}>
              <option value="">Same as normal</option>
              {FALLBACK_ACTIONS.map((a) => (
                <option key={a} value={a}>{FALLBACK_ACTION_LABELS[a]}</option>
              ))}
            </select>
          )}
        </Field>

        {needsClosedDestination && (
          <Field label="Closed destination" required>
            {(id) => (
              <select id={id} required value={form.closed_transfer_destination_id ?? ""}
                onChange={(e) => setForm({
                  ...form,
                  closed_transfer_destination_id: e.target.value || null,
                })}>
                <option value="">Choose a destination</option>
                {destinations.map((d) => (
                  <option key={d.id} value={d.id}>{d.name} ({d.target})</option>
                ))}
              </select>
            )}
          </Field>
        )}
      </form>
    </Dialog>
  );
}
