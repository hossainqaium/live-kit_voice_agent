"use client";

import { Shell } from "@/components/Shell";
import { Loading, Notice } from "@/components/ui";
import { useRequireAuth } from "@/lib/auth";

export default function PlatformDashboard() {
  const { principal, loading } = useRequireAuth();

  if (loading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Platform</h1>
          <p className="page-subtitle">
            Tenants, LiveKit clusters, the AI provider catalog and platform
            capacity.
          </p>
        </div>
      </div>

      <Notice tone="info">
        The platform console is not built yet. Tenants and the provider catalog
        are Phase 4; LiveKit administration and the capacity dashboard are
        Phase 5. Everything the platform can do today is available through the{" "}
        <a href="http://localhost:8200/docs" target="_blank" rel="noreferrer">
          API
        </a>{" "}
        and the CLI.
      </Notice>

      <div className="card">
        <h2 style={{ marginBottom: 8 }}>Signed in as platform staff</h2>
        <p className="muted small" style={{ marginTop: 0 }}>
          This account has no tenant, so the tenant console would refuse every
          request. That is deliberate: acting inside a tenant is an explicit
          operation rather than something a platform administrator does by
          default.
        </p>
        <dl className="small" style={{ margin: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <dt className="subtle" style={{ minWidth: 90 }}>Roles</dt>
            <dd style={{ margin: 0 }}>{principal.roles.join(", ") || "none"}</dd>
          </div>
          <div className="row" style={{ gap: 8, marginTop: 4 }}>
            <dt className="subtle" style={{ minWidth: 90 }}>Permissions</dt>
            <dd style={{ margin: 0 }}>
              {principal.roles.includes("SUPER_ADMIN")
                ? "all (SUPER_ADMIN)"
                : principal.permissions.join(", ") || "none granted"}
            </dd>
          </div>
        </dl>
      </div>
    </Shell>
  );
}
