"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { Button, Field, Notice } from "@/components/ui";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { signIn, principal, loading, error } = useAuth();
  const router = useRouter();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [changedNotice, setChangedNotice] = useState(false);

  useEffect(() => {
    try {
      if (sessionStorage.getItem("voice.password_changed") === "1") {
        sessionStorage.removeItem("voice.password_changed");
        setChangedNotice(true);
      }
    } catch {
      // ignore
    }
  }, []);

  // Already signed in: skip the form rather than showing it and then
  // redirecting, which reads as a flicker.
  useEffect(() => {
    if (!loading && principal) {
      router.replace(principal.is_platform_user ? "/platform" : "/");
    }
  }, [loading, principal, router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await signIn(email.trim(), password);
    } catch {
      // The provider records the message; the notice below renders it.
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-screen">
      <div className="auth-card">
        <h1 style={{ fontSize: 19 }}>AI Voice Agent Platform</h1>
        <p className="muted small" style={{ margin: "6px 0 22px" }}>
          Sign in to administer tenants, telephony and AI agents.
        </p>

        {changedNotice && (
          <Notice tone="ok">
            Password changed. Sign in again with the new password — every
            previous session has been revoked.
          </Notice>
        )}
        {error && <Notice tone="err">{error}</Notice>}

        <form onSubmit={onSubmit} noValidate>
          <Field label="Email" required>
            {(id) => (
              <input
                id={id}
                type="email"
                autoComplete="username"
                autoFocus
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
            )}
          </Field>

          <Field label="Password" required>
            {(id) => (
              <input
                id={id}
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>

          <Button
            type="submit"
            variant="primary"
            busy={busy}
            style={{ width: "100%", marginTop: 6 }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="subtle small" style={{ marginTop: 20, marginBottom: 0 }}>
          Accounts are created by a platform administrator. There is no
          self-registration.
        </p>
      </div>
    </main>
  );
}
