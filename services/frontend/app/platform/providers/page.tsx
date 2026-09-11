"use client";

/**
 * The AI provider catalog (spec 24, 25).
 *
 * ``requires_credential`` is the field that matters most here, and it is shown
 * in the table rather than only in the form: it decides whether the agent
 * validator demands an API key. Wrong in one direction it blocks a valid
 * self-hosted setup; wrong in the other it lets a cloud provider be selected
 * with no key, and the failure appears on a live call.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type Provider, type ProviderInput, type ProviderKind, type ResourceStatus,
} from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

const KINDS: ProviderKind[] = ["STT", "LLM", "TTS"];
const KIND_LABELS: Record<ProviderKind, string> = {
  STT: "Speech to text",
  LLM: "Language model",
  TTS: "Text to speech",
};

export default function ProvidersPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<Provider[] | null>(null);
  const [filter, setFilter] = useState<ProviderKind | "">("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Provider | null>(null);

  const canWrite = principal?.roles.includes("SUPER_ADMIN") ?? false;

  const load = useCallback(async (kind: ProviderKind | "") => {
    try {
      const page = await api.platform.providers.list(kind || undefined);
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load providers");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(filter); }, [principal, filter, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Providers</h1>
          <p className="page-subtitle">
            Which STT, LLM and TTS providers a tenant may choose from. Tenants
            supply their own keys; this catalog decides what is on the menu.
          </p>
        </div>
        <div className="row">
          <select
            value={filter}
            aria-label="Filter by kind"
            onChange={(event) => setFilter(event.target.value as ProviderKind | "")}
          >
            <option value="">All kinds</option>
            {KINDS.map((kind) => <option key={kind} value={kind}>{KIND_LABELS[kind]}</option>)}
          </select>
          {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a provider</Button>}
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading providers…" />
        ) : rows.length === 0 ? (
          <EmptyState title="No providers">
            Nothing in the catalog, so no tenant can configure an agent. Add at
            least one provider per kind.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Provider</th><th>Kind</th><th>Key required</th>
                <th>Streaming</th><th>Models</th><th>Voices</th>
                <th>Tenants using it</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{row.display_name}</div>
                    <div className="subtle small mono">{row.slug}</div>
                    {row.default_base_url && (
                      <div className="subtle small mono" style={{ overflowWrap: "anywhere" }}>
                        {row.default_base_url}
                      </div>
                    )}
                  </td>
                  <td><Badge tone="info">{row.kind}</Badge></td>
                  <td className="small">
                    {row.requires_credential
                      ? "yes"
                      : <span title="Self-hosted, reached over the network without a key">
                          <Badge tone="neutral">self-hosted</Badge>
                        </span>}
                  </td>
                  <td className="small">{row.supports_streaming ? "yes" : "no"}</td>
                  <td className="mono small">{row.model_count}</td>
                  <td className="mono small">{row.kind === "TTS" ? row.voice_count : "—"}</td>
                  <td className="mono small">
                    {row.credential_count > 0
                      ? row.credential_count
                      : <span className="subtle">none</span>}
                  </td>
                  <td>
                    {row.status === "ACTIVE"
                      ? <Badge tone="ok" dot>active</Badge>
                      : <Badge tone="neutral">{row.status.toLowerCase()}</Badge>}
                  </td>
                  <td>
                    <div className="cell-actions">
                      {canWrite && <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <ProviderForm
          provider={editing}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(filter); }}
        />
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function ProviderForm({
  provider, onClose, onSaved,
}: {
  provider: Provider | null;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<ProviderInput>({
    kind: provider?.kind ?? "LLM",
    slug: provider?.slug ?? "",
    display_name: provider?.display_name ?? "",
    supports_streaming: provider?.supports_streaming ?? true,
    default_base_url: provider?.default_base_url ?? "",
    requires_credential: provider?.requires_credential ?? true,
    notes: provider?.notes ?? "",
  });
  const [status, setStatus] = useState<ResourceStatus>(provider?.status ?? "ACTIVE");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const disablingInUse =
    provider !== null && status !== "ACTIVE" && provider.credential_count > 0;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const common = {
        display_name: form.display_name.trim(),
        supports_streaming: form.supports_streaming,
        default_base_url: form.default_base_url?.trim() || null,
        requires_credential: form.requires_credential,
        notes: form.notes?.trim() || null,
      };
      if (provider) {
        await api.platform.providers.update(provider.id, { ...common, status });
        await onSaved(`${common.display_name} updated`);
      } else {
        await api.platform.providers.create({
          ...common,
          kind: form.kind,
          slug: form.slug.trim(),
        });
        await onSaved(`${common.display_name} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={provider ? `Edit ${provider.display_name}` : "Add a provider"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {provider ? "Save changes" : "Add provider"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field
          label="Kind" required
          hint={provider ? "Fixed after creation." : "One vendor can appear once per kind."}
        >
          {(id) => (
            <select id={id} disabled={Boolean(provider)} value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value as ProviderKind })}>
              {KINDS.map((kind) => <option key={kind} value={kind}>{KIND_LABELS[kind]}</option>)}
            </select>
          )}
        </Field>

        <Field
          label="Slug" required
          hint={provider ? "Fixed after creation." : "Lowercase, hyphens. Unique per kind, so 'openai' can be both an STT and a TTS provider."}
        >
          {(id) => <input id={id} required className="mono" disabled={Boolean(provider)}
            value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })}
            placeholder="openai" />}
        </Field>

        <Field label="Display name" required>
          {(id) => <input id={id} required value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            placeholder="OpenAI" />}
        </Field>

        <Field label="Default base URL" hint="Where the adapter points when a tenant sets no override.">
          {(id) => <input id={id} className="mono" value={form.default_base_url ?? ""}
            onChange={(e) => setForm({ ...form, default_base_url: e.target.value })}
            placeholder="https://api.openai.com/v1" />}
        </Field>

        <Field
          label="Credential"
          hint="Turn this off only for a provider reached over the network without a key — a self-hosted Whisper or Kokoro endpoint. The agent validator reads this field."
        >
          {(id) => (
            <label className="row" style={{ gap: 8 }}>
              <input id={id} type="checkbox" checked={form.requires_credential ?? true}
                onChange={(e) => setForm({ ...form, requires_credential: e.target.checked })} />
              <span className="small">A tenant must supply an API key to use this provider</span>
            </label>
          )}
        </Field>

        <Field label="Streaming">
          {(id) => (
            <label className="row" style={{ gap: 8 }}>
              <input id={id} type="checkbox" checked={form.supports_streaming ?? true}
                onChange={(e) => setForm({ ...form, supports_streaming: e.target.checked })} />
              <span className="small">Supports streaming, which voice needs for low latency</span>
            </label>
          )}
        </Field>

        {provider && (
          <Field label="Status">
            {(id) => (
              <select id={id} value={status}
                onChange={(e) => setStatus(e.target.value as ResourceStatus)}>
                <option value="ACTIVE">active</option>
                <option value="DISABLED">disabled</option>
                <option value="ARCHIVED">archived</option>
              </select>
            )}
          </Field>
        )}

        <Field label="Notes">
          {(id) => <textarea id={id} rows={2} value={form.notes ?? ""}
            onChange={(e) => setForm({ ...form, notes: e.target.value })} />}
        </Field>

        {disablingInUse && (
          <Notice tone="err">
            {provider.credential_count} tenant credential
            {provider.credential_count === 1 ? "" : "s"} still reference this
            provider. The API refuses the change rather than letting live calls
            start failing — remove the credentials first.
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
