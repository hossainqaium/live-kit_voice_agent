"use client";

/**
 * Session state for the consoles.
 *
 * The identity and permission set always come from `GET /auth/me` rather than
 * from decoding the token in the browser. Two reasons: the server is
 * authoritative about what a user may do, and a client that parses its own
 * token invites the habit of trusting its contents.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError, api, setAuthLostHandler, tokens, type Principal } from "@/lib/api";

interface AuthState {
  principal: Principal | null;
  /** True until the initial session check finishes, so guards do not flash. */
  loading: boolean;
  error: string | null;
  signIn(email: string, password: string): Promise<void>;
  signOut(): void;
  /** After a self-service password change: clear the session and return to login. */
  signOutAfterPasswordChange(): void;
  /** Cosmetic only — the backend enforces permissions independently. */
  can(permission: string): boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const signOut = useCallback(() => {
    tokens.clear();
    setPrincipal(null);
    router.replace("/login");
  }, [router]);

  const signOutAfterPasswordChange = useCallback(() => {
    try {
      sessionStorage.setItem("voice.password_changed", "1");
    } catch {
      // Storage can be blocked; the login form still works without the banner.
    }
    tokens.clear();
    setPrincipal(null);
    router.replace("/login");
  }, [router]);

  // A refresh that fails mid-session has to reach the UI, not just the console.
  useEffect(() => {
    setAuthLostHandler(() => {
      setPrincipal(null);
      router.replace("/login");
    });
    return () => setAuthLostHandler(null);
  }, [router]);

  useEffect(() => {
    let cancelled = false;

    async function restore() {
      if (!tokens.access()) {
        if (!cancelled) setLoading(false);
        return;
      }
      try {
        const me = await api.me();
        if (!cancelled) setPrincipal(me);
      } catch {
        // An invalid or revoked token is indistinguishable from no session.
        tokens.clear();
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void restore();
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    setError(null);
    try {
      const pair = await api.login(email, password);
      tokens.set(pair.access_token, pair.refresh_token);
      const me = await api.me();
      setPrincipal(me);
      // Platform staff have no tenant, so the tenant console would refuse
      // every request. Send them where their account can actually act.
      router.replace(me.is_platform_user ? "/platform" : "/");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : "could not reach the API — is the Configuration API running?";
      setError(message);
      throw err;
    }
  }, [router]);

  const can = useCallback(
    (permission: string) => {
      if (!principal) return false;
      // SUPER_ADMIN holds everything, matching the server's own shortcut.
      if (principal.roles.includes("SUPER_ADMIN")) return true;
      return principal.permissions.includes(permission);
    },
    [principal],
  );

  const value = useMemo<AuthState>(
    () => ({ principal, loading, error, signIn, signOut, signOutAfterPasswordChange, can }),
    [principal, loading, error, signIn, signOut, signOutAfterPasswordChange, can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

/**
 * Redirect to the login screen unless signed in.
 *
 * Convenience, not security: every protected response comes from the API,
 * which checks the token itself. A user who defeats this sees an empty shell
 * and a series of 401s.
 */
export function useRequireAuth(): AuthState {
  const auth = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!auth.loading && !auth.principal) router.replace("/login");
  }, [auth.loading, auth.principal, router]);

  return auth;
}
