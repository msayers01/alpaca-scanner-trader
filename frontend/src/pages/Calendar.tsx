import { useMemo, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../lib/api";
import type { CalEvent, CalKind } from "../lib/types";
import { etYmd, parseYmd, toYmd } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";
import { toast } from "../components/ui/Toast";

const KINDS: { id: CalKind; label: string }[] = [
  { id: "earnings", label: "Earnings" },
  { id: "fomc", label: "FOMC" },
  { id: "macro", label: "Macro" },
  { id: "opex", label: "OPEX" },
  { id: "holiday", label: "Holiday" },
  { id: "early_close", label: "Early close" },
  { id: "user", label: "Mine" },
];

const kindVar = (k: CalKind) => ({ "--kind": `var(--cal-${k})` }) as CSSProperties;
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

function Importance({ n }: { n: number }) {
  return (
    <span className="imp" aria-label={`Importance ${n} of 3`} title={`Importance ${n}/3`}>
      {[1, 2, 3].map((i) => (
        <i key={i} className={i <= n ? "on" : ""} />
      ))}
    </span>
  );
}

function AddEventForm({ date, onAdded }: { date: string; onAdded: (e: CalEvent) => void }) {
  const [form, setForm] = useState({ date, time: "", title: "", kind: "user" as CalKind, symbol: "", importance: 2 as 1 | 2 | 3 });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // follow the selected day until the user edits the date
  const [dateTouched, setDateTouched] = useState(false);
  const shownDate = dateTouched ? form.date : date;
  return (
    <form
      className="stack"
      style={{ gap: 10 }}
      onSubmit={async (e) => {
        e.preventDefault();
        if (!form.title.trim()) return;
        setBusy(true);
        setErr(null);
        try {
          const ev = await api.addCalendarEvent({
            date: shownDate,
            time: form.time || undefined,
            title: form.title.trim(),
            kind: form.kind,
            symbol: form.symbol.trim().toUpperCase() || undefined,
            importance: form.importance,
          });
          onAdded(ev);
          toast.success("Event added");
          setForm((f) => ({ ...f, title: "", symbol: "", time: "" }));
        } catch (e2) {
          setErr(errorMessage(e2));
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="grid-2" style={{ gap: 10 }}>
        <label className="field">
          <span className="label">Date</span>
          <input
            className="input input-sm"
            type="date"
            value={shownDate}
            onChange={(e) => {
              setDateTouched(true);
              setForm((f) => ({ ...f, date: e.target.value }));
            }}
            required
          />
        </label>
        <label className="field">
          <span className="label">Time (ET, optional)</span>
          <input className="input input-sm" type="time" value={form.time} onChange={(e) => setForm((f) => ({ ...f, time: e.target.value }))} />
        </label>
      </div>
      <label className="field">
        <span className="label">Title</span>
        <input className="input input-sm" value={form.title} onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))} placeholder="e.g. CPI release" required maxLength={200} />
      </label>
      <div className="grid-2" style={{ gap: 10 }}>
        <label className="field">
          <span className="label">Kind</span>
          <select className="select input-sm" value={form.kind} onChange={(e) => setForm((f) => ({ ...f, kind: e.target.value as CalKind }))}>
            {KINDS.map((k) => (
              <option key={k.id} value={k.id}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="label">Symbol (optional)</span>
          <input className="input input-sm" value={form.symbol} onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value }))} placeholder="AAPL" />
        </label>
      </div>
      <div className="row">
        <span className="label">Importance</span>
        <div className="seg" role="group" aria-label="Importance">
          {([1, 2, 3] as const).map((i) => (
            <button type="button" key={i} className={form.importance === i ? "active" : ""} onClick={() => setForm((f) => ({ ...f, importance: i }))}>
              {i === 1 ? "Low" : i === 2 ? "Med" : "High"}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <button className="btn btn-sm btn-primary" type="submit" disabled={busy || !form.title.trim()}>
          {busy ? <span className="spinner" /> : <Icon name="plus" size={12} />}
          Add event
        </button>
      </div>
      {err && <div className="error-box">{err}</div>}
    </form>
  );
}

export default function CalendarPage() {
  const todayYmd = etYmd(Date.now());
  const [month, setMonth] = useState(() => {
    const t = parseYmd(todayYmd);
    return new Date(t.getFullYear(), t.getMonth(), 1);
  });
  const [selected, setSelected] = useState(todayYmd);
  const [view, setView] = useState<"day" | "week">("week");
  const [hidden, setHidden] = useState<Set<CalKind>>(new Set());

  const gridStart = useMemo(() => addDays(month, -month.getDay()), [month]);
  const days = useMemo(() => Array.from({ length: 42 }, (_, i) => addDays(gridStart, i)), [gridStart]);
  const startYmd = toYmd(days[0]);
  const endYmd = toYmd(days[41]);
  const events = useAsync((signal) => api.calendar(startYmd, endYmd, { signal }), [startYmd, endYmd]);

  const byDate = useMemo(() => {
    const m = new Map<string, CalEvent[]>();
    for (const e of events.data ?? []) {
      if (hidden.has(e.kind)) continue;
      const arr = m.get(e.date) ?? [];
      arr.push(e);
      m.set(e.date, arr);
    }
    for (const arr of m.values()) arr.sort((a, b) => b.importance - a.importance || (a.time ?? "").localeCompare(b.time ?? ""));
    return m;
  }, [events.data, hidden]);

  const agendaDays = useMemo(() => {
    const sel = parseYmd(selected);
    if (view === "day") return [selected];
    const start = addDays(sel, -sel.getDay());
    return Array.from({ length: 7 }, (_, i) => toYmd(addDays(start, i)));
  }, [selected, view]);

  const shift = (n: number) => setMonth((m) => new Date(m.getFullYear(), m.getMonth() + n, 1));

  const remove = async (ev: CalEvent) => {
    try {
      await api.deleteCalendarEvent(ev.id);
      events.setData((list) => (list ?? []).filter((x) => x.id !== ev.id));
      toast.info("Event deleted");
    } catch (e) {
      toast.error(`Delete failed: ${errorMessage(e)}`);
    }
  };

  const monthLabel = month.toLocaleDateString("en-US", { month: "long", year: "numeric" });

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Calendar</h1>
          <div className="sub">Earnings, FOMC, macro releases, monthly OPEX, market holidays and your own events. Times are US/Eastern.</div>
        </div>
        <div className="row">
          <button className="icon-btn" onClick={() => shift(-1)} aria-label="Previous month">
            <Icon name="chevron-left" />
          </button>
          <span style={{ minWidth: 150, textAlign: "center", fontWeight: 600 }}>{monthLabel}</span>
          <button className="icon-btn" onClick={() => shift(1)} aria-label="Next month">
            <Icon name="chevron-right" />
          </button>
          <button
            className="btn btn-sm"
            onClick={() => {
              const t = parseYmd(todayYmd);
              setMonth(new Date(t.getFullYear(), t.getMonth(), 1));
              setSelected(todayYmd);
            }}
          >
            Today
          </button>
          {events.loading && <span className="spinner" />}
        </div>
      </div>

      <div className="row-wrap" style={{ marginBottom: 12, gap: 12 }}>
        {KINDS.map((k) => {
          const off = hidden.has(k.id);
          return (
            <label key={k.id} className="checkbox small" style={{ ...kindVar(k.id), opacity: off ? 0.5 : 1 }}>
              <input
                type="checkbox"
                checked={!off}
                onChange={() =>
                  setHidden((h) => {
                    const n = new Set(h);
                    if (n.has(k.id)) n.delete(k.id);
                    else n.add(k.id);
                    return n;
                  })
                }
              />
              <span className="kind-swatch" />
              {k.label}
            </label>
          );
        })}
      </div>

      {events.error && (
        <div style={{ marginBottom: 12 }}>
          <ErrorBox error={events.error} onRetry={events.reload} />
        </div>
      )}

      <div className="grid-main">
        <div className="card" style={{ overflow: "hidden" }}>
          <div className="cal-grid" role="grid" aria-label={monthLabel}>
            {DOW.map((d) => (
              <div key={d} className="cal-dow" role="columnheader">
                {d}
              </div>
            ))}
            {days.map((d) => {
              const ymd = toYmd(d);
              const evs = byDate.get(ymd) ?? [];
              const other = d.getMonth() !== month.getMonth();
              return (
                <button
                  key={ymd}
                  role="gridcell"
                  aria-selected={ymd === selected}
                  className={`cal-cell${other ? " other" : ""}${ymd === selected ? " selected" : ""}${ymd === todayYmd ? " today" : ""}`}
                  onClick={() => setSelected(ymd)}
                >
                  <span className="daynum">{d.getDate()}</span>
                  {evs.slice(0, 3).map((e) => (
                    <span key={e.id} className="cal-ev" style={kindVar(e.kind)} title={`${e.title}${e.time ? ` · ${e.time} ET` : ""}`}>
                      <Importance n={e.importance} />
                      <span className="t">{e.symbol && e.kind === "earnings" ? e.symbol : e.title}</span>
                    </span>
                  ))}
                  {evs.length > 3 && <span className="cal-more">+{evs.length - 3} more</span>}
                </button>
              );
            })}
          </div>
        </div>

        <aside className="stack">
          <div className="card">
            <div className="card-header">
              <span className="card-title">{view === "day" ? parseYmd(selected).toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" }) : "Week of " + parseYmd(agendaDays[0]).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
              <span className="spacer" />
              <div className="seg" role="group" aria-label="Agenda range">
                <button className={view === "day" ? "active" : ""} onClick={() => setView("day")}>
                  Day
                </button>
                <button className={view === "week" ? "active" : ""} onClick={() => setView("week")}>
                  Week
                </button>
              </div>
            </div>
            <div className="card-body" style={{ paddingTop: 4 }}>
              {agendaDays.every((d) => !(byDate.get(d)?.length)) ? (
                <Empty>No events.</Empty>
              ) : (
                agendaDays.map((d) => {
                  const evs = byDate.get(d) ?? [];
                  if (!evs.length) return null;
                  return (
                    <div key={d}>
                      {view === "week" && <div className="agenda-date">{parseYmd(d).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}</div>}
                      {evs.map((e) => (
                        <div key={e.id} className="agenda-item" style={kindVar(e.kind)}>
                          <span className="kind-swatch" style={{ marginTop: 5 }} />
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div className="row">
                              <strong style={{ fontWeight: 600 }}>{e.title}</strong>
                              <Importance n={e.importance} />
                            </div>
                            <div className="small muted">
                              {KINDS.find((k) => k.id === e.kind)?.label ?? e.kind}
                              {e.time ? ` · ${e.time} ET` : ""}
                              {e.symbol && (
                                <>
                                  {" · "}
                                  <Link to={`/symbol/${encodeURIComponent(e.symbol)}`}>{e.symbol}</Link>
                                </>
                              )}
                              <span className="faint"> · {e.source}</span>
                            </div>
                          </div>
                          {e.kind === "user" && (
                            <button className="icon-btn" aria-label={`Delete ${e.title}`} title="Delete" onClick={() => void remove(e)}>
                              <Icon name="trash" size={14} />
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  );
                })
              )}
            </div>
          </div>
          <div className="card">
            <div className="card-header">
              <span className="card-title">Add event</span>
            </div>
            <div className="card-body">
              <AddEventForm date={selected} onAdded={(ev) => events.setData((list) => [...(list ?? []), ev])} />
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
