"use client";

/**
 * Support tickets (Phase 6.0).
 *
 * Manual create from this screen. Server Agent files the same rows during
 * a call via create_ticket() (source=AGENT).
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice,
  RelativeTime, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api,
  type Ticket, type TicketInput, type TicketPriority, type TicketStatus,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

const PRIORITIES: TicketPriority[] = ["LOW", "NORMAL", "HIGH", "URGENT"];
const STATUSES: TicketStatus[] = ["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"];

const STATUS_TONE: Record<TicketStatus, "ok" | "warn" | "err" | "info" | "neutral"> = {
  OPEN: "info",
  IN_PROGRESS: "warn",
  RESOLVED: "ok",
  CLOSED: "neutral",
};

const PRIORITY_TONE: Record<TicketPriority, "ok" | "warn" | "err" | "info" | "neutral"> = {
  LOW: "neutral",
  NORMAL: "info",
  HIGH: "warn",
  URGENT: "err",
};

const EMPTY: TicketInput = { title: "", description: "", priority: "NORMAL", caller_number: "" };

export default function TicketsPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<Ticket[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<TicketStatus | "">("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Ticket | null>(null);
  const [deleting, setDeleting] = useState<Ticket | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const page = await api.tickets.list(50, 0, filter || undefined);
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load tickets");
      setRows([]);
    }
  }, [filter]);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  async function setStatus(ticket: Ticket, status: TicketStatus) {
    setBusyId(ticket.id);
    try {
      await api.tickets.update(ticket.id, { status });
      toasts.ok(`${ticket.ticket_number} is now ${status.toLowerCase().replace("_", " ")}`);
      await load();
    } catch (err) {
      toasts.err(err instanceof Error ? err.message : "could not update");
    } finally {
      setBusyId(null);
    }
  }

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Tickets</h1>
          <p className="page-subtitle">
            Support tickets for this tenant. File one here, or let Server Agent
            file one during a call with create_ticket.
          </p>
        </div>
        <div className="row">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value as TicketStatus | "")}
            style={{ width: 180 }}
            aria-label="Filter by status"
          >
            <option value="">All statuses</option>
            {STATUSES.map((status) => (
              <option key={status} value={status}>{status.toLowerCase().replace("_", " ")}</option>
            ))}
          </select>
          {canWrite && (
            <Button variant="primary" onClick={() => setCreating(true)}>Create a ticket</Button>
          )}
        </div>
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading tickets…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title={filter ? "No tickets in that status" : "No tickets yet"}
            action={canWrite && !filter ? <Button variant="primary" onClick={() => setCreating(true)}>Create a ticket</Button> : undefined}
          >
            {filter
              ? "Try a different filter."
              : "Create one, or wait for Server Agent to file one from a call."}
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Number</th><th>Title</th><th>Priority</th><th>Status</th>
                <th>Source</th><th>Opened</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ticket) => (
                <tr key={ticket.id}>
                  <td className="mono small">{ticket.ticket_number}</td>
                  <td>
                    <div style={{ fontWeight: 500 }}>{ticket.title}</div>
                    {ticket.caller_number && (
                      <div className="subtle small mono">{ticket.caller_number}</div>
                    )}
                  </td>
                  <td>
                    <Badge tone={PRIORITY_TONE[ticket.priority]}>
                      {ticket.priority.toLowerCase()}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={STATUS_TONE[ticket.status]} dot>
                      {ticket.status.toLowerCase().replace("_", " ")}
                    </Badge>
                  </td>
                  <td className="small">
                    {ticket.source === "AGENT"
                      ? ticket.agent_name ?? "agent"
                      : "manual"}
                  </td>
                  <td className="small"><RelativeTime iso={ticket.created_at} /></td>
                  <td>
                    <div className="cell-actions">
                      {canWrite && ticket.status === "OPEN" && (
                        <Button size="sm" busy={busyId === ticket.id}
                          onClick={() => void setStatus(ticket, "IN_PROGRESS")}>
                          Start
                        </Button>
                      )}
                      {canWrite && ticket.status === "IN_PROGRESS" && (
                        <Button size="sm" busy={busyId === ticket.id}
                          onClick={() => void setStatus(ticket, "RESOLVED")}>
                          Resolve
                        </Button>
                      )}
                      {canWrite && (
                        <>
                          <Button size="sm" onClick={() => setEditing(ticket)}>Edit</Button>
                          <Button size="sm" variant="danger" onClick={() => setDeleting(ticket)}>
                            Delete
                          </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {(creating || editing) && (
        <TicketForm
          ticket={editing}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => {
            setCreating(false);
            setEditing(null);
            toasts.ok(msg);
            await load();
          }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Delete ${deleting.ticket_number}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.tickets.remove(deleting.id);
                  toasts.ok(`${deleting.ticket_number} deleted`);
                } catch (err) {
                  toasts.err(err instanceof Error ? err.message : "could not delete");
                }
                setDeleting(null);
                await load();
              }}>Delete</Button>
            </>
          }
        >
          <p style={{ marginTop: 0, marginBottom: 0 }}>
            {deleting.title} will be removed. This does not affect the call that
            filed it, if any.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function TicketForm({
  ticket, onClose, onSaved,
}: {
  ticket: Ticket | null;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [form, setForm] = useState<TicketInput>(
    ticket
      ? {
          title: ticket.title,
          description: ticket.description,
          priority: ticket.priority,
          caller_number: ticket.caller_number ?? "",
          status: ticket.status,
        }
      : EMPTY,
  );
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const payload = {
        title: form.title.trim(),
        description: form.description?.trim() ?? "",
        priority: form.priority,
        caller_number: form.caller_number?.trim() || null,
        ...(ticket ? { status: form.status } : {}),
      };
      if (ticket) {
        await api.tickets.update(ticket.id, payload);
        await onSaved(`${ticket.ticket_number} updated`);
      } else {
        const created = await api.tickets.create(payload);
        await onSaved(`${created.ticket_number} created`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={ticket ? `Edit ${ticket.ticket_number}` : "Create a ticket"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {ticket ? "Save" : "Create"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Title" required>
          {(id) => (
            <input id={id} required value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="Lobby card reader offline" />
          )}
        </Field>
        <Field label="Description">
          {(id) => (
            <textarea id={id} rows={4} value={form.description ?? ""}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          )}
        </Field>
        <div className="field-row">
          <Field label="Priority">
            {(id) => (
              <select id={id} value={form.priority}
                onChange={(e) => setForm({ ...form, priority: e.target.value as TicketPriority })}>
                {PRIORITIES.map((priority) => (
                  <option key={priority} value={priority}>{priority.toLowerCase()}</option>
                ))}
              </select>
            )}
          </Field>
          {ticket && (
            <Field label="Status">
              {(id) => (
                <select id={id} value={form.status}
                  onChange={(e) => setForm({ ...form, status: e.target.value as TicketStatus })}>
                  {STATUSES.map((status) => (
                    <option key={status} value={status}>{status.toLowerCase().replace("_", " ")}</option>
                  ))}
                </select>
              )}
            </Field>
          )}
        </div>
        <Field label="Caller number" hint="Optional. Filled automatically when an agent files the ticket.">
          {(id) => (
            <input id={id} value={form.caller_number ?? ""}
              onChange={(e) => setForm({ ...form, caller_number: e.target.value })}
              placeholder="+8801…" />
          )}
        </Field>
      </form>
    </Dialog>
  );
}
