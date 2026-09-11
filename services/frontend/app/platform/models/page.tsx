"use client";

/**
 * Catalog models (spec 24).
 *
 * A provider has at most one default model, which the API enforces by clearing
 * the previous default in the same transaction. Two defaults would leave the
 * agent validator breaking a tie arbitrarily, so the console shows the default
 * per provider rather than as a free-standing flag.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type CatalogModel, type CatalogModelInput, type Provider, type ResourceStatus,
} from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

export default function ModelsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<CatalogModel[] | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<CatalogModel | null>(null);
  const [deleting, setDeleting] = useState<CatalogModel | null>(null);

  const canWrite = principal?.roles.includes("SUPER_ADMIN") ?? false;

  const load = useCallback(async () => {
    try {
      const [modelPage, providerPage] = await Promise.all([
        api.platform.models.list(), api.platform.providers.list(),
      ]);
      setRows(modelPage.items);
      setProviders(providerPage.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load models");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Models</h1>
          <p className="page-subtitle">
            The models a tenant can pick per provider, and which one an agent
            gets when it picks nothing.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a model</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading models…" />
        ) : rows.length === 0 ? (
          <EmptyState title="No models">
            A provider with no models cannot be selected by an agent. Add the
            model slugs the provider accepts.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Model</th><th>Provider</th><th>Kind</th>
                <th>Languages</th><th>Default</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div className="mono" style={{ fontWeight: 500 }}>{row.slug}</div>
                    <div className="subtle small">{row.display_name}</div>
                  </td>
                  <td className="small mono">{row.provider_slug ?? "—"}</td>
                  <td>{row.provider_kind && <Badge tone="info">{row.provider_kind}</Badge>}</td>
                  <td className="small mono">
                    {row.languages.length
                      ? row.languages.join(", ")
                      : <span className="subtle">any</span>}
                  </td>
                  <td>{row.is_default && <Badge tone="ok">default</Badge>}</td>
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
        <ModelForm
          model={editing} providers={providers}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Remove ${deleting.slug}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.platform.models.remove(deleting.id);
                  toasts.ok(`${deleting.slug} removed`);
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
            The API refuses while an agent version names this model. Published
            versions are immutable, so deleting a model out from under one
            would leave a version that can never run again — disable it instead
            to stop new agents choosing it.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function ModelForm({
  model, providers, onClose, onSaved,
}: {
  model: CatalogModel | null;
  providers: Provider[];
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<CatalogModelInput>({
    provider_id: model?.provider_id ?? providers[0]?.id ?? "",
    slug: model?.slug ?? "",
    display_name: model?.display_name ?? "",
    languages: model?.languages ?? [],
    is_default: model?.is_default ?? false,
  });
  const [languageText, setLanguageText] = useState((model?.languages ?? []).join(", "));
  const [status, setStatus] = useState<ResourceStatus>(model?.status ?? "ACTIVE");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const provider = providers.find((p) => p.id === form.provider_id);
  const existingDefault = provider && !model?.is_default && form.is_default;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const languages = languageText
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
      if (model) {
        await api.platform.models.update(model.id, {
          display_name: form.display_name.trim(),
          languages,
          status,
          is_default: form.is_default,
        });
        await onSaved(`${model.slug} updated`);
      } else {
        await api.platform.models.create({
          ...form,
          slug: form.slug.trim(),
          display_name: form.display_name.trim() || form.slug.trim(),
          languages,
        });
        await onSaved(`${form.slug.trim()} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={model ? `Edit ${model.slug}` : "Add a model"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {model ? "Save changes" : "Add model"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Provider" required hint={model ? "Fixed after creation." : undefined}>
          {(id) => (
            <select id={id} required disabled={Boolean(model)} value={form.provider_id}
              onChange={(e) => setForm({ ...form, provider_id: e.target.value })}>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name} ({p.kind})
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field
          label="Slug" required
          hint={model
            ? "Fixed after creation — it is sent to the provider verbatim."
            : "Exactly as the provider names it. Sent verbatim."}
        >
          {(id) => <input id={id} required className="mono" disabled={Boolean(model)}
            value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })}
            placeholder="gpt-4o-mini" />}
        </Field>

        <Field label="Display name" hint="What the agent builder shows. Defaults to the slug.">
          {(id) => <input id={id} value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })} />}
        </Field>

        <Field label="Languages" hint="Comma-separated codes. Leave empty if it handles any.">
          {(id) => <input id={id} className="mono" value={languageText}
            onChange={(e) => setLanguageText(e.target.value)} placeholder="en, bn" />}
        </Field>

        {model && (
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
              <span className="small">
                The default model for {provider?.display_name ?? "this provider"}
              </span>
            </label>
          )}
        </Field>

        {existingDefault && (
          <Notice tone="info">
            Saving clears the current default for {provider?.display_name} in
            the same transaction, so the catalog never holds two.
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
