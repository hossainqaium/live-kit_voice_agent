"use client";

/**
 * Effective platform configuration (spec 60).
 *
 * Read-only, and the screen says why: these values come from the environment,
 * so an editable form would write somewhere the next restart overwrites.
 *
 * Secret values never leave the API — only whether each one is set, which is
 * the operationally useful half of the question.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice } from "@/components/ui";
import { api, type PlatformSettings } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

export default function PlatformSettingsPage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [data, setData] = useState<PlatformSettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.platform.settings());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not read the configuration");
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  // A note on a secret is how the API flags something that needs attention,
  // such as a JWT secret still set to the built-in development value.
  const warnings = Object.values(data?.groups ?? {})
    .flat()
    .filter((entry) => entry.note && (entry.is_secret || !entry.is_set));

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="page-subtitle">
            What the running Configuration API is actually configured with, as
            opposed to what a file says it should be.
          </p>
        </div>
        <Button size="sm" onClick={() => void load()}>Refresh</Button>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {data === null ? (
        <Loading label="Reading configuration…" />
      ) : (
        <div className="stack">
          <Notice tone="info">
            {data.source} Environment: <strong>{data.environment}</strong>.
          </Notice>

          {warnings.map((entry) => (
            <Notice
              key={entry.name}
              tone={entry.name === "JWT_SECRET" || !entry.is_set ? "warn" : "info"}
            >
              <strong className="mono">{entry.name}</strong> — {entry.note}
            </Notice>
          ))}

          {Object.entries(data.groups).map(([group, entries]) => (
            <div key={group} className="card">
              <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>{group}</h2>
              <table>
                <thead>
                  <tr><th style={{ width: "32%" }}>Variable</th><th>Value</th></tr>
                </thead>
                <tbody>
                  {entries.map((entry) => (
                    <tr key={entry.name}>
                      <td className="mono small">{entry.name}</td>
                      <td className="small">
                        {entry.is_secret ? (
                          entry.is_set
                            ? <Badge tone="ok">set — value withheld</Badge>
                            : <Badge tone="err">not set</Badge>
                        ) : (
                          <span className="mono" style={{ overflowWrap: "anywhere" }}>
                            {entry.value || <span className="subtle">empty</span>}
                          </span>
                        )}
                        {entry.note && !entry.is_secret && (
                          <div className="subtle small">{entry.note}</div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}

          <Notice tone="info">
            A secret shows only whether it is set. That is a real operational
            question — whether provider credentials can be decrypted, for
            instance — while the value itself must not leave the process.
          </Notice>
        </div>
      )}
    </Shell>
  );
}
