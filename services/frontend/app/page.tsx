import { ServiceStatus } from "@/components/ServiceStatus";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Phase 0 landing page.
 *
 * Replaced in Phase 4 by the platform console (spec 60) and tenant console
 * (spec 61). For now it exists so the frontend service is real, reachable and
 * wired to the API rather than a placeholder image.
 */
export default function Home() {
  const consoles = [
    {
      title: "Platform Console",
      spec: "spec 60",
      phase: "Phase 4",
      sections:
        "Dashboard · Tenants · LiveKit · Infrastructure · AI Providers · Models · Voices · System Users · Monitoring · Audit Logs · Settings",
    },
    {
      title: "Tenant Console",
      spec: "spec 61",
      phase: "Phase 4",
      sections:
        "Dashboard · AI Agents · Agent Versions · PBXs · SIP Trunks · Phone Numbers · Routing · Business Hours · Knowledge Bases · Tools · Calls · Recordings · Transcripts · Analytics · Users · Usage · Settings",
    },
  ];

  return (
    <main
      style={{
        maxWidth: 860,
        margin: "0 auto",
        padding: "56px 24px 80px",
      }}
    >
      <p
        style={{
          margin: 0,
          color: "var(--text-muted)",
          fontSize: 13,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
        }}
      >
        Phase 0 — Foundations
      </p>

      <h1 style={{ fontSize: 30, lineHeight: 1.2, margin: "10px 0 12px" }}>
        Multi-Tenant AI Voice Agent Platform
      </h1>

      <p style={{ color: "var(--text-muted)", margin: "0 0 28px", maxWidth: 620 }}>
        Control Plane and Voice Execution Plane are running as separate services.
        Agent behaviour is loaded from configuration at call start — nothing
        tenant-specific is compiled in.
      </p>

      <section
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 10,
          padding: "16px 18px",
          marginBottom: 28,
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <strong style={{ fontSize: 14 }}>Configuration API</strong>
          <ServiceStatus />
        </div>
        <div style={{ marginTop: 12, display: "flex", gap: 16, flexWrap: "wrap" }}>
          <a href={`${API_BASE_URL}/docs`} style={{ color: "var(--accent)", fontSize: 13 }}>
            OpenAPI docs
          </a>
          <a href={`${API_BASE_URL}/health`} style={{ color: "var(--accent)", fontSize: 13 }}>
            /health
          </a>
          <a href={`${API_BASE_URL}/ready`} style={{ color: "var(--accent)", fontSize: 13 }}>
            /ready
          </a>
          <a href={`${API_BASE_URL}/metrics`} style={{ color: "var(--accent)", fontSize: 13 }}>
            /metrics
          </a>
        </div>
      </section>

      <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>Coming in later phases</h2>
      <div style={{ display: "grid", gap: 12 }}>
        {consoles.map((console) => (
          <article
            key={console.title}
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 10,
              padding: "14px 18px",
            }}
          >
            <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
              <strong style={{ fontSize: 14 }}>{console.title}</strong>
              <span style={{ color: "var(--text-muted)", fontSize: 12 }}>
                {console.spec} · {console.phase}
              </span>
            </div>
            <p style={{ margin: "8px 0 0", color: "var(--text-muted)", fontSize: 13 }}>
              {console.sections}
            </p>
          </article>
        ))}
      </div>
    </main>
  );
}
