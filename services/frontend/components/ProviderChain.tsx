"use client";

/**
 * Provider selection for the agent builder (spec 18, 24, 25, 26, 55, 62).
 *
 * Three stages — speech to text, language model, text to speech — each with up
 * to three tiers tried in order. The tiers are named rather than numbered
 * because "fallback" and "local" answer different questions: one insures
 * against a vendor having a bad day, the other against the internet being
 * unavailable. A numbered list would hide that distinction behind an index.
 *
 * The API key belongs to the *provider*, not the tier, so two tiers that name
 * the same provider share one key. The control is rendered beside whichever
 * tier surfaced it, and the shared-key note says so — the alternative, a
 * separate credentials screen, makes the operator hold the mapping in their
 * head while filling in a form that depends on it.
 */

import { useState } from "react";

import { Badge, Button, Dialog, Field, Notice } from "@/components/ui";
import {
  ApiError,
  api,
  type AgentVersion,
  type Catalog,
  type CatalogProvider,
  type ProviderCredential,
  type ProviderKind,
} from "@/lib/api";

/** Which version columns one tier writes. */
interface TierFields {
  provider: keyof AgentVersion;
  model: keyof AgentVersion;
  voice?: keyof AgentVersion;
}

interface Tier extends TierFields {
  label: string;
  hint: string;
}

interface Stage {
  kind: ProviderKind;
  title: string;
  blurb: string;
  tiers: Tier[];
}

const STAGES: Stage[] = [
  {
    kind: "STT",
    title: "Speech to text",
    blurb: "Transcribes the caller. The slowest stage in most deployments.",
    tiers: [
      {
        label: "Primary",
        hint: "Used for every call unless it fails.",
        provider: "stt_provider_id",
        model: "stt_model_id",
      },
      {
        label: "Fallback",
        hint: "Tried when the primary errors or times out.",
        provider: "stt_fallback_provider_id",
        model: "stt_fallback_model_id",
      },
      {
        label: "Local",
        hint: "Last resort. Pick a self-hosted provider here, or this tier adds nothing.",
        provider: "stt_local_provider_id",
        model: "stt_local_model_id",
      },
    ],
  },
  {
    kind: "LLM",
    title: "Language model",
    blurb: "Decides what the agent says.",
    tiers: [
      {
        label: "Primary",
        hint: "Used for every call unless it fails.",
        provider: "llm_provider_id",
        model: "llm_model_id",
      },
      {
        label: "Fallback",
        hint: "Tried when the primary errors or times out.",
        provider: "llm_fallback_provider_id",
        model: "llm_fallback_model_id",
      },
    ],
  },
  {
    kind: "TTS",
    title: "Text to speech",
    blurb: "Speaks the reply, and the transfer announcement.",
    tiers: [
      {
        label: "Primary",
        hint: "Used for every call unless it fails.",
        provider: "tts_provider_id",
        model: "tts_model_id",
        voice: "voice_id",
      },
      {
        label: "Fallback",
        hint: "Tried when the primary errors or times out. A different voice is normal here.",
        provider: "tts_fallback_provider_id",
        model: "tts_fallback_model_id",
        voice: "tts_fallback_voice_id",
      },
      {
        label: "Local",
        hint: "Last resort. Pick a self-hosted provider here, or this tier adds nothing.",
        provider: "tts_local_provider_id",
        model: "tts_local_model_id",
        voice: "tts_local_voice_id",
      },
    ],
  },
];

export function ProviderChain({
  draft,
  catalog,
  credentials,
  disabled,
  onSet,
  onCredentialsChanged,
  onError,
}: {
  draft: Partial<AgentVersion>;
  catalog: Catalog | null;
  credentials: ProviderCredential[];
  disabled: boolean;
  onSet<K extends keyof AgentVersion>(key: K, value: AgentVersion[K]): void;
  onCredentialsChanged(): void | Promise<void>;
  onError(message: string): void;
}) {
  const [keyFor, setKeyFor] = useState<CatalogProvider | null>(null);

  if (!catalog) {
    return (
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="stat-label">Speech and reasoning</div>
        <p className="subtle small" style={{ margin: "8px 0 0" }}>Loading the catalog…</p>
      </div>
    );
  }

  if (catalog.providers.length === 0) {
    return (
      <Notice tone="warn">
        The platform catalog holds no active providers, so there is nothing to
        select. A platform operator adds them under Providers and Models.
      </Notice>
    );
  }

  return (
    <>
      {STAGES.map((stage) => (
        <StageCard
          key={stage.kind}
          stage={stage}
          draft={draft}
          catalog={catalog}
          credentials={credentials}
          disabled={disabled}
          onSet={onSet}
          onKeyRequested={setKeyFor}
        />
      ))}

      {keyFor && (
        <CredentialDialog
          provider={keyFor}
          existing={credentials.find((c) => c.provider_id === keyFor.id) ?? null}
          onClose={() => setKeyFor(null)}
          onSaved={onCredentialsChanged}
          onError={onError}
        />
      )}
    </>
  );
}

function StageCard({
  stage, draft, catalog, credentials, disabled, onSet, onKeyRequested,
}: {
  stage: Stage;
  draft: Partial<AgentVersion>;
  catalog: Catalog;
  credentials: ProviderCredential[];
  disabled: boolean;
  onSet<K extends keyof AgentVersion>(key: K, value: AgentVersion[K]): void;
  onKeyRequested(provider: CatalogProvider): void;
}) {
  const providers = catalog.providers.filter((p) => p.kind === stage.kind);

  return (
    <div className="card" style={{ marginBottom: 14, padding: "12px 14px" }}>
      <div className="stat-label">{stage.title}</div>
      <p className="subtle small" style={{ margin: "2px 0 10px" }}>{stage.blurb}</p>

      {providers.length === 0 ? (
        <p className="subtle small" style={{ margin: 0 }}>
          No active {stage.kind} provider in the catalog.
        </p>
      ) : (
        stage.tiers.map((tier) => (
          <TierRow
            key={tier.label}
            stage={stage}
            tier={tier}
            providers={providers}
            draft={draft}
            catalog={catalog}
            credentials={credentials}
            disabled={disabled}
            onSet={onSet}
            onKeyRequested={onKeyRequested}
          />
        ))
      )}
    </div>
  );
}

function TierRow({
  stage, tier, providers, draft, catalog, credentials, disabled, onSet, onKeyRequested,
}: {
  stage: Stage;
  tier: Tier;
  providers: CatalogProvider[];
  draft: Partial<AgentVersion>;
  catalog: Catalog;
  credentials: ProviderCredential[];
  disabled: boolean;
  onSet<K extends keyof AgentVersion>(key: K, value: AgentVersion[K]): void;
  onKeyRequested(provider: CatalogProvider): void;
}) {
  const providerId = (draft[tier.provider] as string | null | undefined) ?? "";
  const modelId = (draft[tier.model] as string | null | undefined) ?? "";
  const voiceId = tier.voice ? ((draft[tier.voice] as string | null | undefined) ?? "") : "";

  const provider = providers.find((p) => p.id === providerId) ?? null;
  const models = catalog.models.filter((m) => m.provider_id === providerId);
  const voices = catalog.voices.filter((v) => v.provider_id === providerId);
  const credential = credentials.find((c) => c.provider_id === providerId) ?? null;

  function chooseProvider(next: string) {
    onSet(tier.provider, (next || null) as AgentVersion[typeof tier.provider]);
    // Clearing the model and voice is the point: they belong to the previous
    // provider, and carrying them over produces a pair the API rejects with a
    // message about a foreign key rather than about the choice just made.
    onSet(tier.model, null as AgentVersion[typeof tier.model]);
    if (tier.voice) onSet(tier.voice, null as AgentVersion[typeof tier.voice]);

    // Pre-select the catalog default, so the common case is one click.
    const fallbackModel = catalog.models.find((m) => m.provider_id === next && m.is_default);
    if (fallbackModel) {
      onSet(tier.model, fallbackModel.id as AgentVersion[typeof tier.model]);
    }
    if (tier.voice) {
      const defaultVoice = catalog.voices.find((v) => v.provider_id === next && v.is_default);
      if (defaultVoice) {
        onSet(tier.voice, defaultVoice.id as AgentVersion[typeof tier.voice]);
      }
    }
  }

  return (
    <div
      style={{
        display: "grid",
        gap: 8,
        padding: "9px 0",
        borderTop: "1px solid var(--border)",
      }}
    >
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <span className="small" style={{ fontWeight: 600, minWidth: 62 }}>{tier.label}</span>
        {tier.label !== "Primary" && !providerId && (
          <span className="subtle small">not configured</span>
        )}
        {tier.label === "Local" && provider && !provider.self_hosted && (
          <Badge
            tone="warn"
            title={
              "This provider is a public API, so it fails for the same reasons " +
              "the tiers above it do. A local tier only helps if it points somewhere " +
              "that does not depend on the internet."
            }
          >
            not self-hosted
          </Badge>
        )}
        {provider && !provider.requires_credential && (
          <Badge tone="neutral" title="This provider needs no API key, so none is asked for.">
            no key needed
          </Badge>
        )}
        {provider?.requires_credential && credential && (
          <Badge
            tone={credential.last_verified_at ? "ok" : "warn"}
            title={
              credential.last_verified_at
                ? `Checked against the provider on ${new Date(credential.last_verified_at).toLocaleString()}`
                : "A key is stored but has not been checked against the provider"
            }
          >
            key ···{credential.key_hint} {credential.last_verified_at ? "verified" : "untested"}
          </Badge>
        )}
        {provider?.requires_credential && !credential && (
          <Badge tone="err" title="Publishing will fail until a key is stored for this provider.">
            no key
          </Badge>
        )}
      </div>

      <div className="form-grid">
        <Field label="Provider" hint={tier.hint}>
          {(id) => (
            <select
              id={id}
              value={providerId}
              disabled={disabled}
              onChange={(e) => chooseProvider(e.target.value)}
            >
              <option value="">— none —</option>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name}
                  {p.self_hosted ? " · self-hosted" : ""}
                </option>
              ))}
            </select>
          )}
        </Field>

        <Field label="Model">
          {(id) => (
            <select
              id={id}
              value={modelId}
              disabled={disabled || !providerId}
              onChange={(e) =>
                onSet(tier.model, (e.target.value || null) as AgentVersion[typeof tier.model])
              }
            >
              <option value="">{providerId ? "— none —" : "select a provider first"}</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.slug}
                  {m.is_default ? " (default)" : ""}
                </option>
              ))}
            </select>
          )}
        </Field>

        {tier.voice && (
          <Field label="Voice">
            {(id) => (
              <select
                id={id}
                value={voiceId}
                disabled={disabled || !providerId}
                onChange={(e) =>
                  onSet(
                    tier.voice as keyof AgentVersion,
                    (e.target.value || null) as AgentVersion[keyof AgentVersion],
                  )
                }
              >
                <option value="">{providerId ? "— provider default —" : "select a provider first"}</option>
                {voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                    {v.language ? ` · ${v.language}` : ""}
                    {v.is_default ? " (default)" : ""}
                  </option>
                ))}
              </select>
            )}
          </Field>
        )}
      </div>

      {provider?.requires_credential && (
        <CredentialControls
          provider={provider}
          credential={credential}
          disabled={disabled}
          onKeyRequested={onKeyRequested}
        />
      )}
    </div>
  );
}

function CredentialControls({
  provider, credential, disabled, onKeyRequested,
}: {
  provider: CatalogProvider;
  credential: ProviderCredential | null;
  disabled: boolean;
  onKeyRequested(provider: CatalogProvider): void;
}) {
  const [result, setResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  async function test() {
    if (!credential) return;
    setTesting(true);
    setResult(null);
    try {
      const outcome = await api.catalog.verifyCredential(credential.id);
      // The endpoint answers 200 whether or not the key works — the request
      // succeeded in finding out. So the outcome is read from the body, not
      // from the absence of an exception.
      setResult(
        `${outcome.ok ? "✓" : "✗"} ${outcome.detail}` +
          (outcome.latency_ms !== null ? ` · ${outcome.latency_ms} ms` : "") +
          ` · ${outcome.checked_url}`,
      );
    } catch (err) {
      setResult(err instanceof ApiError ? `✗ ${err.message}` : "✗ the check could not be run");
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="row small" style={{ gap: 8, flexWrap: "wrap" }}>
      <Button size="sm" variant="ghost" disabled={disabled} onClick={() => onKeyRequested(provider)}>
        {credential ? "Replace key" : "Set key"}
      </Button>
      <Button size="sm" variant="ghost" disabled={disabled || !credential || testing} onClick={test}>
        {testing ? "Testing…" : "Test connection"}
      </Button>
      {result && <span className="subtle mono" style={{ fontSize: 11 }}>{result}</span>}
    </div>
  );
}

function CredentialDialog({
  provider, existing, onClose, onSaved, onError,
}: {
  provider: CatalogProvider;
  existing: ProviderCredential | null;
  onClose(): void;
  onSaved(): void | Promise<void>;
  onError(message: string): void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? "");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.catalog.setCredential({
        provider_id: provider.id,
        api_key: apiKey,
        base_url: baseUrl.trim() || null,
      });
      await onSaved();
      onClose();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "could not store the key");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={`${existing ? "Replace" : "Set"} the key for ${provider.display_name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={submit} disabled={busy || apiKey.trim().length < 8}>
            {busy ? "Storing…" : existing ? "Replace key" : "Store key"}
          </Button>
        </>
      }
    >
      <form onSubmit={submit} noValidate>
        <Field
          label="API key"
          required
          hint="Encrypted before it is stored. It is never shown again, here or anywhere else in the API."
        >
          {(id) => (
            <input
              id={id}
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={existing ? `replacing the key ending ${existing.key_hint}` : "sk-…"}
            />
          )}
        </Field>

        <Field
          label="Endpoint override"
          hint="Optional. Point at a self-hosted or regional endpoint instead of the provider default."
        >
          {(id) => (
            <input
              id={id}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.openai.com/v1"
            />
          )}
        </Field>

        <Notice tone="info">
          One key per provider, shared by every tier and every agent that names
          it. Replacing it here changes it for all of them, and clears the
          verified mark until it is tested again.
        </Notice>
      </form>
    </Dialog>
  );
}
