"use client";

/**
 * Console shell: sidebar navigation, top bar, signed-in identity.
 *
 * The navigation lists the full section set from spec 61 (tenant) and spec 60
 * (platform), with unbuilt sections shown as disabled rather than hidden.
 * Hiding them would misrepresent how much of the console exists; showing them
 * greyed out with a phase note is honest about what is and is not ready.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge, Button } from "@/components/ui";
import { useAuth } from "@/lib/auth";

interface NavEntry {
  label: string;
  href?: string;
  /** Permission needed to see it at all. Cosmetic; the API enforces. */
  permission?: string;
  /** Set when the section is not built yet, and why. */
  pending?: string;
}

interface NavGroup {
  label: string;
  entries: NavEntry[];
}

/** Tenant console (spec 61). */
const TENANT_NAV: NavGroup[] = [
  {
    label: "Overview",
    entries: [{ label: "Dashboard", href: "/" }],
  },
  {
    label: "Telephony",
    entries: [
      { label: "PBXs", href: "/pbxs", permission: "pbxs.read" },
      { label: "SIP Trunks", permission: "sip_trunks.read", pending: "Phase 4" },
      { label: "Phone Numbers", permission: "sip_trunks.read", pending: "Phase 4" },
    ],
  },
  {
    label: "AI Agents",
    entries: [
      { label: "Agents", permission: "agents.read", pending: "Phase 4" },
      { label: "Agent Versions", permission: "agents.read", pending: "Phase 4" },
      { label: "Routing", permission: "agents.read", pending: "Phase 4" },
      { label: "Business Hours", permission: "agents.read", pending: "Phase 4" },
      { label: "Tools", permission: "agents.read", pending: "Phase 6" },
      { label: "Knowledge Bases", permission: "agents.read", pending: "Phase 6" },
    ],
  },
  {
    label: "Activity",
    entries: [
      { label: "Calls", permission: "calls.read", pending: "Phase 4" },
      { label: "Recordings", permission: "recordings.read", pending: "Phase 2b" },
      { label: "Transcripts", permission: "calls.read", pending: "Phase 4" },
      { label: "Analytics", permission: "analytics.read", pending: "Phase 4" },
    ],
  },
  {
    label: "Administration",
    entries: [
      { label: "Users", permission: "users.manage", pending: "Phase 3b" },
      { label: "Usage", permission: "billing.read", pending: "Phase 4" },
      { label: "Settings", pending: "Phase 4" },
    ],
  },
];

/** Platform console (spec 60). */
const PLATFORM_NAV: NavGroup[] = [
  {
    label: "Overview",
    entries: [{ label: "Dashboard", href: "/platform" }],
  },
  {
    label: "Platform",
    entries: [
      { label: "Tenants", pending: "Phase 4" },
      { label: "LiveKit", pending: "Phase 5" },
      { label: "Infrastructure", pending: "Phase 5" },
      { label: "Capacity", pending: "Phase 5" },
    ],
  },
  {
    label: "AI Catalog",
    entries: [
      { label: "Providers", pending: "Phase 4" },
      { label: "Models", pending: "Phase 4" },
      { label: "Voices", pending: "Phase 4" },
    ],
  },
  {
    label: "Operations",
    entries: [
      { label: "System Users", pending: "Phase 3b" },
      { label: "Monitoring", href: "http://localhost:3201" },
      { label: "Audit Logs", pending: "Phase 3b" },
      { label: "Settings", pending: "Phase 4" },
    ],
  },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { principal, signOut, can } = useAuth();
  const pathname = usePathname();

  const isPlatform = principal?.is_platform_user ?? false;
  const groups = isPlatform ? PLATFORM_NAV : TENANT_NAV;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div style={{ fontWeight: 600, fontSize: 14 }}>Voice Agent Platform</div>
          <div className="subtle small" style={{ marginTop: 2 }}>
            {isPlatform ? "Platform console" : "Tenant console"}
          </div>
        </div>

        <nav className="sidebar-nav">
          {groups.map((group) => {
            // Drop a group entirely when the role cannot see any of it, rather
            // than leaving an empty heading.
            const visible = group.entries.filter(
              (entry) => !entry.permission || can(entry.permission),
            );
            if (visible.length === 0) return null;

            return (
              <div key={group.label}>
                <div className="nav-group-label">{group.label}</div>
                {visible.map((entry) =>
                  entry.href ? (
                    <Link
                      key={entry.label}
                      href={entry.href}
                      className="nav-item"
                      aria-current={pathname === entry.href ? "page" : undefined}
                      {...(entry.href.startsWith("http")
                        ? { target: "_blank", rel: "noreferrer" }
                        : {})}
                    >
                      <span>{entry.label}</span>
                      {entry.href.startsWith("http") && (
                        <span className="subtle" aria-hidden>
                          ↗
                        </span>
                      )}
                    </Link>
                  ) : (
                    <span
                      key={entry.label}
                      className="nav-item is-disabled"
                      title={`Not built yet — ${entry.pending}`}
                    >
                      <span>{entry.label}</span>
                      <span className="badge badge-neutral" style={{ fontSize: 10 }}>
                        {entry.pending}
                      </span>
                    </span>
                  ),
                )}
              </div>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className="truncate" title={principal?.email}>
            {principal?.email}
          </div>
          <div className="row" style={{ marginTop: 6, flexWrap: "wrap", gap: 4 }}>
            {principal?.roles.map((role) => (
              <Badge key={role} tone={role === "SUPER_ADMIN" ? "info" : "neutral"}>
                {role}
              </Badge>
            ))}
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="row">
            {isPlatform ? (
              <Badge tone="info" dot>
                Platform — no tenant
              </Badge>
            ) : (
              <span className="small muted mono truncate" title={principal?.tenant_id ?? ""}>
                tenant {principal?.tenant_id?.slice(0, 8)}…
              </span>
            )}
          </div>
          <div className="row">
            <a
              className="btn btn-ghost btn-sm"
              href="http://localhost:8200/docs"
              target="_blank"
              rel="noreferrer"
            >
              API docs ↗
            </a>
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </header>

        <main className="content">{children}</main>
      </div>
    </div>
  );
}
