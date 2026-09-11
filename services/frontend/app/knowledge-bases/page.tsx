"use client";

/**
 * Knowledge bases (spec 34).
 *
 * A base can be created and configured today, and an agent can be pointed at
 * one. Document ingestion — upload, chunking, embedding — is Phase 6, so this
 * screen says that plainly instead of offering an upload button that would
 * leave a document stuck at PENDING.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, RelativeTime,
  ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type DocumentStatus, type KnowledgeBase, type KnowledgeBaseInput,
  type KnowledgeDocument,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const DOC_TONE: Record<DocumentStatus, "ok" | "warn" | "err" | "neutral"> = {
  INDEXED: "ok",
  PROCESSING: "warn",
  PENDING: "neutral",
  FAILED: "err",
};

export default function KnowledgeBasesPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<KnowledgeBase[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<KnowledgeBase | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<KnowledgeBase | null>(null);
  const [viewing, setViewing] = useState<KnowledgeBase | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[] | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const page = await api.knowledgeBases.list();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load knowledge bases");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  useEffect(() => {
    if (!viewing) {
      setDocuments(null);
      return;
    }
    let cancelled = false;
    void api.knowledgeBases
      .documents(viewing.id)
      .then((page) => { if (!cancelled) setDocuments(page.items); })
      .catch(() => { if (!cancelled) setDocuments([]); });
    return () => { cancelled = true; };
  }, [viewing]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Knowledge Bases</h1>
          <p className="page-subtitle">
            What an agent can look up when answering. A base holds documents,
            chunked and embedded, that the agent retrieves from mid-sentence.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a base</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <Notice tone="info">
        Bases can be created and assigned to an agent now. Uploading and
        indexing documents is Phase 6 — until it lands, a base has no chunks,
        so an agent pointed at one retrieves nothing rather than failing. The
        document list below is live, so anything ingested out-of-band appears
        here.
      </Notice>

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading knowledge bases…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No knowledge bases"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Add a base</Button> : undefined}
          >
            Create one for each body of material an agent should be able to
            quote — a rate card, a policy document, a menu.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Base</th><th>Embedding</th><th>Documents</th>
                <th>Chunks</th><th>Top K</th><th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{row.name}</div>
                    {row.description && <div className="subtle small">{row.description}</div>}
                  </td>
                  <td className="small mono">
                    {row.embedding_model_slug ?? <span className="subtle">not set</span>}
                    <div className="subtle small">{row.embedding_dimensions} dims</div>
                  </td>
                  <td className="small">
                    {row.document_count === 0 ? (
                      <span className="subtle">none</span>
                    ) : (
                      <>
                        {row.indexed_count} of {row.document_count} indexed
                      </>
                    )}
                  </td>
                  <td className="small mono">{row.chunk_count}</td>
                  <td className="small mono">{row.top_k}</td>
                  <td>
                    {row.status === "ACTIVE"
                      ? <Badge tone="ok" dot>active</Badge>
                      : <Badge tone="neutral">{row.status.toLowerCase()}</Badge>}
                  </td>
                  <td>
                    <div className="cell-actions">
                      <Button size="sm" onClick={() => setViewing(row)}>Documents</Button>
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

      {viewing && (
        <Dialog
          title={`${viewing.name} — documents`}
          onClose={() => setViewing(null)}
          footer={<Button onClick={() => setViewing(null)}>Close</Button>}
        >
          {documents === null ? (
            <Loading label="Loading documents…" />
          ) : documents.length === 0 ? (
            <p className="muted" style={{ marginTop: 0 }}>
              No documents. Ingestion is Phase 6, so there is nothing to index
              from here yet — the base is configured and an agent can already
              point at it.
            </p>
          ) : (
            <table>
              <thead>
                <tr><th>Title</th><th>Type</th><th>Status</th><th>Chunks</th><th>Indexed</th></tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr key={doc.id}>
                    <td>
                      {doc.title}
                      {doc.ingest_error && (
                        <div className="small" style={{ color: "var(--err)" }}>{doc.ingest_error}</div>
                      )}
                    </td>
                    <td className="small mono">{doc.source_type}</td>
                    <td><Badge tone={DOC_TONE[doc.status]}>{doc.status.toLowerCase()}</Badge></td>
                    <td className="small mono">{doc.chunk_count}</td>
                    <td className="small"><RelativeTime iso={doc.indexed_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Dialog>
      )}

      {(creating || editing) && (
        <BaseForm
          base={editing}
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
                  await api.knowledgeBases.remove(deleting.id);
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
            Its documents and chunks go with it. An agent version still
            referencing this base keeps the reference, and the API refuses the
            removal while one does.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function BaseForm({
  base, onClose, onSaved,
}: {
  base: KnowledgeBase | null;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<KnowledgeBaseInput>({
    name: base?.name ?? "",
    description: base?.description ?? "",
    embedding_model_slug: base?.embedding_model_slug ?? "text-embedding-3-small",
    embedding_dimensions: base?.embedding_dimensions ?? 1536,
    top_k: base?.top_k ?? 5,
  });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload = {
        ...form,
        name: form.name.trim(),
        description: form.description?.trim() || null,
      };
      if (base) {
        // The embedding model and dimensions are fixed after creation: every
        // stored vector has that width, so changing it would invalidate the
        // index rather than migrate it.
        await api.knowledgeBases.update(base.id, {
          name: payload.name,
          description: payload.description,
          top_k: payload.top_k,
        });
        await onSaved(`${payload.name} updated`);
      } else {
        await api.knowledgeBases.create(payload);
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
      title={base ? `Edit ${base.name}` : "Add a knowledge base"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {base ? "Save changes" : "Add base"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required>
          {(id) => <input id={id} required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Room rates and policies" />}
        </Field>

        <Field label="Description">
          {(id) => <textarea id={id} rows={2} value={form.description ?? ""}
            onChange={(e) => setForm({ ...form, description: e.target.value })} />}
        </Field>

        <Field
          label="Retrieved chunks (top K)"
          hint="How many chunks are put in front of the model per question. More context costs latency on every turn."
        >
          {(id) => <input id={id} type="number" min={1} max={20} value={form.top_k ?? 5}
            onChange={(e) => setForm({ ...form, top_k: Number(e.target.value) })} />}
        </Field>

        {base ? (
          <Notice tone="info">
            The embedding model ({base.embedding_model_slug ?? "unset"},{" "}
            {base.embedding_dimensions} dimensions) is fixed after creation:
            every stored vector has that width, so changing it would invalidate
            the index rather than migrate it.
          </Notice>
        ) : (
          <div className="form-grid">
            <Field label="Embedding model" hint="The model slug used to embed chunks.">
              {(id) => <input id={id} className="mono" value={form.embedding_model_slug ?? ""}
                onChange={(e) => setForm({ ...form, embedding_model_slug: e.target.value })} />}
            </Field>
            <Field label="Dimensions" hint="Must match the model. 1536 for text-embedding-3-small.">
              {(id) => <input id={id} type="number" min={64} max={4096}
                value={form.embedding_dimensions ?? 1536}
                onChange={(e) => setForm({ ...form, embedding_dimensions: Number(e.target.value) })} />}
            </Field>
          </div>
        )}
      </form>
    </Dialog>
  );
}
