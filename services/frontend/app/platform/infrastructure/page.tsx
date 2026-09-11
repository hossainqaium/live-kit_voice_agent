"use client";

/**
 * Infrastructure (spec 60).
 *
 * The dependency probes plus a way into the tools that already exist. The
 * service links are read from the browser's own origin rather than hard-coded
 * hostnames, so they still work when the stack is reached by IP from another
 * machine on the LAN.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import { Badge, Button, Loading, Notice } from "@/components/ui";
import { api, fetchApiHealth, type Capacity, type HealthResponse } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";

interface ServiceLink {
  name: string;
  what: string;
  /** Port on the host, from the compose file's published ports. */
  port: number;
  path?: string;
}

/**
 * Non-default ports throughout: the host already runs other stacks, so every
 * published port was moved off its conventional number.
 */
const SERVICES: ServiceLink[] = [
  { name: "Grafana", what: "Dashboards for the voice latency metrics", port: 3201 },
  { name: "Prometheus", what: "The metric store the dashboards read", port: 9290 },
  { name: "MinIO console", what: "Object storage holding recordings", port: 9201 },
  { name: "API docs", what: "The Configuration API's own OpenAPI UI", port: 8200, path: "/docs" },
];

export default function InfrastructurePage() {
  const { principal, loading: authLoading } = useRequireAuth();

  const [capacity, setCapacity] = useState<Capacity | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [host, setHost] = useState("localhost");

  useEffect(() => {
    // The hostname the operator actually reached this console on. Hard-coding
    // "localhost" would give a dead link to anyone on another machine.
    if (typeof window !== "undefined") setHost(window.location.hostname);
  }, []);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [cap, hp] = await Promise.all([
        api.platform.capacity(),
        fetchApiHealth().catch(() => null),
      ]);
      setCapacity(cap);
      setHealth(hp);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not probe the infrastructure");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  const down = (capacity?.components ?? []).filter((c) => !c.reachable);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Infrastructure</h1>
          <p className="page-subtitle">
            The services this platform runs on, probed live, and the operational
            tools that come with the stack.
          </p>
        </div>
        <Button size="sm" busy={busy} onClick={() => void load()}>Re-probe</Button>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {down.length > 0 && (
        <Notice tone="err">
          {down.map((c) => c.name).join(", ")}{" "}
          {down.length === 1 ? "is" : "are"} unreachable from the Configuration
          API. Calls will not complete while PostgreSQL or LiveKit is down;
          Redis affects live call state rather than call setup.
        </Notice>
      )}

      {capacity === null ? (
        <Loading label="Probing…" />
      ) : (
        <div className="stack">
          <div className="card">
            <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>Dependencies</h2>
            <table>
              <thead>
                <tr><th>Component</th><th>State</th><th>Round trip</th><th>Detail</th></tr>
              </thead>
              <tbody>
                {capacity.components.map((component) => (
                  <tr key={component.name}>
                    <td className="mono small">{component.name}</td>
                    <td>
                      {component.reachable
                        ? <Badge tone="ok" dot>reachable</Badge>
                        : <Badge tone="err" dot>unreachable</Badge>}
                    </td>
                    <td className="mono small">
                      {component.latency_ms === null ? "—" : `${component.latency_ms} ms`}
                    </td>
                    <td className="small subtle" style={{ overflowWrap: "anywhere" }}>
                      {component.detail ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <div className="row-between" style={{ marginBottom: 12 }}>
              <h2 style={{ margin: 0, fontSize: 15 }}>Configuration API</h2>
              {health
                ? <Badge tone="ok" dot>{health.status}</Badge>
                : <Badge tone="err" dot>no response</Badge>}
            </div>
            <dl className="kv">
              <dt>Service</dt>
              <dd className="mono small">{health?.service ?? "—"}</dd>
              <dt>Environment</dt>
              <dd className="mono small">{health?.environment ?? "—"}</dd>
              <dt>Reached at</dt>
              <dd className="mono small">{host}</dd>
            </dl>
          </div>

          <div className="card">
            <h2 style={{ marginTop: 0, marginBottom: 12, fontSize: 15 }}>Operational tools</h2>
            <table>
              <thead><tr><th>Tool</th><th>What it is for</th><th /></tr></thead>
              <tbody>
                {SERVICES.map((service) => (
                  <tr key={service.name}>
                    <td style={{ fontWeight: 500 }}>{service.name}</td>
                    <td className="small subtle">{service.what}</td>
                    <td style={{ textAlign: "right" }}>
                      <a
                        className="btn btn-sm"
                        href={`http://${host}:${service.port}${service.path ?? ""}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open ↗
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="subtle small" style={{ marginBottom: 0, marginTop: 10 }}>
              Every published port is deliberately off its conventional number,
              because this host already runs other stacks. The numbers come
              from the compose file.
            </p>
          </div>
        </div>
      )}
    </Shell>
  );
}
