"use client";

import { useEffect, useState } from "react";
import { fetchApiHealth } from "@/lib/api";

type Status = "checking" | "ok" | "unreachable";

/**
 * Live reachability indicator for the Configuration API.
 *
 * Phase 0 scaffolding: it confirms the browser can reach the Control Plane
 * through the host-published port, which is the boundary most likely to be
 * misconfigured locally.
 */
export function ServiceStatus() {
  const [status, setStatus] = useState<Status>("checking");
  const [detail, setDetail] = useState<string>("");

  useEffect(() => {
    let cancelled = false;

    async function check() {
      try {
        const health = await fetchApiHealth();
        if (!cancelled) {
          setStatus("ok");
          setDetail(`${health.service} · ${health.environment}`);
        }
      } catch (error) {
        if (!cancelled) {
          setStatus("unreachable");
          setDetail(error instanceof Error ? error.message : "unknown error");
        }
      }
    }

    void check();
    const timer = setInterval(check, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const color =
    status === "ok"
      ? "var(--ok)"
      : status === "unreachable"
        ? "var(--err)"
        : "var(--text-muted)";

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <span
        aria-hidden
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          flexShrink: 0,
        }}
      />
      <span style={{ color, fontSize: 13 }}>
        {status === "checking" ? "checking…" : status}
        {detail && status === "ok" ? ` — ${detail}` : ""}
      </span>
    </span>
  );
}
