"use client";

/**
 * Catalog voices (spec 30).
 *
 * Only TTS providers have voices, so the form offers only those — the API
 * refuses the rest, and a select listing providers that cannot work would
 * invite the error rather than prevent it.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type CatalogVoice, type CatalogVoiceInput, type Provider, type ResourceStatus,
} from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

export default function VoicesPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<CatalogVoice[] | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<CatalogVoice | null>(null);

  const canWrite = principal?.roles.includes("SUPER_ADMIN") ?? false;
  const ttsProviders = providers.filter((p) => p.kind === "TTS");

  const load = useCallback(async () => {
    try {
      const [voicePage, providerPage] = await Promise.all([
        api.platform.voices.list(), api.platform.providers.list(),
      ]);
      setRows(voicePage.items);
      setProviders(providerPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load voices");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Voices</h1>
          <p className="page-subtitle">
            The voices an agent can speak with. Each is a provider's own
            identifier, passed through to the TTS call unchanged.
          </p>
        </div>
        {canWrite && ttsProviders.length > 0 && (
          <Button variant="primary" onClick={() => setCreating(true)}>Add a voice</Button>
        )}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {providers.length > 0 && ttsProviders.length === 0 && (
        <Notice tone="warn">
          There is no TTS provider in the catalog, so no voice can be added. Add
          one under Providers first.
        </Notice>
      )}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading voices…" />
        ) : rows.length === 0 ? (
          <EmptyState title="No voices">
            An agent needs a voice to speak. Add the voice identifiers your TTS
            provider offers.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Voice</th><th>Provider</th><th>Language</th>
                <th>Accent</th><th>Default</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{row.name}</div>
                    <div className="subtle small mono">{row.voice_id}</div>
                    {row.description && <div className="subtle small">{row.description}</div>}
                  </td>
                  <td className="small mono">{row.provider_slug ?? "—"}</td>
                  <td className="small mono">
                    {row.language ?? <span className="subtle">any</span>}
                  </td>
                  <td className="small">{row.accent ?? <span className="subtle">—</span>}</td>
                  <td>{row.is_default && <Badge tone="ok">default</Badge>}</td>
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
        <VoiceForm
          voice={editing} providers={ttsProviders}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function VoiceForm({
  voice, providers, onClose, onSaved,
}: {
  voice: CatalogVoice | null;
  providers: Provider[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<CatalogVoiceInput>({
    provider_id: voice?.provider_id ?? providers[0]?.id ?? "",
    voice_id: voice?.voice_id ?? "",
    name: voice?.name ?? "",
    language: voice?.language ?? "",
    accent: voice?.accent ?? "",
    description: voice?.description ?? "",
    is_default: voice?.is_default ?? false,
  });
  const [status, setStatus] = useState<ResourceStatus>(voice?.status ?? "ACTIVE");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const common = {
        name: form.name.trim() || form.voice_id.trim(),
        language: form.language?.trim() || null,
        accent: form.accent?.trim() || null,
        description: form.description?.trim() || null,
        is_default: form.is_default,
      };
      if (voice) {
        await api.platform.voices.update(voice.id, { ...common, status });
        await onSaved(`${common.name} updated`);
      } else {
        await api.platform.voices.create({
          ...common,
          provider_id: form.provider_id,
          voice_id: form.voice_id.trim(),
        });
        await onSaved(`${common.name} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={voice ? `Edit ${voice.name}` : "Add a voice"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {voice ? "Save changes" : "Add voice"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Provider" required hint="Only TTS providers have voices.">
          {(id) => (
            <select id={id} required disabled={Boolean(voice)} value={form.provider_id}
              onChange={(e) => setForm({ ...form, provider_id: e.target.value })}>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>{p.display_name}</option>
              ))}
            </select>
          )}
        </Field>

        <Field
          label="Voice ID" required
          hint={voice
            ? "Fixed after creation — it is the provider's own identifier."
            : "The provider's own identifier, passed to the TTS call verbatim."}
        >
          {(id) => <input id={id} required className="mono" disabled={Boolean(voice)}
            value={form.voice_id} onChange={(e) => setForm({ ...form, voice_id: e.target.value })}
            placeholder="alloy" />}
        </Field>

        <Field label="Name" hint="What the agent builder shows. Defaults to the voice ID.">
          {(id) => <input id={id} value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />}
        </Field>

        <div className="form-grid">
          <Field label="Language">
            {(id) => <input id={id} className="mono" value={form.language ?? ""}
              onChange={(e) => setForm({ ...form, language: e.target.value })} placeholder="en" />}
          </Field>
          <Field label="Accent">
            {(id) => <input id={id} value={form.accent ?? ""}
              onChange={(e) => setForm({ ...form, accent: e.target.value })} placeholder="American" />}
          </Field>
        </div>

        <Field label="Description" hint="How it sounds, so whoever picks it does not have to audition every voice.">
          {(id) => <textarea id={id} rows={2} value={form.description ?? ""}
            onChange={(e) => setForm({ ...form, description: e.target.value })} />}
        </Field>

        {voice && (
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

        <Field label="Default">
          {(id) => (
            <label className="row" style={{ gap: 8 }}>
              <input id={id} type="checkbox" checked={form.is_default ?? false}
                onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
              <span className="small">The default voice for this provider</span>
            </label>
          )}
        </Field>
      </form>
    </Dialog>
  );
}
