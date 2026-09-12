"use client";

/**
 * Tenant settings.
 *
 * The call limits are shown but not editable: they are commercial terms the
 * platform sets (spec 47), and a tenant raising its own concurrency cap would
 * make the limit meaningless. The API refuses them in the request body, so the
 * form does not offer fields that would fail.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import { ApiError, api, type TenantSettings } from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

/** A handful of common zones, so the field is not a blank text box. */
const COMMON_ZONES = [
  "UTC", "Asia/Dhaka", "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore",
  "Europe/London", "Europe/Berlin", "America/New_York", "America/Chicago",
  "America/Los_Angeles", "Australia/Sydney",
];

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "bn", label: "Bengali" },
  { code: "hi", label: "Hindi" },
  { code: "ar", label: "Arabic" },
  { code: "es", label: "Spanish" },
  { code: "fr", label: "French" },
  { code: "de", label: "German" },
];

export default function SettingsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [settings, setSettings] = useState<TenantSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [language, setLanguage] = useState("en");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);

  const canWrite = can("users.manage");

  const load = useCallback(async () => {
    try {
      const row = await api.settings.get();
      setSettings(row);
      setName(row.name);
      setTimezone(row.timezone);
      setLanguage(row.default_language);
      setNotes(row.notes ?? "");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load settings");
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const localZone = typeof Intl !== "undefined"
    ? Intl.DateTimeFormat().resolvedOptions().timeZone
    : "UTC";
  const zones = COMMON_ZONES.includes(localZone) ? COMMON_ZONES : [localZone, ...COMMON_ZONES];

  const dirty =
    settings !== null &&
    (name !== settings.name ||
      timezone !== settings.timezone ||
      language !== settings.default_language ||
      notes !== (settings.notes ?? ""));

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const saved = await api.settings.update({
        name: name.trim(),
        timezone,
        default_language: language,
        notes: notes.trim() || null,
      });
      setSettings(saved);
      toasts.ok("settings saved");
    } catch (err) {
      toasts.err(err instanceof ApiError ? err.message : "could not save settings");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="page-subtitle">
            This tenant's name, timezone and default language. The timezone is
            what a business-hours schedule falls back to when it does not
            declare its own.
          </p>
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {settings === null ? (
        <Loading label="Loading settings…" />
      ) : (
        <div className="stack">
          <div className="card">
            <form onSubmit={onSave} noValidate>
              <Field label="Tenant name" required>
                {(id) => <input id={id} required disabled={!canWrite} value={name}
                  onChange={(e) => setName(e.target.value)} />}
              </Field>

              <Field
                label="Timezone"
                hint={`Used by schedules that do not set their own. This browser is in ${localZone}.`}
              >
                {(id) => (
                  <select id={id} disabled={!canWrite} value={timezone}
                    onChange={(e) => setTimezone(e.target.value)}>
                    {zones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
                  </select>
                )}
              </Field>

              <Field label="Default language" hint="The language a new agent version starts in.">
                {(id) => (
                  <select id={id} disabled={!canWrite} value={language}
                    onChange={(e) => setLanguage(e.target.value)}>
                    {LANGUAGES.map((l) => (
                      <option key={l.code} value={l.code}>{l.label} ({l.code})</option>
                    ))}
                  </select>
                )}
              </Field>

              <Field label="Notes" hint="Free text for whoever administers this tenant.">
                {(id) => <textarea id={id} rows={3} disabled={!canWrite} value={notes}
                  onChange={(e) => setNotes(e.target.value)} />}
              </Field>

              {canWrite ? (
                <div className="row" style={{ marginTop: 4 }}>
                  <Button variant="primary" busy={busy} disabled={!dirty} onClick={onSave}>
                    Save changes
                  </Button>
                  {dirty && (
                    <Button variant="ghost" type="button" onClick={() => void load()}>
                      Discard
                    </Button>
                  )}
                </div>
              ) : (
                <Notice tone="info">
                  Your role can read these settings but not change them.
                </Notice>
              )}
            </form>
          </div>

          <div className="card">
            <div className="row-between" style={{ marginBottom: 12 }}>
              <h2 style={{ margin: 0, fontSize: 15 }}>Identity and limits</h2>
              <Badge tone={settings.status === "ACTIVE" ? "ok" : "warn"} dot>
                {settings.status.toLowerCase()}
              </Badge>
            </div>
            <dl className="kv">
              <dt>Tenant ID</dt>
              <dd className="mono small">{settings.id}</dd>
              <dt>Slug</dt>
              <dd className="mono small">{settings.slug}</dd>
              <dt>Concurrent calls</dt>
              <dd>{settings.max_concurrent_calls ?? "no limit"}</dd>
              <dt>Daily calls</dt>
              <dd>{settings.max_daily_calls ?? "no limit"}</dd>
              <dt>Monthly minutes</dt>
              <dd>{settings.max_monthly_minutes ?? "no limit"}</dd>
            </dl>
            <p className="subtle small" style={{ marginBottom: 0, marginTop: 12 }}>
              The limits and the slug are set by the platform, not here. A
              tenant able to raise its own concurrency cap would make the cap
              meaningless, so the API refuses those fields from this endpoint
              regardless of role.
            </p>
          </div>

          <Notice tone="info">
            Cluster infrastructure is platform-controlled. Redis, media ports,
            RTP ranges, external IPs, TLS termination, load balancers,
            Kubernetes, networking, firewall rules and topology are not visible
            or editable from this tenant.
          </Notice>
        </div>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}
