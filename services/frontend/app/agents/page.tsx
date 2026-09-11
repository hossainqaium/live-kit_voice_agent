"use client";

/**
 * Agent builder (spec 18, 19, 62, 63, 64).
 *
 * The design follows the versioning rules rather than hiding them, because
 * those rules are what a tenant administrator needs to understand to use this
 * safely:
 *
 *  - The form always edits a **draft**. Saving while the latest version is
 *    published creates a new draft, so a live call is never altered.
 *  - **Publish** is a separate, deliberate act, gated by validation.
 *  - Validation issues are shown per field, so a non-developer can act on them.
 *  - The dependency panel (spec 64) shows not just what is chosen but whether
 *    the chosen thing is usable.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice,
  RelativeTime, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type Agent, type AgentVersion, type ValidationReport,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

export default function AgentsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<Agent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [building, setBuilding] = useState<Agent | null>(null);
  const [deleting, setDeleting] = useState<Agent | null>(null);

  const canWrite = can("agents.write");
  const canPublish = can("agents.publish");

  const load = useCallback(async () => {
    try {
      const page = await api.agents.list();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load agents");
      setRows([]);
    }
  }, []);

  useEffect(() => {
    if (principal) void load();
  }, [principal, load]);

  if (authLoading || !principal) {
    return <div className="auth-screen"><Loading /></div>;
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>AI Agents</h1>
          <p className="page-subtitle">
            An agent holds identity; a version holds everything the worker runs.
            Editing a published agent creates a new draft, so a call already in
            progress keeps the configuration it started with.
          </p>
        </div>
        {canWrite && (
          <Button variant="primary" onClick={() => setCreating(true)}>Create an agent</Button>
        )}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading agents…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No agents yet"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Create an agent</Button> : undefined}
          >
            An agent is what answers a call. Create one, configure its speech,
            reasoning and voice, then publish it.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Published</th><th>Unpublished work</th>
                <th>Versions</th><th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((agent) => (
                <tr key={agent.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{agent.name}</div>
                    {agent.description && (
                      <div className="subtle small truncate" style={{ maxWidth: 300 }}>
                        {agent.description}
                      </div>
                    )}
                  </td>
                  <td>
                    {agent.published_version_number !== null ? (
                      <Badge tone="ok" dot>live · v{agent.published_version_number}</Badge>
                    ) : (
                      <Badge tone="warn">not published</Badge>
                    )}
                  </td>
                  <td className="small">
                    {agent.latest_draft_version_number !== null ? (
                      <Badge tone="info">draft v{agent.latest_draft_version_number}</Badge>
                    ) : (
                      <span className="subtle">none</span>
                    )}
                  </td>
                  <td className="small">{agent.version_count}</td>
                  <td>
                    <div className="cell-actions">
                      <Button size="sm" onClick={() => setBuilding(agent)}>
                        {canWrite ? "Configure" : "View"}
                      </Button>
                      {canWrite && (
                        <Button size="sm" variant="danger" onClick={() => setDeleting(agent)}>Delete</Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {creating && (
        <CreateAgentDialog
          onClose={() => setCreating(false)}
          onCreated={async (agent) => {
            setCreating(false);
            toasts.ok(`${agent.name} created with draft v1`);
            await load();
            setBuilding(agent);
          }}
        />
      )}

      {building && (
        <AgentBuilder
          agent={building}
          canWrite={canWrite}
          canPublish={canPublish}
          onClose={() => setBuilding(null)}
          onChanged={async (message) => { toasts.ok(message); await load(); }}
          onError={(message) => toasts.err(message)}
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
                  await api.agents.remove(deleting.id);
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
            This removes the agent and all its versions. Refused while a phone
            number still routes to it.
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            Call history survives — past calls keep their record of what this
            agent did.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function CreateAgentDialog({
  onClose, onCreated,
}: { onClose(): void; onCreated(agent: Agent): void | Promise<void> }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const agent = await api.agents.create({
        name: name.trim(),
        description: description.trim() || null,
      });
      await onCreated(agent);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not create the agent");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title="Create an agent"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={submit}>Create</Button>
        </>
      }
    >
      {error && <Notice tone="err">{error}</Notice>}
      <form onSubmit={submit} noValidate>
        <Field label="Name" required hint="Unique within your tenant.">
          {(id) => <input id={id} required value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Reservations Agent" />}
        </Field>
        <Field label="Description" hint="Optional note for your team.">
          {(id) => <textarea id={id} rows={2} value={description}
            onChange={(e) => setDescription(e.target.value)} />}
        </Field>
        <Notice tone="info">
          A draft version is created with it, ready to configure. Nothing goes
          live until you publish.
        </Notice>
      </form>
    </Dialog>
  );
}

function AgentBuilder({
  agent, canWrite, canPublish, onClose, onChanged, onError,
}: {
  agent: Agent;
  canWrite: boolean;
  canPublish: boolean;
  onClose(): void;
  onChanged(message: string): void | Promise<void>;
  onError(message: string): void;
}) {
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [draft, setDraft] = useState<Partial<AgentVersion>>({});
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"config" | "history">("config");

  const load = useCallback(async () => {
    const list = await api.agents.versions(agent.id);
    setVersions(list);
    // Edit the newest draft when there is one, otherwise show what is live.
    const editable = list.find((v) => v.state === "DRAFT") ?? list[0];
    if (editable) {
      setDraft(editable);
      try {
        setReport(await api.agents.validate(agent.id, editable.version_number));
      } catch {
        setReport(null);
      }
    }
  }, [agent.id]);

  useEffect(() => { void load(); }, [load]);

  const editingVersion = versions?.find((v) => v.version_number === draft.version_number);
  const editingPublished = editingVersion?.state === "PUBLISHED";

  async function save() {
    setBusy(true);
    try {
      const saved = await api.agents.saveDraft(agent.id, {
        language: draft.language,
        greeting: draft.greeting,
        system_prompt: draft.system_prompt,
        temperature: draft.temperature,
        interruption_enabled: draft.interruption_enabled,
        silence_timeout_seconds: draft.silence_timeout_seconds,
        max_call_duration_seconds: draft.max_call_duration_seconds,
        recording_enabled: draft.recording_enabled,
        transcription_enabled: draft.transcription_enabled,
        transfer_enabled: draft.transfer_enabled,
        transfer_announcement_text: draft.transfer_announcement_text,
      });
      await onChanged(
        editingPublished
          ? `Saved as a new draft, v${saved.version_number} — v${editingVersion?.version_number} stays live`
          : `Draft v${saved.version_number} saved`,
      );
      await load();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "could not save the draft");
    } finally {
      setBusy(false);
    }
  }

  async function publish() {
    setBusy(true);
    try {
      const published = await api.agents.publish(agent.id);
      await onChanged(`v${published.version_number} published — new calls will use it`);
      await load();
    } catch (err) {
      // The 422 body carries the field-by-field list, which the client
      // flattens into a readable message.
      onError(err instanceof ApiError ? err.message : "could not publish");
    } finally {
      setBusy(false);
    }
  }

  async function rollback(versionNumber: number) {
    setBusy(true);
    try {
      await api.agents.rollback(agent.id, versionNumber);
      await onChanged(`Rolled back to v${versionNumber}`);
      await load();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "could not roll back");
    } finally {
      setBusy(false);
    }
  }

  const set = <K extends keyof AgentVersion>(key: K, value: AgentVersion[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  return (
    <Dialog
      title={`${agent.name}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Close</Button>
          {canWrite && tab === "config" && (
            <Button busy={busy} onClick={save}>
              {editingPublished ? "Save as new draft" : "Save draft"}
            </Button>
          )}
          {canPublish && tab === "config" && (
            <Button
              variant="primary"
              busy={busy}
              disabled={report ? !report.publishable : false}
              onClick={publish}
              title={report && !report.publishable
                ? "Fix the validation issues first"
                : "Publish this draft; new calls will use it"}
            >
              Publish
            </Button>
          )}
        </>
      }
    >
      <div className="row" style={{ marginBottom: 14, gap: 6 }}>
        <Button size="sm" variant={tab === "config" ? "primary" : "ghost"} onClick={() => setTab("config")}>
          Configuration
        </Button>
        <Button size="sm" variant={tab === "history" ? "primary" : "ghost"} onClick={() => setTab("history")}>
          Versions{versions ? ` (${versions.length})` : ""}
        </Button>
      </div>

      {versions === null ? (
        <Loading />
      ) : tab === "history" ? (
        <div className="stack" style={{ gap: 10 }}>
          {versions.map((version) => (
            <div key={version.id} className="row-between" style={{ alignItems: "flex-start" }}>
              <div>
                <div className="row" style={{ gap: 8 }}>
                  <strong style={{ fontWeight: 500 }}>v{version.version_number}</strong>
                  <Badge tone={
                    version.state === "PUBLISHED" ? "ok"
                      : version.state === "DRAFT" ? "info"
                      : "neutral"
                  }>
                    {version.state.toLowerCase()}
                  </Badge>
                </div>
                <div className="subtle small" style={{ marginTop: 2 }}>
                  {version.change_note ?? "no note"}
                  {version.published_at && <> · published <RelativeTime iso={version.published_at} /></>}
                </div>
              </div>
              {canPublish && version.state === "ARCHIVED" && (
                <Button size="sm" busy={busy} onClick={() => void rollback(version.version_number)}>
                  Roll back to this
                </Button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <>
          {editingPublished && (
            <Notice tone="info">
              You are viewing v{editingVersion?.version_number}, which is live.
              Saving creates a new draft rather than changing it, so calls in
              progress are unaffected.
            </Notice>
          )}

          {report && report.issues.length > 0 && (
            <Notice tone="warn">
              <strong>Not publishable yet.</strong>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {report.issues.map((issue) => (
                  <li key={issue.field + issue.message}>{issue.message}</li>
                ))}
              </ul>
            </Notice>
          )}

          {report && report.dependencies.length > 0 && (
            <div className="card" style={{ marginBottom: 14, padding: "10px 13px" }}>
              <div className="stat-label" style={{ marginBottom: 6 }}>Dependencies</div>
              <div className="stack" style={{ gap: 4 }}>
                {report.dependencies.map((dep) => (
                  <div key={dep.field + dep.message} className="row small" style={{ gap: 7 }}>
                    <Badge tone={dep.severity === "ok" ? "ok" : "err"} dot>
                      {dep.severity === "ok" ? "ready" : "problem"}
                    </Badge>
                    <span className={dep.severity === "ok" ? "muted" : undefined}>{dep.message}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <fieldset disabled={!canWrite} style={{ border: 0, padding: 0, margin: 0 }}>
            <Field label="System prompt" required
              hint="The agent's instructions. Without one it has nothing to go on.">
              {(id) => <textarea id={id} rows={4} value={draft.system_prompt ?? ""}
                onChange={(e) => set("system_prompt", e.target.value)}
                placeholder="You are the reservations agent for a hotel. Keep replies brief." />}
            </Field>

            <Field label="Greeting" hint="Spoken as soon as the call connects.">
              {(id) => <input id={id} value={draft.greeting ?? ""}
                onChange={(e) => set("greeting", e.target.value)} />}
            </Field>

            <div className="field-row">
              <Field label="Language">
                {(id) => <input id={id} value={draft.language ?? "en"}
                  onChange={(e) => set("language", e.target.value)} />}
              </Field>
              <Field label="Temperature" hint="0 is deterministic, 2 is loose.">
                {(id) => <input id={id} type="number" step="0.1" min={0} max={2}
                  value={draft.temperature ?? 0.7}
                  onChange={(e) => set("temperature", Number(e.target.value))} />}
              </Field>
            </div>

            <div className="card" style={{ marginBottom: 14, padding: "10px 13px" }}>
              <div className="stat-label" style={{ marginBottom: 6 }}>Speech and reasoning</div>
              <div className="stack small" style={{ gap: 3 }}>
                <div><span className="subtle">STT</span> {draft.stt_label ?? "not selected"}</div>
                <div><span className="subtle">LLM</span> {draft.llm_label ?? "not selected"}</div>
                <div><span className="subtle">TTS</span> {draft.tts_label ?? "not selected"}</div>
                <div><span className="subtle">Voice</span> {draft.voice_label ?? "not selected"}</div>
              </div>
              <p className="subtle small" style={{ margin: "8px 0 0" }}>
                Provider selection is not editable here yet — it is set through
                the CLI or API until the platform provider catalog screen lands.
              </p>
            </div>

            <div className="field-row">
              <Field label="Silence timeout (s)"
                hint="Ends a call after this much silence.">
                {(id) => <input id={id} type="number" min={1} max={600}
                  value={draft.silence_timeout_seconds ?? ""}
                  onChange={(e) => set("silence_timeout_seconds", e.target.value ? Number(e.target.value) : null)} />}
              </Field>
              <Field label="Max duration (s)" hint="Hard limit on call length.">
                {(id) => <input id={id} type="number" min={10} max={86400}
                  value={draft.max_call_duration_seconds ?? ""}
                  onChange={(e) => set("max_call_duration_seconds", e.target.value ? Number(e.target.value) : null)} />}
              </Field>
            </div>

            <Notice tone="warn">
              Silence timeout, maximum duration and recording are stored but{" "}
              <strong>not yet enforced by the worker</strong> (Phase 2b). They
              are shown here because they are part of the configuration, not
              because they currently take effect.
            </Notice>

            <div className="stack" style={{ gap: 8, marginBottom: 14 }}>
              <label className="row small" style={{ gap: 8 }}>
                <input type="checkbox" style={{ width: "auto" }}
                  checked={draft.interruption_enabled ?? true}
                  onChange={(e) => set("interruption_enabled", e.target.checked)} />
                Allow the caller to interrupt the agent (barge-in)
              </label>
              <label className="row small" style={{ gap: 8 }}>
                <input type="checkbox" style={{ width: "auto" }}
                  checked={draft.transcription_enabled ?? true}
                  onChange={(e) => set("transcription_enabled", e.target.checked)} />
                Store a transcript of each call
              </label>
              <label className="row small" style={{ gap: 8 }}>
                <input type="checkbox" style={{ width: "auto" }}
                  checked={draft.recording_enabled ?? false}
                  onChange={(e) => set("recording_enabled", e.target.checked)} />
                Record audio to object storage
              </label>
              <label className="row small" style={{ gap: 8 }}>
                <input type="checkbox" style={{ width: "auto" }}
                  checked={draft.transfer_enabled ?? false}
                  onChange={(e) => set("transfer_enabled", e.target.checked)} />
                Allow transfer to a human agent
              </label>
            </div>

            {draft.transfer_enabled && (
              <Field label="Transfer announcement" required
                hint="What the caller hears while the human agent is reached. Silence here reads as a dropped call.">
                {(id) => <input id={id} value={draft.transfer_announcement_text ?? ""}
                  onChange={(e) => set("transfer_announcement_text", e.target.value)}
                  placeholder="Your call is being transferred to a human agent. Please wait." />}
              </Field>
            )}
          </fieldset>
        </>
      )}
    </Dialog>
  );
}
