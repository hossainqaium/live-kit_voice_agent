"use client";

/**
 * Tools the agent can call mid-conversation (spec 31, 32, 33).
 *
 * The request schema becomes a function definition the model sees, so an
 * invalid one is shown as a first-class problem rather than a detail: a tool
 * whose schema a provider rejects is a tool the agent silently never calls.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, TOOL_AUTH_LABELS,
  type HttpMethod, type Tool, type ToolAuthType, type ToolInput,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const METHODS: HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const AUTH_TYPES: ToolAuthType[] = [
  "NONE", "API_KEY_HEADER", "BEARER_TOKEN", "BASIC", "OAUTH2_CLIENT_CREDENTIALS",
];

const EXAMPLE_SCHEMA = `{
  "type": "object",
  "properties": {
    "guest_id": { "type": "string", "description": "The guest's reference" }
  },
  "required": ["guest_id"]
}`;

export default function ToolsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<Tool[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Tool | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Tool | null>(null);
  const [inspecting, setInspecting] = useState<Tool | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const page = await api.tools.list();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load tools");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const invalid = (rows ?? []).filter((row) => !row.schema_valid);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Tools</h1>
          <p className="page-subtitle">
            HTTP calls the agent can make during a conversation — a booking
            lookup, an availability check. The request schema is what the model
            sees, so it decides whether the agent can call the tool at all.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a tool</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {invalid.length > 0 && (
        <Notice tone="err">
          {invalid.length === 1
            ? `${invalid[0].name} has an invalid request schema`
            : `${invalid.length} tools have an invalid request schema`}
          , so the provider will refuse the function definition and the agent
          will never call it. Fix the schema before granting it to an agent.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading tools…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No tools"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Add a tool</Button> : undefined}
          >
            An agent without tools can only talk. Add one to let it look
            something up or write something back while the caller waits.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Tool</th><th>Request</th><th>Auth</th><th>Inputs</th>
                <th>Schema</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div className="mono" style={{ fontWeight: 500 }}>{row.name}</div>
                    <div className="subtle small" style={{ maxWidth: "44ch" }}>{row.description}</div>
                  </td>
                  <td className="small">
                    <span className="mono">{row.http_method}</span>{" "}
                    <span className="subtle mono" style={{ overflowWrap: "anywhere" }}>
                      {row.url_template}
                    </span>
                  </td>
                  <td className="small">
                    {TOOL_AUTH_LABELS[row.auth_type]}
                    {row.auth_type !== "NONE" && !row.has_secret && (
                      <div><Badge tone="warn">no secret set</Badge></div>
                    )}
                  </td>
                  <td className="small mono">
                    {row.variables.length
                      ? row.variables.join(", ")
                      : <span className="subtle">none</span>}
                  </td>
                  <td>
                    {row.schema_valid
                      ? <Badge tone="ok">valid</Badge>
                      : <Badge tone="err" title={row.schema_validation_error ?? ""}>invalid</Badge>}
                  </td>
                  <td>
                    {row.status === "ACTIVE"
                      ? <Badge tone="ok" dot>active</Badge>
                      : <Badge tone="neutral">{row.status.toLowerCase()}</Badge>}
                  </td>
                  <td>
                    <div className="cell-actions">
                      <Button size="sm" onClick={() => setInspecting(row)}>Schema</Button>
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

      {inspecting && (
        <Dialog
          title={`${inspecting.name} — schema`}
          onClose={() => setInspecting(null)}
          footer={<Button onClick={() => setInspecting(null)}>Close</Button>}
        >
          {!inspecting.schema_valid && inspecting.schema_validation_error && (
            <Notice tone="err">{inspecting.schema_validation_error}</Notice>
          )}
          <div className="form-section-label">Request</div>
          <pre className="code-block">
            {JSON.stringify(inspecting.request_schema, null, 2)}
          </pre>
          {Object.keys(inspecting.response_schema).length > 0 && (
            <>
              <div className="form-section-label">Response</div>
              <pre className="code-block">
                {JSON.stringify(inspecting.response_schema, null, 2)}
              </pre>
            </>
          )}
          {Object.keys(inspecting.headers).length > 0 && (
            <>
              <div className="form-section-label">Headers</div>
              <pre className="code-block">
                {JSON.stringify(inspecting.headers, null, 2)}
              </pre>
            </>
          )}
        </Dialog>
      )}

      {(creating || editing) && (
        <ToolForm
          tool={editing}
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
                  await api.tools.remove(deleting.id);
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
            The API refuses while an agent still has this tool granted — the
            grant has to be removed from the agent first, so a published
            version cannot lose a tool it was validated with.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function ToolForm({
  tool, onClose, onSaved,
}: {
  tool: Tool | null;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<ToolInput>({
    name: tool?.name ?? "",
    description: tool?.description ?? "",
    http_method: tool?.http_method ?? "GET",
    url_template: tool?.url_template ?? "",
    auth_type: tool?.auth_type ?? "NONE",
    auth_header_name: tool?.auth_header_name ?? null,
    timeout_seconds: tool?.timeout_seconds ?? 10,
    max_retries: tool?.max_retries ?? 1,
  });
  // JSON is edited as text so an in-progress edit is not destroyed by a parse
  // failure on every keystroke.
  const [schemaText, setSchemaText] = useState(
    JSON.stringify(tool?.request_schema ?? {}, null, 2),
  );
  const [headersText, setHeadersText] = useState(
    JSON.stringify(tool?.headers ?? {}, null, 2),
  );
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const needsHeaderName = form.auth_type === "API_KEY_HEADER";
  const needsSecret = form.auth_type !== "NONE";

  function parseOr(text: string, label: string): Record<string, unknown> {
    if (!text.trim()) return {};
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("must be a JSON object");
      }
      return parsed as Record<string, unknown>;
    } catch (err) {
      throw new Error(`${label}: ${err instanceof Error ? err.message : "invalid JSON"}`);
    }
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload: ToolInput = {
        ...form,
        name: form.name.trim(),
        description: form.description.trim(),
        url_template: form.url_template.trim(),
        request_schema: parseOr(schemaText, "Request schema"),
        headers: parseOr(headersText, "Headers") as Record<string, string>,
      };
      // Omitted rather than sent empty: an empty string would overwrite a
      // stored secret with nothing.
      if (secret.trim()) payload.auth_secret = secret.trim();

      if (tool) {
        await api.tools.update(tool.id, payload);
        await onSaved(`${payload.name} updated`);
      } else {
        await api.tools.create(payload);
        await onSaved(`${payload.name} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError || err instanceof Error ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={tool ? `Edit ${tool.name}` : "Add a tool"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {tool ? "Save changes" : "Add tool"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field
          label="Name" required
          hint="Becomes the function name the model calls, so letters, digits and underscores only."
        >
          {(id) => <input id={id} required className="mono" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="look_up_reservation" />}
        </Field>

        <Field
          label="Description" required
          hint="The model reads this to decide when to call the tool. Write it for the model, not for a person."
        >
          {(id) => <textarea id={id} required rows={2} value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Look up a guest's reservation by their reference number." />}
        </Field>

        <div className="form-grid">
          <Field label="Method" required>
            {(id) => (
              <select id={id} value={form.http_method}
                onChange={(e) => setForm({ ...form, http_method: e.target.value as HttpMethod })}>
                {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            )}
          </Field>
          <Field label="Timeout" hint="Seconds. The caller is waiting.">
            {(id) => <input id={id} type="number" min={1} max={60}
              value={form.timeout_seconds ?? 10}
              onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })} />}
          </Field>
        </div>

        <Field
          label="URL" required
          hint="Use {{name}} for a value the model supplies. Every placeholder must be declared in the request schema below."
        >
          {(id) => <input id={id} required className="mono" value={form.url_template}
            onChange={(e) => setForm({ ...form, url_template: e.target.value })}
            placeholder="https://api.example.com/reservations/{{guest_id}}" />}
        </Field>

        <Field
          label="Request schema"
          hint="JSON Schema for the arguments. This is the function definition the provider receives."
        >
          {(id) => <textarea id={id} rows={8} className="mono" value={schemaText}
            onChange={(e) => setSchemaText(e.target.value)} placeholder={EXAMPLE_SCHEMA} />}
        </Field>

        <Field label="Headers" hint="Static headers as a JSON object. Values may use {{placeholders}} too.">
          {(id) => <textarea id={id} rows={3} className="mono" value={headersText}
            onChange={(e) => setHeadersText(e.target.value)} placeholder='{"Accept": "application/json"}' />}
        </Field>

        <div className="form-section-label">Authentication</div>

        <Field label="Type">
          {(id) => (
            <select id={id} value={form.auth_type}
              onChange={(e) => setForm({ ...form, auth_type: e.target.value as ToolAuthType })}>
              {AUTH_TYPES.map((a) => (
                <option key={a} value={a}>{TOOL_AUTH_LABELS[a]}</option>
              ))}
            </select>
          )}
        </Field>

        {needsHeaderName && (
          <Field label="Header name" required>
            {(id) => <input id={id} required className="mono" value={form.auth_header_name ?? ""}
              onChange={(e) => setForm({ ...form, auth_header_name: e.target.value })}
              placeholder="X-API-Key" />}
          </Field>
        )}

        {needsSecret && (
          <Field
            label={tool?.has_secret ? "Replace the secret" : "Secret"}
            hint={
              tool?.has_secret
                ? "A secret is stored. Leave this empty to keep it; anything typed here replaces it."
                : "Stored encrypted and never returned by the API."
            }
          >
            {(id) => <input id={id} type="password" autoComplete="off" className="mono"
              value={secret} onChange={(e) => setSecret(e.target.value)} />}
          </Field>
        )}
      </form>
    </Dialog>
  );
}
