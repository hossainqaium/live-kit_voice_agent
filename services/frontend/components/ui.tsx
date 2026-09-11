"use client";

/**
 * Shared presentational primitives.
 *
 * Small and explicit rather than a component library: these screens are dense
 * admin forms and tables, and the styling lives in globals.css as tokens, so a
 * dependency would mostly be indirection.
 */

import { useEffect, useId, useRef, useState } from "react";

// --------------------------------------------------------------------------- //
// Buttons
// --------------------------------------------------------------------------- //

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "md" | "sm";
  busy?: boolean;
};

export function Button({
  variant = "default",
  size = "md",
  busy = false,
  disabled,
  children,
  className = "",
  ...rest
}: ButtonProps) {
  const classes = [
    "btn",
    variant === "primary" && "btn-primary",
    variant === "danger" && "btn-danger",
    variant === "ghost" && "btn-ghost",
    size === "sm" && "btn-sm",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button className={classes} disabled={disabled || busy} {...rest}>
      {busy && <span className="spinner" aria-hidden />}
      {children}
    </button>
  );
}

// --------------------------------------------------------------------------- //
// Form fields
// --------------------------------------------------------------------------- //

interface FieldProps {
  label: string;
  hint?: string;
  error?: string | null;
  required?: boolean;
  children: (id: string) => React.ReactNode;
}

export function Field({ label, hint, error, required, children }: FieldProps) {
  const id = useId();
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
        {required && (
          <span aria-hidden style={{ color: "var(--err)" }}>
            {" *"}
          </span>
        )}
      </label>
      {children(id)}
      {error ? (
        <div className="field-error" role="alert">
          {error}
        </div>
      ) : (
        hint && <div className="field-hint">{hint}</div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Status presentation
// --------------------------------------------------------------------------- //

type BadgeTone = "ok" | "warn" | "err" | "info" | "neutral";

export function Badge({
  tone = "neutral",
  dot = false,
  title,
  children,
}: {
  tone?: BadgeTone;
  dot?: boolean;
  /** Hover text. A one-word status often cannot carry its own reason. */
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {dot && <span className="dot" aria-hidden />}
      {children}
    </span>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "ok" | "err" | "info" | "warn";
  children: React.ReactNode;
}) {
  return (
    <div className={`notice notice-${tone}`} role={tone === "err" ? "alert" : "status"}>
      {children}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Dialog
// --------------------------------------------------------------------------- //

interface DialogProps {
  title: string;
  onClose(): void;
  footer?: React.ReactNode;
  children: React.ReactNode;
}

export function Dialog({ title, onClose, footer, children }: DialogProps) {
  const ref = useRef<HTMLDivElement>(null);

  // Escape to dismiss, and focus moves inside on open. Without the focus move,
  // keyboard users stay behind the overlay with no obvious way forward.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <div className="dialog-header">
          <h2>{title}</h2>
        </div>
        <div className="dialog-body">{children}</div>
        {footer && <div className="dialog-footer">{footer}</div>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Toasts
// --------------------------------------------------------------------------- //

export interface Toast {
  id: number;
  tone: "ok" | "err" | "info";
  message: string;
}

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  function push(tone: Toast["tone"], message: string) {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, tone, message }]);
    // Errors stay longer: they usually need reading, not just noticing.
    const ttl = tone === "err" ? 8000 : 4000;
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), ttl);
  }

  return {
    toasts,
    ok: (message: string) => push("ok", message),
    err: (message: string) => push("err", message),
    info: (message: string) => push("info", message),
    dismiss: (id: number) => setToasts((current) => current.filter((t) => t.id !== id)),
  };
}

export function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss(id: number): void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast-${toast.tone}`}>
          <span style={{ flex: 1 }}>{toast.message}</span>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// States
// --------------------------------------------------------------------------- //

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      {children && <p style={{ margin: "0 auto 14px", maxWidth: "46ch" }}>{children}</p>}
      {action}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="empty-state">
      <div className="row" style={{ justifyContent: "center" }}>
        <span className="spinner" aria-hidden />
        <span>{label}</span>
      </div>
    </div>
  );
}

/**
 * A relative timestamp, rendered only after mount.
 *
 * Server and client would otherwise disagree about "2 minutes ago" and React
 * would report a hydration mismatch.
 */
export function RelativeTime({ iso }: { iso: string | null }) {
  const [text, setText] = useState<string>("—");

  useEffect(() => {
    if (!iso) {
      setText("—");
      return;
    }
    function render() {
      const seconds = Math.round((Date.now() - new Date(iso!).getTime()) / 1000);
      if (seconds < 60) setText("just now");
      else if (seconds < 3600) setText(`${Math.floor(seconds / 60)}m ago`);
      else if (seconds < 86400) setText(`${Math.floor(seconds / 3600)}h ago`);
      else setText(new Date(iso!).toLocaleDateString());
    }
    render();
    const timer = setInterval(render, 30_000);
    return () => clearInterval(timer);
  }, [iso]);

  return <span title={iso ?? undefined}>{text}</span>;
}
