import { memo, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../lib/api";
import type { IntelConfig, IntelFeed, IntelItem, IntelMover, IntelSource, MacroState, PredictionMarket, SocialStat } from "../lib/types";
import { fmtAgo, fmtCompactUsd, fmtNum, fmtPct, fmtWhen, signClass } from "../lib/format";
import { useNow } from "../hooks/useNow";
import { useOnReconnect } from "../hooks/useOnReconnect";
import { useIntelStore } from "../store/intel";
import { Empty, ErrorBox, Loading, ScoreBar, Switch, Tip } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";
import { toast } from "../components/ui/Toast";

type Tab = "overview" | "statements" | "social" | "markets" | "sources";
const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "statements", label: "Movers" },
  { id: "social", label: "Social" },
  { id: "markets", label: "Prediction markets" },
  { id: "sources", label: "Sources" },
];

const REGIME_TONE: Record<string, string> = { calm: "up", normal: "", elevated: "warn", stressed: "warn", panic: "down" };
const THEME_LABEL: Record<string, string> = {
  tariffs: "Tariffs", fed: "Fed", china: "China", geopolitics: "Geopolitics", energy: "Energy", crypto: "Crypto", tech: "Tech/AI",
  banks: "Banks", health: "Health", fiscal: "Fiscal", autos: "Autos", defense: "Defense", elections: "Elections", recession: "Recession",
  inflation: "Inflation", shutdown: "Shutdown", markets: "Markets",
};

const sentBadge = (s: number | null) => (s === null || Math.abs(s) < 0.02 ? "" : s > 0 ? "up" : "down");
const polLabel = (p: number) => (p > 0 ? "↑ bullish" : p < 0 ? "↑ bearish" : "vol only");
const polTitle = (p: number) => (p > 0 ? "A rising probability is bullish for US equities" : p < 0 ? "A rising probability is bearish for US equities" : "Direction unclear: counts toward event risk only");

function RegimeStats({ m }: { m: MacroState | null }) {
  if (!m) return <div className="stats skeleton" style={{ height: 78 }} />;
  const v = m.vix;
  return (
    <div className="stats">
      <div className="stat">
        <div className="label">
          VIX
          <Tip text="CBOE Volatility Index: the market's 30-day implied volatility for the S&P 500. Rising fast = fear; the bar below the regime label is the percentile of today's level over the past year." />
        </div>
        <div className="value">{v ? v.vix.toFixed(2) : "—"}</div>
        <div className={`hint ${v ? (v.chg_1d > 0 ? "down" : "up") : ""}`}>{v ? `${fmtPct(v.chg_1d, { signed: true, decimals: 1 })} today · ${fmtPct(v.pct, { decimals: 0 })} pctile` : "no data yet"}</div>
      </div>
      <div className="stat">
        <div className="label">
          Regime
          <Tip text="calm < elevated < stressed < panic. Panic = VIX ≥ 35, or a ≥3σ one-day spike with VIX ≥ 25. The Sentiment & Macro Catalyst strategy blocks bullish entries in a panic and halves them when stressed." />
        </div>
        <div className="value">{v ? <span className={`badge ${REGIME_TONE[v.label]}`}>{v.label}</span> : "—"}</div>
        <div className="hint">{v?.inverted ? "curve inverted" : v?.term !== null && v?.term !== undefined ? `3M/spot ${fmtPct(v.term, { signed: true, decimals: 1 })}` : ""}</div>
      </div>
      <div className="stat">
        <div className="label">
          Risk-on
          <Tip text="Blend of the VIX regime (50 %), repricing of macro prediction markets (30 %) and statements by market movers (20 %). +1 = strongly risk-on, −1 = strongly risk-off." />
        </div>
        <div className={`value ${signClass(m.risk_on)}`}>{fmtNum(m.risk_on, 2)}</div>
        <div className="hint">
          <ScoreBar value={m.risk_on} width={90} />
        </div>
      </div>
      <div className="stat">
        <div className="label">
          Event risk
          <Tip text="Chance that a volatility event is near: uncertain prediction markets that resolve soon or are repricing fast, plus VIX stress. High event risk shrinks Catalyst position sizes." />
        </div>
        <div className="value">{fmtPct(m.event_risk, { decimals: 0 })}</div>
        <div className="hint">
          <ScoreBar value={m.event_risk} diverging={false} width={90} tone={m.event_risk > 0.6 ? "down" : "neutral"} />
        </div>
      </div>
      <div className="stat">
        <div className="label">VVIX / SKEW</div>
        <div className="value">{v?.vvix ? v.vvix.toFixed(0) : "—"} / {v?.skew ? v.skew.toFixed(0) : "—"}</div>
        <div className="hint">vol-of-vol / tail hedging</div>
      </div>
      <div className="stat">
        <div className="label">Movers tilt</div>
        <div className={`value ${signClass(m.statements.tilt)}`}>{fmtNum(m.statements.tilt, 2)}</div>
        <div className="hint">{m.statements.n_eff > 0 ? `${fmtNum(m.statements.n_eff, 1)} effective statements` : "none recent"}</div>
      </div>
    </div>
  );
}

const StatementRow = memo(function StatementRow({ it }: { it: IntelItem }) {
  const s = it.sentiment;
  const cls = sentBadge(s);
  const neutral = !it.strength;
  return (
    <div className="intel-row" style={neutral ? { opacity: 0.6 } : undefined}>
      <span className={`edge ${cls || "flat"}`} />
      <div className="intel-main">
        <div className="intel-head">
          {it.mover && <span className="badge accent">{it.mover}</span>}
          <span className={`badge ${cls}`} title="Market impact estimate, −1 bearish … +1 bullish" style={{ minWidth: 44, justifyContent: "center" }}>
            {s === null ? "—" : fmtNum(s, 2)}
          </span>
          <span className="faint small">{it.author}</span>
          <span className="faint xsmall">· {it.source}</span>
          <span className="spacer" />
          <span className="faint xsmall">{fmtWhen(it.t)}</span>
        </div>
        <div className="intel-text">
          {it.url ? (
            <a href={it.url} target="_blank" rel="noopener noreferrer">
              {it.text}
            </a>
          ) : (
            it.text
          )}
        </div>
        {(it.themes.length > 0 || it.symbols.length > 0 || it.llm) && (
          <div className="intel-tags">
            {it.themes.map((t) => (
              <span key={t} className="badge outline">
                {THEME_LABEL[t] ?? t}
              </span>
            ))}
            {it.symbols.slice(0, 6).map((s2) => (
              <Link key={s2} to={`/symbol/${s2}`} className="badge">
                {s2}
              </Link>
            ))}
            {it.llm?.rationale && (
              <span className="faint xsmall ellipsis" title={it.llm.rationale}>
                LLM: {it.llm.rationale}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

const PostRow = memo(function PostRow({ it }: { it: IntelItem }) {
  const cls = sentBadge(it.sentiment);
  return (
    <div className="intel-row">
      <span className={`edge ${cls || "flat"}`} />
      <div className="intel-main">
        <div className="intel-head">
          <span className={`badge ${cls}`} style={{ minWidth: 44, justifyContent: "center" }}>
            {it.label ?? (it.sentiment === null ? "—" : fmtNum(it.sentiment, 2))}
          </span>
          <span className="faint small">{it.author}</span>
          <span className="faint xsmall">· {it.source}</span>
          {it.engagement > 0 && <span className="faint xsmall">· {fmtNum(it.engagement, 0)} eng.</span>}
          <span className="spacer" />
          <span className="faint xsmall">{fmtWhen(it.t)}</span>
        </div>
        <div className="intel-text">
          {it.url ? (
            <a href={it.url} target="_blank" rel="noopener noreferrer">
              {it.text}
            </a>
          ) : (
            it.text
          )}
        </div>
      </div>
    </div>
  );
});

function MarketsTable({ markets, limit }: { markets: PredictionMarket[]; limit?: number }) {
  const now = useNow(60_000);
  const rows = limit ? markets.slice(0, limit) : markets;
  if (!rows.length) return <Empty>No prediction-market data yet.</Empty>;
  return (
    <div className="table-wrap">
      <table className="tbl compact">
        <thead>
          <tr>
            <th>Question</th>
            <th className="num">Yes</th>
            <th className="num">24 h</th>
            <th className="num hide-md">Volume 24 h</th>
            <th className="hide-md">Resolves</th>
            <th>Equity read</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => {
            const days = m.end_ms ? Math.max(0, Math.round((m.end_ms - now) / 86_400_000)) : null;
            return (
              <tr key={m.id}>
                <td style={{ whiteSpace: "normal", minWidth: 220 }}>
                  <a href={m.url || undefined} target="_blank" rel="noopener noreferrer" title={m.question}>
                    {m.question}
                  </a>
                  <div className="xsmall faint">
                    {m.source} · {m.themes.map((t) => THEME_LABEL[t] ?? t).join(", ")}
                  </div>
                </td>
                <td className="num">{fmtPct(m.p, { decimals: 0 })}</td>
                <td className={`num ${signClass(m.dp24)}`}>{Math.abs(m.dp24) < 0.0005 ? "—" : `${m.dp24 > 0 ? "+" : "−"}${(Math.abs(m.dp24) * 100).toFixed(1)} pp`}</td>
                <td className="num hide-md">{fmtCompactUsd(m.volume24h)}</td>
                <td className="hide-md">{days === null ? "—" : days === 0 ? "today" : `${days} d`}</td>
                <td>
                  <span className={`badge ${m.polarity > 0 ? "up" : m.polarity < 0 ? "down" : "outline"}`} title={polTitle(m.polarity)}>
                    {polLabel(m.polarity)}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SocialTable({ rows, selected, onSelect }: { rows: SocialStat[]; selected: string | null; onSelect: (s: string) => void }) {
  if (!rows.length) return <Empty>No social data yet. Sources poll every 1–5 minutes.</Empty>;
  return (
    <div className="table-wrap">
      <table className="tbl compact">
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Mentions 24 h</th>
            <th className="num">Attention</th>
            <th>Polarity</th>
            <th className="num hide-md">Posts</th>
            <th className="num">Signal</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className={`clickable${selected === r.symbol ? " selected" : ""}`} onClick={() => onSelect(r.symbol)}>
              <td>
                <Link to={`/symbol/${r.symbol}`} onClick={(e) => e.stopPropagation()} style={{ fontWeight: 600 }}>
                  {r.symbol}
                </Link>
                {r.in_universe === false && <span className="badge outline" style={{ marginLeft: 6 }} title="Trending on Reddit but not in your universe">trending</span>}
              </td>
              <td className="num">{r.mentions_24h === null ? "—" : fmtNum(r.mentions_24h, 0)}</td>
              <td className={`num ${r.attention > 0.4 ? "up" : r.attention < -0.3 ? "down" : ""}`} title="ln(mentions now / mentions a day ago)">
                {r.attention >= 0 ? "×" : "÷"}
                {Math.exp(Math.abs(r.attention)).toFixed(1)}
              </td>
              <td>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <ScoreBar value={r.polarity} width={70} />
                  <span className={`small num ${signClass(r.polarity)}`}>{fmtNum(r.polarity, 2)}</span>
                </span>
              </td>
              <td className="num hide-md">{r.n}</td>
              <td className={`num ${signClass(r.score_z)}`}>{r.n < 3 && r.mentions_24h === null ? "—" : fmtNum(r.score_z, 2)}</td>
              <td>{r.crowded && <span className="badge warn" title="Loud and one-sided: retail positioning looks stretched, so the signal is cut by 60 %">crowded</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Section({ title, to, children }: { title: string; to?: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">{title}</span>
        <span className="spacer" />
        {to && (
          <Link to={to} className="small">
            All
          </Link>
        )}
      </div>
      {children}
    </div>
  );
}

function Overview() {
  const s = useIntelStore((st) => st.summary);
  const items = useIntelStore((st) => st.items);
  const stmts = useMemo(() => items.filter((i) => i.kind === "statement" && i.strength).slice(0, 5), [items]);
  if (!s) return <Loading />;
  return (
    <div className="grid-2" style={{ alignItems: "start" }}>
      <div className="stack">
        <Section title="Latest mover statements" to="/intel/statements">
          {stmts.length ? stmts.map((i) => <StatementRow key={i.id} it={i} />) : <Empty>No market-relevant statements yet.</Empty>}
        </Section>
        <Section title="Prediction markets" to="/intel/markets">
          <MarketsTable markets={s.markets} limit={6} />
        </Section>
      </div>
      <div className="stack">
        <Section title="Social chatter" to="/intel/social">
          <SocialTable rows={s.social.slice(0, 8)} selected={null} onSelect={() => undefined} />
        </Section>
        <Section title="Source health" to="/intel/sources">
          <SourceTable sources={s.sources} compact />
        </Section>
      </div>
    </div>
  );
}

function Statements() {
  const items = useIntelStore((s) => s.items);
  const [mover, setMover] = useState("all");
  const [hideNeutral, setHideNeutral] = useState(true);
  const movers = useMemo(() => [...new Set(items.filter((i) => i.kind === "statement" && i.mover).map((i) => i.mover as string))].sort(), [items]);
  const rows = useMemo(
    () => items.filter((i) => i.kind === "statement" && (mover === "all" || i.mover === mover) && (!hideNeutral || i.strength)).slice(0, 150),
    [items, mover, hideNeutral],
  );
  return (
    <>
      <div className="feed-toolbar">
        <select className="select input-sm" style={{ width: 180 }} value={mover} onChange={(e) => setMover(e.target.value)} aria-label="Mover">
          <option value="all">All movers</option>
          {movers.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <label className="checkbox small">
          <input type="checkbox" checked={hideNeutral} onChange={(e) => setHideNeutral(e.target.checked)} /> Hide statements with no market read
        </label>
        <span className="spacer" />
        <span className="small muted">{rows.length} shown</span>
      </div>
      <div className="card">{rows.length ? rows.map((i) => <StatementRow key={i.id} it={i} />) : <Empty>No statements match. Movers are configured on the Sources tab.</Empty>}</div>
    </>
  );
}

function Social() {
  const s = useIntelStore((st) => st.summary);
  const items = useIntelStore((st) => st.items);
  const [sym, setSym] = useState<string | null>(null);
  const rows = s?.social ?? [];
  const selected = sym ?? rows[0]?.symbol ?? null;
  const posts = useMemo(() => items.filter((i) => i.kind === "social" && selected && i.symbols.includes(selected)).slice(0, 60), [items, selected]);
  return (
    <div className="grid-2" style={{ alignItems: "start" }}>
      <div className="card">
        <SocialTable rows={rows} selected={selected} onSelect={setSym} />
      </div>
      <div className="card">
        <div className="card-header">
          <span className="card-title">{selected ? `Recent posts — ${selected}` : "Recent posts"}</span>
        </div>
        {posts.length ? posts.map((i) => <PostRow key={i.id} it={i} />) : <Empty>{selected ? "No individual posts for this symbol (counts only)." : "Pick a symbol."}</Empty>}
      </div>
    </div>
  );
}

function Markets() {
  const s = useIntelStore((st) => st.summary);
  const [theme, setTheme] = useState("all");
  const all = s?.markets ?? [];
  const themes = useMemo(() => [...new Set(all.flatMap((m) => m.themes))].sort(), [all]);
  const rows = theme === "all" ? all : all.filter((m) => m.themes.includes(theme));
  return (
    <>
      <div className="feed-toolbar">
        <select className="select input-sm" style={{ width: 180 }} value={theme} onChange={(e) => setTheme(e.target.value)} aria-label="Theme">
          <option value="all">All themes</option>
          {themes.map((t) => (
            <option key={t} value={t}>
              {THEME_LABEL[t] ?? t}
            </option>
          ))}
        </select>
        <span className="spacer" />
        <span className="small muted">{rows.length} markets · probabilities are market-implied, not forecasts</span>
      </div>
      <div className="card">
        <MarketsTable markets={rows} />
      </div>
    </>
  );
}

function SourceTable({ sources, compact, onToggle, onRefresh, busy }: { sources: IntelSource[]; compact?: boolean; onToggle?: (id: string, on: boolean) => void; onRefresh?: (id: string) => void; busy?: string | null }) {
  const now = useNow(10_000);
  return (
    <div className="table-wrap">
      <table className="tbl compact">
        <thead>
          <tr>
            <th>Source</th>
            <th>Status</th>
            <th className="num hide-md">Last OK</th>
            {!compact && <th className="num hide-md">Items</th>}
            {!compact && <th />}
          </tr>
        </thead>
        <tbody>
          {sources.map((s) => {
            const state = !s.configured ? "needs key" : !s.enabled ? "off" : s.last_error ? "error" : s.last_ok ? "ok" : "waiting";
            const dot = state === "ok" ? "ok" : state === "error" ? "bad" : state === "waiting" ? "warn pulse" : "";
            return (
              <tr key={s.id}>
                <td style={{ whiteSpace: "normal" }}>
                  <strong>{s.label}</strong>
                  {!compact && <div className="xsmall faint" style={{ maxWidth: 460 }}>{s.note}</div>}
                </td>
                <td style={{ whiteSpace: "normal" }}>
                  <span className={`dot ${dot}`} /> {state}
                  {s.last_error && <div className="xsmall down" title={s.last_error}>{s.last_error}</div>}
                  {!s.configured && s.needs && <div className="xsmall faint">set {s.needs} in .env</div>}
                </td>
                <td className="num hide-md">{s.last_ok ? fmtAgo(s.last_ok, now) : "—"}</td>
                {!compact && <td className="num hide-md">{s.n_total}</td>}
                {!compact && (
                  <td>
                    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                      <Switch checked={s.enabled} disabled={!s.configured} onChange={(v) => onToggle?.(s.id, v)} title={s.configured ? "Enable/disable this source" : `Needs ${s.needs}`} />
                      <button className="icon-btn" title="Poll now" aria-label={`Poll ${s.label} now`} disabled={!s.configured || busy === s.id} onClick={() => onRefresh?.(s.id)}>
                        {busy === s.id ? <span className="spinner" /> : <Icon name="refresh" size={14} />}
                      </button>
                    </span>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ConfigEditor({ cfg, onSaved }: { cfg: IntelConfig; onSaved: (c: IntelConfig, s: IntelSource[]) => void }) {
  const [subs, setSubs] = useState(cfg.subreddits.join(", "));
  const [handles, setHandles] = useState(cfg.x_handles.join(", "));
  const [movers, setMovers] = useState<IntelMover[]>(cfg.movers);
  const [feeds, setFeeds] = useState<IntelFeed[]>(cfg.rss);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.updateIntelConfig({ subreddits: split(subs), x_handles: split(handles), movers, rss: feeds });
      onSaved(r.config, r.sources);
      toast.success("Intel sources saved");
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const upd = <T,>(arr: T[], i: number, patch: Partial<T>) => arr.map((x, j) => (j === i ? { ...x, ...patch } : x));
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">What to watch</span>
        <Tip text="Subreddits and X handles are read-only inputs. Movers are the people/institutions whose statements move markets: weight (0–1) scales how much their statements count; the query finds headlines about them via Google News. API keys are set in the backend .env, never here." />
      </div>
      <div className="card-body stack">
        {err && <div className="notice danger">{err}</div>}
        <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
          <label className="field">
            <span className="label">Subreddits</span>
            <input className="input" style={{ width: "100%" }} value={subs} onChange={(e) => setSubs(e.target.value)} placeholder="wallstreetbets, stocks, options" />
          </label>
          <label className="field">
            <span className="label">X handles (needs X_BEARER_TOKEN)</span>
            <input className="input" style={{ width: "100%" }} value={handles} onChange={(e) => setHandles(e.target.value)} placeholder="realDonaldTrump, federalreserve" />
          </label>
        </div>
        <div>
          <div className="label small">Market movers</div>
          {movers.map((m, i) => (
            <div key={i} className="row-wrap" style={{ marginTop: 6 }}>
              <Switch checked={m.enabled !== false} onChange={(v) => setMovers(upd(movers, i, { enabled: v }))} />
              <input className="input input-sm" style={{ width: 110 }} value={m.name} onChange={(e) => setMovers(upd(movers, i, { name: e.target.value }))} aria-label="Mover name" />
              <input className="input input-sm input-num" style={{ width: 64 }} type="number" min={0} max={1} step={0.05} value={m.weight} onChange={(e) => setMovers(upd(movers, i, { weight: Number(e.target.value) }))} aria-label="Weight" title="Weight 0–1" />
              <input className="input input-sm" style={{ flex: 1, minWidth: 200 }} value={m.query ?? ""} onChange={(e) => setMovers(upd(movers, i, { query: e.target.value }))} placeholder="Google News query" aria-label="News query" />
              <button className="icon-btn" aria-label="Remove mover" onClick={() => setMovers(movers.filter((_, j) => j !== i))}>
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
          <button className="btn btn-sm btn-ghost" style={{ marginTop: 6 }} onClick={() => setMovers([...movers, { name: "", weight: 0.5, query: "", enabled: true }])}>
            <Icon name="plus" size={12} /> Add mover
          </button>
        </div>
        <div>
          <div className="label small">Official RSS / Atom feeds (public http(s) only)</div>
          {feeds.map((f, i) => (
            <div key={i} className="row-wrap" style={{ marginTop: 6 }}>
              <Switch checked={f.enabled !== false} onChange={(v) => setFeeds(upd(feeds, i, { enabled: v }))} />
              <input className="input input-sm" style={{ width: 130 }} value={f.label ?? f.id} onChange={(e) => setFeeds(upd(feeds, i, { label: e.target.value }))} aria-label="Feed label" />
              <input className="input input-sm" style={{ flex: 1, minWidth: 240 }} value={f.url} onChange={(e) => setFeeds(upd(feeds, i, { url: e.target.value }))} aria-label="Feed URL" />
              <input className="input input-sm" style={{ width: 90 }} value={f.mover ?? ""} onChange={(e) => setFeeds(upd(feeds, i, { mover: e.target.value }))} placeholder="mover" aria-label="Attributed mover" />
              <button className="icon-btn" aria-label="Remove feed" onClick={() => setFeeds(feeds.filter((_, j) => j !== i))}>
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
          <button className="btn btn-sm btn-ghost" style={{ marginTop: 6 }} onClick={() => setFeeds([...feeds, { id: `feed-${feeds.length + 1}`, label: "", url: "https://", weight: 0.6, enabled: true }])}>
            <Icon name="plus" size={12} /> Add feed
          </button>
        </div>
        <div className="form-actions">
          <button className="btn btn-primary" disabled={busy} onClick={() => void save()}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Sources() {
  const [data, setData] = useState<{ sources: IntelSource[]; config: IntelConfig; enabled: boolean } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.intelSources().then((d) => { setData(d); setErr(null); }).catch((e) => setErr(errorMessage(e)));
  useEffect(() => { void load(); }, []);
  useOnReconnect(() => void load());
  if (err && !data) return <ErrorBox error={err} onRetry={() => void load()} />;
  if (!data) return <Loading />;
  const toggle = async (id: string, on: boolean) => {
    try {
      const r = await api.updateIntelConfig({ sources: { [id]: on } });
      setData(r);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  const refresh = async (id: string) => {
    setBusy(id);
    try {
      const r = await api.refreshIntel(id);
      setData((d) => (d ? { ...d, sources: r.sources } : d));
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="stack-lg">
      <div className="notice">
        <strong>How data is collected.</strong> Only official or public interfaces are used — no scraping of logged-in pages. X/Twitter has no free read API (and logged-out
        scraping is blocked and against its terms), so X stays off until you add <code>X_BEARER_TOKEN</code> to <code>.env</code>. Donald Trump's posts come from a public Truth Social
        archive feed. Reddit works best with a free app (<code>REDDIT_CLIENT_ID</code>/<code>REDDIT_CLIENT_SECRET</code>); otherwise ApeWisdom supplies Reddit mention counts.
      </div>
      <div className="card">
        <SourceTable sources={data.sources} onToggle={(id, on) => void toggle(id, on)} onRefresh={(id) => void refresh(id)} busy={busy} />
      </div>
      <ConfigEditor cfg={data.config} onSaved={(config, sources) => setData((d) => (d ? { ...d, config, sources } : d))} />
    </div>
  );
}

export default function IntelPage() {
  const { tab: tabParam } = useParams();
  const navigate = useNavigate();
  const tab: Tab = TABS.some((t) => t.id === tabParam) ? (tabParam as Tab) : "overview";
  const { summary, macro, error, loading } = useIntelStore();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void useIntelStore.getState().load();
    const id = window.setInterval(() => void useIntelStore.getState().load(), 60_000);
    return () => window.clearInterval(id);
  }, []);
  useOnReconnect(() => void useIntelStore.getState().load());
  const refreshAll = async () => {
    setBusy(true);
    try {
      await api.refreshIntel();
      await useIntelStore.getState().load();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const disabled = summary && !summary.enabled;
  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Intel</h1>
          <div className="sub">VIX regime, mover statements, social sentiment and prediction markets.</div>
        </div>
        <button className="btn btn-sm" disabled={busy} onClick={() => void refreshAll()}>
          {busy ? <span className="spinner" /> : <Icon name="refresh" size={12} />} Poll all now
        </button>
      </div>
      {error && !summary && <ErrorBox error={error} onRetry={() => void useIntelStore.getState().load()} />}
      {disabled && (
        <div className="notice warn">
          Intel collection is switched off (<code>INTEL_ENABLED=false</code> or disabled in config). Enable it on the Sources tab.
        </div>
      )}
      <RegimeStats m={macro} />
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={t.id === tab} className={`tab${t.id === tab ? " active" : ""}`} onClick={() => navigate(t.id === "overview" ? "/intel" : `/intel/${t.id}`)}>
            {t.label}
          </button>
        ))}
        {loading && <span className="spinner" style={{ marginLeft: 8 }} />}
      </div>
      {tab === "overview" && <Overview />}
      {tab === "statements" && <Statements />}
      {tab === "social" && <Social />}
      {tab === "markets" && <Markets />}
      {tab === "sources" && <Sources />}
    </div>
  );
}
