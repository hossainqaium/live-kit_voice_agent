"use client";

/**
 * Console shell: sidebar navigation, top bar, signed-in identity.
 *
 * The navigation covers the full section set from spec 61 (tenant) and spec 60
 * (platform), and every entry now leads somewhere real. Two sections depend on
 * machinery that is not finished — recordings need worker egress (Plan 2b.3)
 * and knowledge-base ingestion is Phase 6 — so their screens exist, read live
 * data, and say so on the page. A disabled menu entry would hide the
 * configuration that *is* usable behind a feature that is not.
 *
 * The `permission` on an entry is presentation only. The API enforces RBAC
 * independently (spec 8); hiding a section prevents a confusing 403, it does
 * not prevent the request.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { Badge, Button, Dialog, Field, Notice } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface NavEntry {
  label: string;
  href: string;
  /** Permission needed to see it at all. Cosmetic; the API enforces. */
  permission?: string;
  /** Shown as a small tag, for a section whose backing work is incomplete. */
  note?: string;
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
      { label: "SIP Setup Wizard", href: "/sip-wizard", permission: "sip_trunks.write" },
      { label: "PBXs", href: "/pbxs", permission: "pbxs.read" },
      { label: "SIP Trunks", href: "/sip-trunks", permission: "sip_trunks.read" },
      { label: "Dispatch Rules", href: "/dispatch-rules", permission: "sip_trunks.read" },
      { label: "Phone Numbers", href: "/phone-numbers", permission: "sip_trunks.read" },
    ],
  },
  {
    label: "AI Agents",
    entries: [
      { label: "Agents", href: "/agents", permission: "agents.read" },
      { label: "AI Setup", href: "/ai-setup", permission: "agents.read" },
      { label: "Routing", href: "/routing", permission: "agents.read" },
      { label: "Business Hours", href: "/business-hours", permission: "agents.read" },
      { label: "Transfer Targets", href: "/transfer-destinations", permission: "agents.read" },
      { label: "Tools", href: "/tools", permission: "agents.read" },
      {
        label: "Knowledge Bases",
        href: "/knowledge-bases",
        permission: "agents.read",
      },
    ],
  },
  {
    label: "Activity",
    entries: [
      { label: "Tickets", href: "/tickets", permission: "calls.read" },
      { label: "Calls", href: "/calls", permission: "calls.read" },
      { label: "Transcripts", href: "/calls", permission: "calls.read" },
      // Live, and empty until the worker starts egress (Plan 2b.3).
      { label: "Recordings", href: "/recordings", permission: "recordings.read", note: "no egress" },
      { label: "Analytics", href: "/analytics", permission: "analytics.read" },
    ],
  },
  {
    label: "Administration",
    entries: [
      { label: "Users", href: "/users", permission: "users.manage" },
      { label: "Usage", href: "/usage", permission: "billing.read" },
      { label: "Settings", href: "/settings" },
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
      { label: "Tenants", href: "/platform/tenants" },
      { label: "LiveKit", href: "/platform/livekit" },
      { label: "Infrastructure", href: "/platform/infrastructure" },
      { label: "Capacity", href: "/platform/capacity" },
    ],
  },
  {
    label: "AI Catalog",
    entries: [
      { label: "Providers", href: "/platform/providers" },
      { label: "Models", href: "/platform/models" },
      { label: "Voices", href: "/platform/voices" },
    ],
  },
  {
    label: "Operations",
    entries: [
      { label: "System Users", href: "/platform/system-users" },
      { label: "Audit Logs", href: "/platform/audit-logs" },
      { label: "Monitoring", href: "http://localhost:3201" },
      { label: "Settings", href: "/platform/settings" },
    ],
  },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { principal, signOut, signOutAfterPasswordChange, can } = useAuth();
  const pathname = usePathname();
  const [changingPassword, setChangingPassword] = useState(false);

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
                {visible.map((entry) => {
                  const external = entry.href.startsWith("http");
                  return (
                    <Link
                      key={`${entry.label}-${entry.href}`}
                      href={entry.href}
                      className="nav-item"
                      aria-current={pathname === entry.href ? "page" : undefined}
                      {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
                    >
                      <span>{entry.label}</span>
                      {external && (
                        <span className="subtle" aria-hidden>
                          ↗
                        </span>
                      )}
                      {entry.note && (
                        <span className="badge badge-neutral" style={{ fontSize: 10 }}>
                          {entry.note}
                        </span>
                      )}
                    </Link>
                  );
                })}
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
            <Button size="sm" variant="ghost" onClick={() => setChangingPassword(true)}>
              Change password
            </Button>
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </header>

        <main className="content">{children}</main>
      </div>

      {changingPassword && (
        <ChangePasswordForm
          onClose={() => setChangingPassword(false)}
          onChanged={signOutAfterPasswordChange}
        />
      )}
    </div>
  );
}

function ChangePasswordForm({
  onClose,
  onChanged,
}: {
  onClose(): void;
  onChanged(): void;
}) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (newPassword !== confirm) {
      setFormError("the new passwords do not match");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await api.changePassword(currentPassword, newPassword);
      onChanged();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not change the password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title="Change your password"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            Change password
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Current password" required>
          {(id) => (
            <input
              id={id}
              type="password"
              required
              autoComplete="current-password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
            />
          )}
        </Field>
        <Field label="New password" required hint="At least 12 characters.">
          {(id) => (
            <input
              id={id}
              type="password"
              required
              minLength={12}
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          )}
        </Field>
        <Field label="Confirm new password" required>
          {(id) => (
            <input
              id={id}
              type="password"
              required
              minLength={12}
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          )}
        </Field>
        <Notice tone="info">
          Every existing session — including this one — stops immediately.
          You will be asked to sign in again.
        </Notice>
      </form>
    </Dialog>
  );
}
