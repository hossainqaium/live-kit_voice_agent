"use client";

/**
 * AI Setup — central hub for AI provider credentials (CR-2, AIProviders.md).
 *
 * Four tabs, one per provider kind: LLM · Embedding · STT · TTS.
 * Each tab shows the credentials already stored for that kind and lets the
 * tenant add, test, rotate, or delete them.
 *
 * Every provider the agent builder offers comes from here. Configure once,
 * select anywhere.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, RelativeTime,
  ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type Catalog, type CatalogProvider, type CredentialVerifyResult,
  type ProviderCredential, type ProviderKind,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

// --------------------------------------------------------------------------- //
// Tab definitions
// --------------------------------------------------------------------------- //

interface TabDef {
  kind: ProviderKind;
  label: string;
  description: string;
  phaseNote?: string;
}

const TABS: TabDef[] = [
  {
    kind: "LLM",
    label: "LLM",
    description: "Language models that decide what the agent says.",
  },
  {
    kind: "EMBEDDING",
    label: "Embedding",
    description: "Embedding models that power knowledge-base retrieval (RAG).",
    phaseNote:
      "Embedding is used by knowledge-base ingestion, which lands in Phase 6. " +
      "You can configure your provider now; it will be used once ingestion is available.",
  },
  {
    kind: "STT",
    label: "STT",
    description: "Speech-to-text providers that transcribe the caller.",
  },
  {
    kind: "TTS",
    label: "TTS",
    description: "Text-to-speech providers that speak the agent's replies.",
  },
];

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export default function AISetupPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [activeTab, setActiveTab] = useState<ProviderKind>("LLM");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [credentials, setCredentials] = useState<ProviderCredential[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const [cat, creds] = await Promise.all([
        api.catalog.get(),
        api.catalog.credentials(),
      ]);
      setCatalog(cat);
      setCredentials(creds);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "could not load provider data");
    }
  }, []);

  useEffect(() => {
    if (principal) void load();
  }, [principal, load]);

  if (authLoading || !principal) {
    return <div className="auth-screen"><Loading /></div>;
  }

  const currentTab = TABS.find((t) => t.kind === activeTab) ?? TABS[0];

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>AI Setup</h1>
          <p className="page-subtitle">
            Configure the AI providers your agents can use. Every agent builder
            dropdown is populated from what you set up here.
          </p>
        </div>
      </div>

      {loadError && <Notice tone="err">{loadError}</Notice>}

      {/* Tab bar */}
      <div className="row" style={{ gap: 4, marginBottom: 20, borderBottom: "1px solid var(--border)", paddingBottom: 0 }}>
        {TABS.map((tab) => {
          const count = credentials?.filter(
            (c) => c.provider_kind === tab.kind,
          ).length ?? 0;
          return (
            <button
              key={tab.kind}
              onClick={() => setActiveTab(tab.kind)}
              style={{
                background: "none",
                border: "none",
                borderBottom: activeTab === tab.kind
                  ? "2px solid var(--accent)"
                  : "2px solid transparent",
                padding: "8px 14px",
                cursor: "pointer",
                fontWeight: activeTab === tab.kind ? 600 : 400,
                color: activeTab === tab.kind ? "var(--text)" : "var(--subtle)",
                fontSize: 14,
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              {tab.label}
              {count > 0 && (
                <span style={{
                  background: "var(--accent)",
                  color: "#fff",
                  borderRadius: 99,
                  fontSize: 11,
                  padding: "1px 6px",
                  fontWeight: 600,
                }}>
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      {catalog === null || credentials === null ? (
        <Loading label="Loading…" />
      ) : (
        <ProviderTab
          tab={currentTab}
          catalog={catalog}
          credentials={credentials}
          canWrite={canWrite}
          onChanged={async (msg) => { toasts.ok(msg); await load(); }}
          onError={(msg) => toasts.err(msg)}
        />
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

// --------------------------------------------------------------------------- //
// Provider tab
// --------------------------------------------------------------------------- //

function ProviderTab({
  tab, catalog, credentials, canWrite, onChanged, onError,
}: {
  tab: TabDef;
  catalog: Catalog;
  credentials: ProviderCredential[];
  canWrite: boolean;
  onChanged(message: string): void | Promise<void>;
  onError(message: string): void;
}) {
  const [adding, setAdding] = useState(false);
  const [testing, setTesting] = useState<ProviderCredential | null>(null);
  const [rotating, setRotating] = useState<ProviderCredential | null>(null);
  const [deleting, setDeleting] = useState<ProviderCredential | null>(null);
  const [testResult, setTestResult] = useState<Record<string, CredentialVerifyResult>>({});
  const [testingId, setTestingId] = useState<string | null>(null);

  const tabCreds = credentials.filter((c) => c.provider_kind === tab.kind);

  async function handleTest(cred: ProviderCredential) {
    setTestingId(cred.id);
    try {
      const result = await api.catalog.verifyCredential(cred.id);
      setTestResult((prev) => ({ ...prev, [cred.id]: result }));
      if (result.ok) {
        await onChanged(`${cred.provider_display_name ?? "Provider"} key verified`);
      }
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "the test could not be run");
    } finally {
      setTestingId(null);
    }
  }

  async function handleDelete(cred: ProviderCredential) {
    try {
      await api.catalog.removeCredential(cred.id);
      await onChanged(`${cred.provider_display_name ?? "Provider"} key removed`);
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "could not remove the key");
    } finally {
      setDeleting(null);
    }
  }

  return (
    <>
      <div style={{ marginBottom: 14 }}>
        <p className="subtle small" style={{ margin: "0 0 12px" }}>{tab.description}</p>

        {tab.phaseNote && (
          <Notice tone="info" style={{ marginBottom: 14 }}>
            {tab.phaseNote}
          </Notice>
        )}

        {canWrite && (
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add {tab.label}
          </Button>
        )}
      </div>

      <div className="table-wrap">
        {tabCreds.length === 0 ? (
          <EmptyState
            title={`No ${tab.label} providers configured`}
            action={canWrite
              ? <Button variant="primary" onClick={() => setAdding(true)}>Add {tab.label}</Button>
              : undefined}
          >
            {tab.kind === "EMBEDDING"
              ? "Add an embedding provider to enable knowledge-base retrieval in your agents."
              : `Add a ${tab.label} provider and its API key. Agents can only use providers configured here.`}
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Label</th>
                <th>Key</th>
                <th>Endpoint</th>
                <th>Verified</th>
                <th>Added</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {tabCreds.map((cred) => {
                const result = testResult[cred.id];
                return (
                  <tr key={cred.id}>
                    <td style={{ fontWeight: 500 }}>
                      {cred.provider_display_name ?? cred.provider_slug ?? "—"}
                    </td>
                    <td>
                      <Badge tone="neutral">{cred.label}</Badge>
                    </td>
                    <td className="mono small">
                      {cred.key_hint ? `···${cred.key_hint}` : <span className="subtle">no key</span>}
                    </td>
                    <td className="small subtle truncate" style={{ maxWidth: 200 }}>
                      {cred.base_url
                        ? (() => { try { return new URL(cred.base_url).hostname; } catch { return cred.base_url; } })()
                        : <span className="subtle">provider default</span>}
                    </td>
                    <td>
                      {cred.last_verified_at ? (
                        <Badge tone="ok" dot title={`Verified ${new Date(cred.last_verified_at).toLocaleString()}`}>
                          verified <RelativeTime iso={cred.last_verified_at} />
                        </Badge>
                      ) : (
                        <Badge tone="warn">unverified</Badge>
                      )}
                    </td>
                    <td className="small subtle">
                      <RelativeTime iso={cred.created_at} />
                    </td>
                    <td>
                      <div className="cell-actions">
                        <Button
                          size="sm"
                          variant="ghost"
                          busy={testingId === cred.id}
                          disabled={!canWrite}
                          onClick={() => void handleTest(cred)}
                        >
                          Test
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!canWrite}
                          onClick={() => setRotating(cred)}
                        >
                          Rotate
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={!canWrite}
                          onClick={() => setDeleting(cred)}
                        >
                          Delete
                        </Button>
                      </div>
                      {result && (
                        <div
                          className="mono"
                          style={{
                            fontSize: 11,
                            color: result.ok ? "var(--ok)" : "var(--err)",
                            marginTop: 4,
                          }}
                        >
                          {result.ok ? "✓" : "✗"} {result.detail}
                          {result.latency_ms !== null ? ` · ${result.latency_ms} ms` : ""}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {adding && (
        <AddProviderDialog
          tab={tab}
          catalog={catalog}
          existingCredentials={credentials}
          onClose={() => setAdding(false)}
          onAdded={async (name) => {
            setAdding(false);
            await onChanged(`${name} added`);
          }}
          onError={onError}
        />
      )}

      {rotating && (
        <RotateKeyDialog
          credential={rotating}
          onClose={() => setRotating(null)}
          onRotated={async () => {
            setRotating(null);
            await onChanged(`Key rotated — verify it to confirm it works`);
          }}
          onError={onError}
        />
      )}

      {deleting && (
        <Dialog
          title={`Remove ${deleting.provider_display_name ?? "this provider"}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={() => void handleDelete(deleting)}>Remove key</Button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            Removing this key stops future calls from using{" "}
            <strong>{deleting.provider_display_name ?? deleting.provider_slug}</strong> as their{" "}
            {tab.label} provider. Any agent with this as its primary will fail to publish until
            a new key is added.
          </p>
          <p className="muted small" style={{ marginBottom: 0 }}>
            This takes effect immediately. You can add the key back at any time.
          </p>
        </Dialog>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- //
// Per-provider metadata for a better Add UX
// --------------------------------------------------------------------------- //

interface ProviderMeta {
  keyPlaceholder: string;
  keyHint: string;
  docsUrl?: string;
  endpointPlaceholder?: string;
}

/** Keyed by provider slug. Cloud providers come from this map; everything else gets defaults. */
const PROVIDER_META: Record<string, ProviderMeta> = {
  // LLM
  openai_hosted:       { keyPlaceholder: "sk-…",          keyHint: "Get from platform.openai.com/api-keys",    docsUrl: "https://platform.openai.com/api-keys" },
  anthropic:           { keyPlaceholder: "sk-ant-…",       keyHint: "Get from console.anthropic.com/account/keys", docsUrl: "https://console.anthropic.com/account/keys" },
  groq:                { keyPlaceholder: "gsk_…",          keyHint: "Get from console.groq.com/keys",           docsUrl: "https://console.groq.com/keys" },
  mistral:             { keyPlaceholder: "…",              keyHint: "Get from console.mistral.ai/api-keys",     docsUrl: "https://console.mistral.ai/api-keys" },
  google_gemini:       { keyPlaceholder: "AIza…",          keyHint: "Get from aistudio.google.com/app/apikey",  docsUrl: "https://aistudio.google.com/app/apikey" },
  together:            { keyPlaceholder: "…",              keyHint: "Get from api.together.xyz/settings/api-keys", docsUrl: "https://api.together.xyz/settings/api-keys" },
  deepseek:            { keyPlaceholder: "sk-…",           keyHint: "Get from platform.deepseek.com",           docsUrl: "https://platform.deepseek.com" },
  // STT
  deepgram:            { keyPlaceholder: "…",              keyHint: "Get from console.deepgram.com",            docsUrl: "https://console.deepgram.com" },
  assemblyai:          { keyPlaceholder: "…",              keyHint: "Get from assemblyai.com/dashboard",        docsUrl: "https://www.assemblyai.com/dashboard" },
  google_stt:          { keyPlaceholder: "AIza…",          keyHint: "Google Cloud API key with Speech-to-Text enabled", docsUrl: "https://console.cloud.google.com/apis/credentials" },
  speechmatics:        { keyPlaceholder: "…",              keyHint: "Get from portal.speechmatics.com",         docsUrl: "https://portal.speechmatics.com" },
  gladia:              { keyPlaceholder: "…",              keyHint: "Get from app.gladia.io",                   docsUrl: "https://app.gladia.io" },
  // TTS
  elevenlabs:          { keyPlaceholder: "sk_…",           keyHint: "Get from elevenlabs.io/app/speech-synthesis/api-keys", docsUrl: "https://elevenlabs.io/app/speech-synthesis/api-keys" },
  cartesia:            { keyPlaceholder: "…",              keyHint: "Get from play.cartesia.ai/keys",           docsUrl: "https://play.cartesia.ai/keys" },
  playht:              { keyPlaceholder: "…",              keyHint: "Get from app.play.ht/account/api-access",  docsUrl: "https://app.play.ht/account/api-access" },
  lmnt:                { keyPlaceholder: "…",              keyHint: "Get from app.lmnt.com/account",            docsUrl: "https://app.lmnt.com/account" },
  deepgram_tts:        { keyPlaceholder: "…",              keyHint: "Get from console.deepgram.com",            docsUrl: "https://console.deepgram.com" },
  // Embedding
  cohere:              { keyPlaceholder: "…",              keyHint: "Get from dashboard.cohere.com/api-keys",   docsUrl: "https://dashboard.cohere.com/api-keys" },
  voyage:              { keyPlaceholder: "pa-…",           keyHint: "Get from dash.voyageai.com",               docsUrl: "https://dash.voyageai.com" },
  google_embedding:    { keyPlaceholder: "AIza…",          keyHint: "Google Cloud API key with Generative Language API enabled", docsUrl: "https://console.cloud.google.com/apis/credentials" },
  // Self-hosted defaults
  openai_compatible:   { keyPlaceholder: "sk-…",           keyHint: "API key for your self-hosted server (leave blank if not required)", endpointPlaceholder: "http://your-server:8010/v1" },
  ollama:              { keyPlaceholder: "ollama",         keyHint: "Ollama doesn't use a real key — type anything", endpointPlaceholder: "http://host.docker.internal:11434/v1" },
};

function getProviderMeta(slug: string): ProviderMeta {
  return PROVIDER_META[slug] ?? { keyPlaceholder: "…", keyHint: "Paste your API key" };
}

// --------------------------------------------------------------------------- //
// Add modal (verify-before-save)
// --------------------------------------------------------------------------- //

function AddProviderDialog({
  tab, catalog, existingCredentials, onClose, onAdded, onError,
}: {
  tab: TabDef;
  catalog: Catalog;
  existingCredentials: ProviderCredential[];
  onClose(): void;
  onAdded(providerName: string): void | Promise<void>;
  onError(message: string): void;
}) {
  const allProviders = catalog.providers.filter((p) => p.kind === tab.kind);
  const cloudProviders = allProviders.filter((p) => !p.self_hosted);
  const selfHostedProviders = allProviders.filter((p) => p.self_hosted);

  const firstProvider = cloudProviders[0] ?? selfHostedProviders[0] ?? null;

  const [selectedProvider, setSelectedProvider] = useState<CatalogProvider | null>(firstProvider);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [label, setLabel] = useState("primary");
  const [testResult, setTestResult] = useState<CredentialVerifyResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  const meta = selectedProvider ? getProviderMeta(selectedProvider.slug) : null;
  const needsKey = selectedProvider?.requires_credential ?? true;
  const canTest = selectedProvider !== null && (!needsKey || apiKey.trim().length >= 8);
  const canSave = testResult?.ok === true || !needsKey;

  const existingForLabel = selectedProvider
    ? existingCredentials.find(
        (c) => c.provider_id === selectedProvider.id && c.label === label,
      )
    : null;

  async function runTest() {
    if (!selectedProvider) return;
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.catalog.verifyDraft({
        provider_id: selectedProvider.id,
        api_key: needsKey ? apiKey.trim() : "no-key-required",
        base_url: baseUrl.trim() || null,
      });
      setTestResult(result);
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "the test could not be run");
    } finally {
      setTesting(false);
    }
  }

  async function handleAdd() {
    if (!selectedProvider) return;
    setSaving(true);
    try {
      await api.catalog.setCredential({
        provider_id: selectedProvider.id,
        api_key: needsKey ? apiKey.trim() : "no-key-required",
        base_url: baseUrl.trim() || null,
        label,
      });
      await onAdded(selectedProvider.display_name);
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "could not store the key");
    } finally {
      setSaving(false);
    }
  }

  function chooseProvider(id: string) {
    const p = allProviders.find((pr) => pr.id === id) ?? null;
    setSelectedProvider(p);
    setApiKey("");
    // Pre-fill endpoint for self-hosted providers where we know a typical URL.
    if (p) {
      const m = getProviderMeta(p.slug);
      setBaseUrl(p.self_hosted && m.endpointPlaceholder ? m.endpointPlaceholder : "");
    }
    setTestResult(null);
  }

  return (
    <Dialog
      title={`Add ${tab.label} provider`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={testing || saving}>
            Cancel
          </Button>
          {needsKey && (
            <Button
              busy={testing}
              disabled={!canTest || testing || saving}
              onClick={() => void runTest()}
            >
              Test connection
            </Button>
          )}
          <Button
            variant="primary"
            busy={saving}
            disabled={!canSave || saving || testing}
            onClick={() => void handleAdd()}
            title={needsKey && !canSave ? "Run a successful test first" : undefined}
          >
            Add {tab.label}
          </Button>
        </>
      }
    >
      {allProviders.length === 0 ? (
        <Notice tone="warn">
          No active {tab.label} providers are in the platform catalog. A platform
          operator adds them under Providers and Models.
        </Notice>
      ) : (
        <form onSubmit={(e) => e.preventDefault()} noValidate>
          <Field label="Provider" required hint="Choose the provider you have an account with.">
            {(id) => (
              <select
                id={id}
                value={selectedProvider?.id ?? ""}
                onChange={(e) => chooseProvider(e.target.value)}
              >
                {cloudProviders.length > 0 && (
                  <optgroup label="Cloud providers">
                    {cloudProviders.map((p) => (
                      <option key={p.id} value={p.id}>{p.display_name}</option>
                    ))}
                  </optgroup>
                )}
                {selfHostedProviders.length > 0 && (
                  <optgroup label="Self-hosted / Local">
                    {selfHostedProviders.map((p) => (
                      <option key={p.id} value={p.id}>{p.display_name}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            )}
          </Field>

          {selectedProvider && (
            <>
              {needsKey ? (
                <Field
                  label="API key"
                  required
                  hint={
                    meta?.docsUrl
                      ? <>{meta.keyHint} → <a href={meta.docsUrl} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>Get key ↗</a></>
                      : (meta?.keyHint ?? "Encrypted before it is stored. Never shown again.")
                  }
                >
                  {(id) => (
                    <input
                      id={id}
                      type="password"
                      autoComplete="off"
                      value={apiKey}
                      onChange={(e) => { setApiKey(e.target.value); setTestResult(null); }}
                      placeholder={
                        existingForLabel
                          ? `replacing key ending ···${existingForLabel.key_hint}`
                          : (meta?.keyPlaceholder ?? "…")
                      }
                    />
                  )}
                </Field>
              ) : (
                <Notice tone="info">
                  This provider authenticates by network reachability, not by API key —
                  no key is needed.
                </Notice>
              )}

              {/* For self-hosted providers the endpoint field is required; for cloud it is optional override. */}
              <Field
                label={selectedProvider.self_hosted ? "Endpoint URL" : "Endpoint override"}
                hint={
                  selectedProvider.self_hosted
                    ? "Base URL of your self-hosted server."
                    : "Optional. Leave blank to use the provider's default endpoint."
                }
              >
                {(id) => (
                  <input
                    id={id}
                    value={baseUrl}
                    onChange={(e) => { setBaseUrl(e.target.value); setTestResult(null); }}
                    placeholder={meta?.endpointPlaceholder ?? "https://api.example.com/v1"}
                  />
                )}
              </Field>

              <Field
                label="Label"
                hint={`"primary" is the default. Use "fallback" or "local" for resilience tiers.`}
              >
                {(id) => (
                  <input
                    id={id}
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder="primary"
                  />
                )}
              </Field>

              {existingForLabel && (
                <Notice tone="warn">
                  A <strong>{label}</strong> key for this provider already exists (ending{" "}
                  <code>···{existingForLabel.key_hint}</code>). Saving will replace it.
                </Notice>
              )}

              {testResult && (
                <div
                  className="card"
                  style={{
                    padding: "10px 13px",
                    borderColor: testResult.ok ? "var(--ok)" : "var(--err)",
                    marginTop: 4,
                  }}
                >
                  <div
                    className="small"
                    style={{ color: testResult.ok ? "var(--ok)" : "var(--err)", fontWeight: 600 }}
                  >
                    {testResult.ok ? "✓" : "✗"} {testResult.detail}
                  </div>
                  {testResult.checked_url && (
                    <div className="subtle small mono" style={{ marginTop: 3 }}>
                      {testResult.checked_url}
                      {testResult.latency_ms !== null ? ` · ${testResult.latency_ms} ms` : ""}
                    </div>
                  )}
                  {testResult.ok && (
                    <div className="subtle small" style={{ marginTop: 4 }}>
                      Key accepted and endpoint is reachable. It does not confirm a specific
                      model is available to this key.
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </form>
      )}
    </Dialog>
  );
}

// --------------------------------------------------------------------------- //
// Rotate key modal (key entry only, no provider change)
// --------------------------------------------------------------------------- //

function RotateKeyDialog({
  credential, onClose, onRotated, onError,
}: {
  credential: ProviderCredential;
  onClose(): void;
  onRotated(): void | Promise<void>;
  onError(message: string): void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.catalog.setCredential({
        provider_id: credential.provider_id,
        api_key: apiKey.trim(),
        base_url: credential.base_url,
        label: credential.label,
      });
      await onRotated();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "could not rotate the key");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={`Rotate key for ${credential.provider_display_name ?? credential.provider_slug}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={apiKey.trim().length < 8}
            onClick={submit}
          >
            Replace key
          </Button>
        </>
      }
    >
      <form onSubmit={submit} noValidate>
        <Notice tone="info">
          Rotating clears the verified mark. Use <strong>Test</strong> on the table row after
          saving to confirm the new key works.
        </Notice>
        <Field
          label="New API key"
          required
          hint={`Replacing the key ending ···${credential.key_hint ?? ""}. Encrypted on save.`}
        >
          {(id) => (
            <input
              id={id}
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-…"
            />
          )}
        </Field>
      </form>
    </Dialog>
  );
}
