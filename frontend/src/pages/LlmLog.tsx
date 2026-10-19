import { memo, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api, errorMessage } from "../lib/api";
import type { LlmCallDetail, LlmCallSummary, LlmCallsResponse } from "../lib/types";
import { fmtDateTime, fmtInt, fmtTimeSec, fmtWhen } from "../lib/format";
import { useChannel } from "../hooks/useChannel";
import { useOnReconnect } from "../hooks/useOnReconnect";
import { VirtualList } from "../components/ui/VirtualList";
import { Empty, ErrorBox, Loading, Tip } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";

const PURPOSES: { id: string; label: string; help: string }[] = [
  { id: "", label: "All", help: "" },
  { id: "gate", label: "Trade gate", help: "Final review of a proposed trade: approve, reduce or reject" },
  { id: "news", label: "News", help: "Turns a headline into per-ticker direction / magnitude signals" },
  { id: "analyst", label: "Analyst", help: "LLM Analyst strategy: reads a full dossier and picks a direction" },
  { id: "test", label: "Test", help: "Test prompts sent from Settings → LLM" },
];
const PURPOSE_LABEL: Record<string, string> = Object.fromEntries(PURPOSES.filter((p) => p.id).map((p) => [p.id, p.label]));
type Status = "" | "ok" | "fail" | "cached";
const MAX_ROWS = 1000;

function matchesFilters(c: LlmCallSummary, purpose: string, status: Status, q: string): boolean {
  if (purpose && c.purpose !== purpose) return false;
  if (status === "ok" && !c.ok) return false;
  if (status === "fail" && c.ok) return false;
  if (status === "cached" && !c.cached) return false;
  if (q && !`${c.preview} ${c.error ?? ""} ${c.purpose}`.toLowerCase().includes(q.toLowerCase())) return false;
  return true;
}

function StatusBadge({ c }: { c: Pick<LlmCallSummary, "ok" | "cached"> }) {
  if (!c.ok) return <span className="badge danger">failed</span>;
  if (c.cached) return <span className="badge outline">cached</span>;
  return <span className="badge up">ok</span>;
}

const CallRow = memo(function CallRow({
  c,
  style,
  selected,
  onOpen,
}: {
  c: LlmCallSummary;
  style: CSSProperties;
  selected: boolean;
  onOpen: (id: string) => void;
}) {
  return (
    <div
      className={`vrow two-line clickable${selected ? " selected" : ""}`}
      style={style}
      role="option"
      aria-selected={selected}
      tabIndex={0}
      onClick={() => onOpen(c.id)}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen(c.id)}
    >
      <div className="line1">
        <span className="time" title={fmtDateTime(c.t)}>
          {fmtTimeSec(c.t)}
        </span>
        <span className="badge accent" style={{ minWidth: 76, justifyContent: "center" }}>
          {PURPOSE_LABEL[c.purpose] ?? c.purpose}
        </span>
        <StatusBadge c={c} />
        <span className="grow" />
        <span className="cell small faint num">{c.cached ? "—" : `${fmtInt(c.latency_ms)} ms`}</span>
        <span className="cell small faint num" style={{ width: 92 }} title="Prompt → completion tokens">
          {c.prompt_tokens != null ? `${fmtInt(c.prompt_tokens)}→${fmtInt(c.completion_tokens ?? 0)}` : ""}
        </span>
      </div>
      <div className={`line2 ellipsis${c.ok ? "" : " down"}`} title={c.preview}>
        {c.preview || <span className="faint">(empty reply)</span>}
      </div>
    </div>
  );
});

/** Pretty-print a prompt: JSON (or "prefix:\n{json}") becomes indented JSON, everything else is shown as-is. */
function prettyPrompt(text: string | null | undefined): string {
  if (!text) return "";
  const i = text.indexOf("{");
  if (i < 0) return text;
  const j = text.lastIndexOf("}");
  try {
    const obj = JSON.parse(text.slice(i, j + 1));
    return `${text.slice(0, i)}${JSON.stringify(obj, null, 2)}${text.slice(j + 1)}`;
  } catch {
    return text;
  }
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn btn-sm"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        });
      }}
      disabled={!text}
      aria-label="Copy to clipboard"
    >
      <Icon name={done ? "check" : "copy"} size={12} /> {done ? "Copied" : "Copy"}
    </button>
  );
}

function Section({ title, text, children, defaultOpen = true, help }: { title: string; text: string; children?: ReactNode; defaultOpen?: boolean; help?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="llm-section">
      <div className="llm-section-head">
        <button className="link-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="caret">{open ? "▾" : "▸"}</span> {title}
        </button>
        {help && <Tip text={help} />}
        <span className="spacer" />
        <span className="xsmall faint">{text ? `${fmtInt(text.length)} chars` : ""}</span>
        <CopyButton text={text} />
      </div>
      {open && (children ?? <pre className="llm-pre">{text || <span className="faint">(none recorded)</span>}</pre>)}
    </section>
  );
}

function CallDetail({ id }: { id: string }) {
  const [d, setD] = useState<LlmCallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    setD(null);
    setError(null);
    api
      .llmCall(id, { signal: ctrl.signal })
      .then(setD)
      .catch((e) => !ctrl.signal.aborted && setError(errorMessage(e)));
    return () => ctrl.abort();
  }, [id]);

  if (error) return <ErrorBox error={error} />;
  if (!d) return <Loading />;
  const parsedText = d.parsed ? JSON.stringify(d.parsed, null, 2) : "";
  const userText = prettyPrompt(d.user);
  return (
    <div className="llm-detail">
      <div className="llm-detail-head">
        <span className="badge accent lg">{PURPOSE_LABEL[d.purpose] ?? d.purpose}</span>
        <StatusBadge c={d} />
        <span className="spacer" />
        <span className="small muted">{fmtDateTime(d.t)}</span>
      </div>
      <dl className="kv llm-meta">
        <dt>Model</dt>
        <dd className="mono">{d.model}</dd>
        <dt>Latency</dt>
        <dd>{d.cached ? "served from cache" : `${fmtInt(d.latency_ms)} ms`}</dd>
        <dt>Tokens</dt>
        <dd>{d.prompt_tokens != null ? `${fmtInt(d.prompt_tokens)} prompt → ${fmtInt(d.completion_tokens ?? 0)} completion` : "—"}</dd>
        {d.error && (
          <>
            <dt>Error</dt>
            <dd className="down">{d.error}</dd>
          </>
        )}
        <dt>Call id</dt>
        <dd className="mono xsmall">{d.id}</dd>
      </dl>
      <Section title="Parsed result" text={parsedText} help="The reply after JSON parsing and schema validation — this is what Vantage acts on.">
        {d.parsed ? <pre className="llm-pre">{parsedText}</pre> : <div className="small down" style={{ padding: "8px 0" }}>{d.error ?? "No valid JSON in the reply"}</div>}
      </Section>
      <Section title="Raw reply" text={d.response} help="Exactly what the model returned, before parsing." />
      <Section title="Input sent (user message)" text={userText} help="The dossier / headline / prompt Vantage sent. JSON is pretty-printed here; the model received it compact." />
      <Section title="System prompt" text={d.system ?? ""} defaultOpen={false} help="Instructions that frame the task. The same for every call of this purpose." />
    </div>
  );
}

export default function LlmLog() {
  const [purpose, setPurpose] = useState("");
  const [status, setStatus] = useState<Status>("");
  const [query, setQuery] = useState("");
  const [serverQ, setServerQ] = useState("");
  const [calls, setCalls] = useState<LlmCallSummary[]>([]);
  const [byPurpose, setByPurpose] = useState<LlmCallsResponse["by_purpose"]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const pending = useRef<LlmCallSummary[]>([]);

  // debounce text search into a server query (searches full prompts/replies, not just previews)
  useEffect(() => {
    const t = window.setTimeout(() => setServerQ(query.trim()), 300);
    return () => window.clearTimeout(t);
  }, [query]);

  const load = useCallback(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .llmCalls({ limit: 500, purpose: purpose || undefined, status: status || undefined, q: serverQ || undefined }, { signal: ctrl.signal })
      .then((r) => {
        setCalls(r.calls);
        setByPurpose(r.by_purpose);
        setSelected((s) => s ?? r.calls[0]?.id ?? null);
      })
      .catch((e) => !ctrl.signal.aborted && setError(errorMessage(e)))
      .finally(() => !ctrl.signal.aborted && setLoading(false));
    return () => ctrl.abort();
  }, [purpose, status, serverQ]);

  useEffect(() => load(), [load]);
  useOnReconnect(() => void load());

  useChannel<LlmCallSummary>("llm", (c) => {
    setByPurpose((rows) => {
      const i = rows.findIndex((r) => r.purpose === c.purpose);
      const next = rows.slice();
      if (i < 0) next.push({ purpose: c.purpose, n: 1, fails: c.ok ? 0 : 1, avg_ms: c.ok && !c.cached ? c.latency_ms : null });
      else next[i] = { ...next[i], n: next[i].n + 1, fails: next[i].fails + (c.ok ? 0 : 1) };
      return next;
    });
    if (!matchesFilters(c, purpose, status, serverQ)) return;
    if (paused) {
      pending.current.unshift(c);
      return;
    }
    setCalls((xs) => [c, ...xs].slice(0, MAX_ROWS));
  });

  const resume = () => {
    setPaused(false);
    if (pending.current.length) {
      const add = pending.current;
      pending.current = [];
      setCalls((xs) => [...add, ...xs].slice(0, MAX_ROWS));
    }
  };

  const totals = useMemo(() => {
    const n = byPurpose.reduce((s, r) => s + r.n, 0);
    const fails = byPurpose.reduce((s, r) => s + (r.fails ?? 0), 0);
    const w = byPurpose.filter((r) => r.avg_ms != null);
    const avg = w.length ? w.reduce((s, r) => s + (r.avg_ms ?? 0) * r.n, 0) / w.reduce((s, r) => s + r.n, 0) : null;
    return { n, fails, avg };
  }, [byPurpose]);

  const renderRow = useCallback(
    (c: LlmCallSummary, _i: number, style: CSSProperties) => <CallRow key={c.id} c={c} style={style} selected={c.id === selected} onOpen={setSelected} />,
    [selected],
  );

  return (
    <div className="page" style={{ paddingBottom: 16 }}>
      <div className="page-header" style={{ marginBottom: 12 }}>
        <div>
          <h1>LLM Log</h1>
          <div className="sub">Every prompt Vantage sends to your local model and every reply, newest first. Click a call to read the full exchange.</div>
        </div>
      </div>

      <div className="stats" style={{ marginBottom: 12 }}>
        <div className="stat">
          <div className="label">Calls (this mode)</div>
          <div className="value">{fmtInt(totals.n)}</div>
        </div>
        <div className="stat">
          <div className="label">Failures</div>
          <div className={`value ${totals.fails ? "down" : ""}`}>{fmtInt(totals.fails)}</div>
          <div className="hint">{totals.n ? `${((totals.fails / totals.n) * 100).toFixed(1)}% of calls` : "—"}</div>
        </div>
        <div className="stat">
          <div className="label">
            Avg latency <Tip text="Mean time for successful, non-cached calls. The first call after the model loads can take a minute." />
          </div>
          <div className="value">{totals.avg != null ? `${fmtInt(totals.avg)} ms` : "—"}</div>
        </div>
        {byPurpose
          .slice()
          .sort((a, b) => b.n - a.n)
          .slice(0, 3)
          .map((r) => (
            <div className="stat" key={r.purpose}>
              <div className="label">{PURPOSE_LABEL[r.purpose] ?? r.purpose}</div>
              <div className="value">{fmtInt(r.n)}</div>
              <div className="hint">{r.avg_ms != null ? `${fmtInt(r.avg_ms)} ms avg` : ""}{r.fails ? ` · ${r.fails} failed` : ""}</div>
            </div>
          ))}
      </div>

      <div className="feed-toolbar">
        <div className="seg" role="group" aria-label="Purpose">
          {PURPOSES.map((p) => (
            <button key={p.id || "all"} className={purpose === p.id ? "active" : ""} onClick={() => setPurpose(p.id)} title={p.help}>
              {p.label}
            </button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Status">
          {(
            [
              ["", "Any"],
              ["ok", "OK"],
              ["fail", "Failed"],
              ["cached", "Cached"],
            ] as [Status, string][]
          ).map(([id, label]) => (
            <button key={id || "any"} className={status === id ? "active" : ""} onClick={() => setStatus(id)}>
              {label}
            </button>
          ))}
        </div>
        <input
          className="input input-sm"
          style={{ width: 220 }}
          placeholder="Search prompts & replies"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search prompts and replies"
        />
        <span className="spacer" />
        <span className="small muted">{calls.length} shown</span>
        <button className="btn btn-sm" onClick={() => (paused ? resume() : setPaused(true))} aria-pressed={paused}>
          <Icon name={paused ? "play" : "pause"} size={10} />
          {paused ? `Resume${pending.current.length ? ` (${pending.current.length})` : ""}` : "Pause"}
        </button>
        <button className="icon-btn" title="Reload from server" aria-label="Reload" onClick={() => void load()}>
          {loading ? <span className="spinner" /> : <Icon name="refresh" size={14} />}
        </button>
      </div>

      {error && <ErrorBox error={error} onRetry={() => void load()} />}

      <div className="llm-split">
        <div className="card llm-list" role="listbox" aria-label="LLM calls">
          <VirtualList
            items={calls}
            rowHeight={58}
            height="fill"
            getKey={(c) => c.id}
            renderRow={renderRow}
            ariaLabel="LLM calls"
            empty={
              loading ? (
                <Empty>Loading…</Empty>
              ) : (
                <Empty title={purpose || status || serverQ ? "No matching calls" : "No LLM calls yet"}>
                  {purpose || status || serverQ
                    ? "Try a different filter."
                    : "Calls appear when news is scored, the trade gate reviews an idea, or the LLM Analyst runs. Use Settings → LLM → Test to send one now."}
                </Empty>
              )
            }
          />
        </div>
        <div className="card llm-detail-card">
          {selected ? <CallDetail key={selected} id={selected} /> : <Empty title="Select a call">Pick a call on the left to read the full prompt and reply.</Empty>}
        </div>
      </div>
      <div className="xsmall faint" style={{ marginTop: 8 }}>
        Stored locally in data/vantage.db (up to 60,000 characters per prompt and reply). Times are US/Eastern · last update {fmtWhen(calls[0]?.t)}
      </div>
    </div>
  );
}
