"use client";

/**
 * Business hours (spec 37).
 *
 * A schedule is evaluated in its own timezone, not the server's, so the
 * timezone is shown next to every schedule and "open now" is the API's answer
 * rather than one computed in the browser — the browser's clock and zone are
 * the viewer's, not the schedule's.
 */

import { useCallback, useEffect, useState } from "react";

import { Shell } from "@/components/Shell";
import {
  Badge, Button, Dialog, EmptyState, Field, Loading, Notice, ToastStack, useToasts,
} from "@/components/ui";
import {
  ApiError, api, DAY_LABELS, DAY_ORDER,
  type BusinessHours, type BusinessHoursInput, type DayOfWeek, type Holiday,
} from "@/lib/api";
import { useAuth, useRequireAuth } from "@/lib/auth";

type IntervalDraft = { day_of_week: DayOfWeek; opens_at: string; closes_at: string };

/** "09:00:00" from the API, "09:00" from an <input type="time">. */
function toInputTime(value: string): string {
  return value.slice(0, 5);
}
function toApiTime(value: string): string {
  return value.length === 5 ? `${value}:00` : value;
}

function summarise(hours: BusinessHours): string {
  if (hours.intervals.length === 0) return "no intervals — always closed";
  const byDay = new Map<DayOfWeek, string[]>();
  for (const interval of hours.intervals) {
    const list = byDay.get(interval.day_of_week) ?? [];
    list.push(`${toInputTime(interval.opens_at)}–${toInputTime(interval.closes_at)}`);
    byDay.set(interval.day_of_week, list);
  }
  return DAY_ORDER.filter((day) => byDay.has(day))
    .map((day) => `${DAY_LABELS[day].slice(0, 3)} ${byDay.get(day)!.join(", ")}`)
    .join(" · ");
}

export default function BusinessHoursPage() {
  const { principal, loading: authLoading } = useRequireAuth();
  const { can } = useAuth();
  const toasts = useToasts();

  const [rows, setRows] = useState<BusinessHours[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<BusinessHours | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<BusinessHours | null>(null);

  const canWrite = can("agents.write");

  const load = useCallback(async () => {
    try {
      const page = await api.businessHours.list();
      setRows(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load schedules");
      setRows([]);
    }
  }, []);

  useEffect(() => { if (principal) void load(); }, [principal, load]);

  if (authLoading || !principal) return <div className="auth-screen"><Loading /></div>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Business Hours</h1>
          <p className="page-subtitle">
            When a routing rule counts as open. Each schedule is evaluated in
            its own timezone, so a schedule for one country stays correct when
            the server moves.
          </p>
        </div>
        {canWrite && <Button variant="primary" onClick={() => setCreating(true)}>Add a schedule</Button>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      <div className="table-wrap">
        {rows === null ? (
          <Loading label="Loading schedules…" />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No schedules"
            action={canWrite ? <Button variant="primary" onClick={() => setCreating(true)}>Add a schedule</Button> : undefined}
          >
            A routing rule with no schedule is treated as always open. Add one
            to send out-of-hours calls somewhere else.
          </EmptyState>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Schedule</th><th>Timezone</th><th>Open</th>
                <th>Holidays</th><th>Right now</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td style={{ fontWeight: 500 }}>{row.name}</td>
                  <td className="small mono">{row.timezone ?? "tenant default"}</td>
                  <td className="small">{summarise(row)}</td>
                  <td className="small">
                    {row.holidays.length
                      ? `${row.holidays.length} listed`
                      : <span className="subtle">none</span>}
                  </td>
                  <td>
                    {row.open_now === null ? (
                      <Badge tone="warn" title="The timezone is not recognised">unknown</Badge>
                    ) : row.open_now ? (
                      <Badge tone="ok" dot>open</Badge>
                    ) : (
                      <Badge tone="neutral">closed</Badge>
                    )}
                  </td>
                  <td>
                    <div className="cell-actions">
                      {canWrite && (
                        <>
                          <Button size="sm" onClick={() => setEditing(row)}>Edit</Button>
                          <Button size="sm" variant="danger" onClick={() => setDeleting(row)}>Remove</Button>
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
        <HoursForm
          hours={editing}
          tenantTimezone={null}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={async (msg) => { setCreating(false); setEditing(null); toasts.ok(msg); await load(); }}
        />
      )}

      {deleting && (
        <Dialog
          title={`Remove ${deleting.name}?`}
          onClose={() => setDeleting(null)}
          footer={
            <>
              <Button onClick={() => setDeleting(null)}>Cancel</Button>
              <Button variant="danger" onClick={async () => {
                try {
                  await api.businessHours.remove(deleting.id);
                  toasts.ok(`${deleting.name} removed`);
                } catch (err) {
                  toasts.err(err instanceof Error ? err.message : "could not remove");
                }
                setDeleting(null);
                await load();
              }}>Remove</Button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            Any routing rule using this schedule will be treated as always
            open. The API refuses the removal if a rule still references it.
          </p>
        </Dialog>
      )}

      <ToastStack toasts={toasts.toasts} onDismiss={toasts.dismiss} />
    </Shell>
  );
}

function HoursForm({
  hours, tenantTimezone, onClose, onSaved,
}: {
  hours: BusinessHours | null;
  tenantTimezone: string | null;
  onClose(): void;
  onSaved(message: string): void | Promise<void>;
}) {
  const [name, setName] = useState(hours?.name ?? "");
  const [timezone, setTimezone] = useState(hours?.timezone ?? "");
  const [intervals, setIntervals] = useState<IntervalDraft[]>(
    hours?.intervals.map((i) => ({
      day_of_week: i.day_of_week,
      opens_at: toInputTime(i.opens_at),
      closes_at: toInputTime(i.closes_at),
    })) ?? [{ day_of_week: "MONDAY", opens_at: "09:00", closes_at: "17:00" }],
  );
  const [holidays, setHolidays] = useState<Holiday[]>(hours?.holidays ?? []);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  /**
   * The browser's own zone, offered as the obvious default. It is the
   * operator's zone, not necessarily the business's, so it is a suggestion
   * rather than a silent default.
   */
  const localZone = typeof Intl !== "undefined"
    ? Intl.DateTimeFormat().resolvedOptions().timeZone
    : "UTC";

  function copyToWeekdays() {
    const first = intervals[0];
    if (!first) return;
    setIntervals(
      (["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"] as DayOfWeek[]).map((day) => ({
        day_of_week: day,
        opens_at: first.opens_at,
        closes_at: first.closes_at,
      })),
    );
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);

    // Checked here as well as server-side so the message names the row.
    const bad = intervals.findIndex((i) => i.opens_at >= i.closes_at);
    if (bad !== -1) {
      setFormError(
        `Interval ${bad + 1} (${DAY_LABELS[intervals[bad].day_of_week]}) closes before it opens.`,
      );
      setBusy(false);
      return;
    }

    try {
      const payload: BusinessHoursInput = {
        name: name.trim(),
        timezone: timezone.trim() || null,
        intervals: intervals.map((i) => ({
          day_of_week: i.day_of_week,
          opens_at: toApiTime(i.opens_at),
          closes_at: toApiTime(i.closes_at),
        })),
        holidays: holidays.filter((h) => h.date),
      };
      if (hours) {
        await api.businessHours.update(hours.id, payload);
        await onSaved(`${payload.name} updated`);
      } else {
        await api.businessHours.create(payload);
        await onSaved(`${payload.name} added`);
      }
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={hours ? `Edit ${hours.name}` : "Add a schedule"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={onSubmit}>
            {hours ? "Save changes" : "Add schedule"}
          </Button>
        </>
      }
    >
      {formError && <Notice tone="err">{formError}</Notice>}
      <form onSubmit={onSubmit} noValidate>
        <Field label="Name" required>
          {(id) => <input id={id} required value={name}
            onChange={(e) => setName(e.target.value)} placeholder="Reception hours" />}
        </Field>

        <Field
          label="Timezone"
          hint={`IANA name. Leave empty to use the tenant default${tenantTimezone ? ` (${tenantTimezone})` : ""}. This browser is in ${localZone}.`}
        >
          {(id) => (
            <div className="row">
              <input id={id} value={timezone} style={{ flex: 1 }}
                onChange={(e) => setTimezone(e.target.value)} placeholder="Asia/Dhaka" />
              <Button size="sm" type="button" onClick={() => setTimezone(localZone)}>
                Use {localZone}
              </Button>
            </div>
          )}
        </Field>

        <div className="form-section-label">Open intervals</div>
        <p className="subtle small" style={{ marginTop: 0 }}>
          The closing time is exclusive, so 09:00–17:00 is closed at 17:00
          exactly. Saving replaces the whole list.
        </p>

        {intervals.map((interval, index) => (
          <div key={index} className="row" style={{ marginBottom: 8, alignItems: "flex-end" }}>
            <select
              value={interval.day_of_week}
              aria-label="Day"
              onChange={(e) => {
                const next = [...intervals];
                next[index] = { ...interval, day_of_week: e.target.value as DayOfWeek };
                setIntervals(next);
              }}
            >
              {DAY_ORDER.map((day) => (
                <option key={day} value={day}>{DAY_LABELS[day]}</option>
              ))}
            </select>
            <input type="time" value={interval.opens_at} aria-label="Opens at"
              onChange={(e) => {
                const next = [...intervals];
                next[index] = { ...interval, opens_at: e.target.value };
                setIntervals(next);
              }} />
            <span className="subtle">to</span>
            <input type="time" value={interval.closes_at} aria-label="Closes at"
              onChange={(e) => {
                const next = [...intervals];
                next[index] = { ...interval, closes_at: e.target.value };
                setIntervals(next);
              }} />
            <Button size="sm" variant="ghost" type="button"
              onClick={() => setIntervals(intervals.filter((_, i) => i !== index))}
              aria-label="Remove interval">
              ✕
            </Button>
          </div>
        ))}

        <div className="row">
          <Button size="sm" type="button" onClick={() =>
            setIntervals([...intervals, { day_of_week: "MONDAY", opens_at: "09:00", closes_at: "17:00" }])}>
            Add interval
          </Button>
          {intervals.length > 0 && (
            <Button size="sm" variant="ghost" type="button" onClick={copyToWeekdays}>
              Apply the first to Mon–Fri
            </Button>
          )}
        </div>

        <div className="form-section-label">Holidays</div>
        <p className="subtle small" style={{ marginTop: 0 }}>
          A dated exception. Closed by default, or tick "open" for a day that
          trades against the usual pattern.
        </p>

        {holidays.map((holiday, index) => (
          <div key={index} className="row" style={{ marginBottom: 8 }}>
            <input type="date" value={holiday.date} aria-label="Date"
              onChange={(e) => {
                const next = [...holidays];
                next[index] = { ...holiday, date: e.target.value };
                setHolidays(next);
              }} />
            <input value={holiday.name ?? ""} placeholder="Name" aria-label="Holiday name"
              style={{ flex: 1 }}
              onChange={(e) => {
                const next = [...holidays];
                next[index] = { ...holiday, name: e.target.value };
                setHolidays(next);
              }} />
            <label className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={holiday.closed === false}
                onChange={(e) => {
                  const next = [...holidays];
                  next[index] = { ...holiday, closed: !e.target.checked };
                  setHolidays(next);
                }} />
              open
            </label>
            <Button size="sm" variant="ghost" type="button"
              onClick={() => setHolidays(holidays.filter((_, i) => i !== index))}
              aria-label="Remove holiday">
              ✕
            </Button>
          </div>
        ))}

        <Button size="sm" type="button"
          onClick={() => setHolidays([...holidays, { date: "", name: "", closed: true }])}>
          Add holiday
        </Button>
      </form>
    </Dialog>
  );
}
