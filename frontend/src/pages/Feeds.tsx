import { memo, useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { useNavigate, useParams } from "react-router-dom";
import type { Decision, FlowEvent, NewsItem, SignalRow } from "../lib/types";
import { describeOcc, fmtCompactUsd, fmtNum, fmtPct, fmtPrice, fmtTimeSec, fmtWhen, signClass } from "../lib/format";
import { useFeedsStore, flowKey, signalKey, type FeedKind } from "../store/feeds";
import { useUiStore } from "../store/ui";
import { VirtualList } from "../components/ui/VirtualList";
import { Empty, ErrorBox, ScoreBar } from "../components/ui/States";
import { Modal } from "../components/ui/Modal";
import { ActionBadge, DecisionItem, symbolHref } from "../components/DecisionItem";
import { sentimentOf } from "../components/NewsList";
import { Icon } from "../components/ui/Icon";

const TABS: { id: FeedKind; label: string }[] = [
  { id: "news", label: "News" },
  { id: "flow", label: "Flow" },
  { id: "signals", label: "Signals" },
  { id: "decisions", label: "Decisions" },
];

const ROW_H: Record<FeedKind, number> = { news: 58, flow: 34, signals: 34, decisions: 58 };

const NewsRow = memo(function NewsRow({ n, style }: { n: NewsItem; style: CSSProperties }) {
  const s = sentimentOf(n);
  const cls = signClass(s);
  return (
    <div className="vrow two-line" style={style} role="listitem">
      <div className="line1">
        <span className={`edge ${cls}`} style={{ alignSelf: "stretch", height: "100%" }} />
        <span className="time">{fmtWhen(n.t)}</span>
        <span className={`badge ${cls === "flat" ? "" : cls}`} style={{ minWidth: 44, justifyContent: "center" }}>
          {s === null ? "—" : fmtNum(s, 2)}
        </span>
        <span className="symcell ellipsis" title={n.symbols.join(", ")}>
          {n.symbols.slice(0, 2).join(", ") || "—"}
        </span>
        <a className="grow" href={n.url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--text)", fontWeight: 500 }} title={n.headline}>
          {n.headline}
        </a>
      </div>
      <div className="line2 ellipsis" title={n.llm?.rationale ?? n.summary}>
        <span className="faint">{n.source}</span>
        {n.llm?.rationale ? <span className="ellipsis">LLM: {n.llm.rationale}</span> : n.summary ? <span className="ellipsis">{n.summary}</span> : null}
      </div>
    </div>
  );
});

const FlowRow = memo(function FlowRow({ f, style, onOpen }: { f: FlowEvent; style: CSSProperties; onOpen: (s: string) => void }) {
  const cls = signClass(f.direction);
  return (
    <div className="vrow clickable" style={style} role="listitem" onClick={() => onOpen(f.contract)} title={describeOcc(f.contract)}>
      <span className={`edge ${cls}`} />
      <span className="time">{fmtTimeSec(f.t)}</span>
      <span className="symcell">{f.underlying}</span>
      <span className={`cell ${f.type === "call" ? "up" : "down"}`} style={{ width: 42, textAlign: "left" }}>
        {f.type === "call" ? "CALL" : "PUT"}
      </span>
      <span className="cell" style={{ width: 70 }}>
        ${fmtPrice(f.strike, f.strike % 1 === 0 ? 0 : 1)}
      </span>
      <span className="cell faint" style={{ width: 84 }}>
        {f.expiration}
      </span>
      <span className="cell" style={{ width: 120 }}>
        {fmtNum(f.size, 0)} @ {fmtPrice(f.price)}
      </span>
      <span className="cell" style={{ width: 80, fontWeight: 600 }}>
        {fmtCompactUsd(f.premium)}
      </span>
      <span className="cell" style={{ width: 46 }}>
        <span className={`badge ${f.side === "ask" ? "up" : f.side === "bid" ? "down" : ""}`}>{f.side}</span>
      </span>
      <span className="cell" style={{ width: 58 }}>
        {f.is_sweep && <span className="badge accent">sweep</span>}
      </span>
      <span className="grow" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <ScoreBar value={Math.min(1, f.unusual_score / 5)} diverging={false} width={60} tone="neutral" />
        <span className="small faint num">{fmtNum(f.unusual_score, 1)}</span>
      </span>
      <span className="cell xsmall faint" style={{ width: 60 }}>
        {f.source}
      </span>
    </div>
  );
});

const SignalRowView = memo(function SignalRowView({ s, style, onOpen }: { s: SignalRow; style: CSSProperties; onOpen: (s: string) => void }) {
  return (
    <div className="vrow clickable" style={style} role="listitem" onClick={() => onOpen(s.symbol)}>
      <span className={`edge ${signClass(s.score)}`} />
      <span className="time">{fmtTimeSec(s.t)}</span>
      <span className="symcell">{s.symbol}</span>
      <span className="cell" style={{ width: 130, textAlign: "left" }}>
        {s.model}
      </span>
      <span className="cell" style={{ width: 90 }}>
        <ScoreBar value={s.score} width={80} />
      </span>
      <span className={`cell ${signClass(s.score)}`} style={{ width: 56 }}>
        {fmtNum(s.score, 2)}
      </span>
      <span className="cell faint" style={{ width: 80 }}>
        conf {fmtPct(s.confidence, { decimals: 0 })}
      </span>
      <span className="cell faint" style={{ width: 60 }}>
        {fmtNum(s.horizon_days, s.horizon_days < 1 ? 1 : 0)}d
      </span>
      <span className="grow faint small">{s.ic === null || s.ic === undefined ? "" : `IC ${fmtNum(s.ic, 3)}`}</span>
    </div>
  );
});

const DecisionRow = memo(function DecisionRow({ d, style, onOpen }: { d: Decision; style: CSSProperties; onOpen: (d: Decision) => void }) {
  return (
    <div className="vrow two-line clickable" style={style} role="listitem" onClick={() => onOpen(d)}>
      <div className="line1">
        <span className="time">{fmtWhen(d.t)}</span>
        <ActionBadge action={d.action} />
        <span className="symcell">{d.symbol}</span>
        <span className="faint small" style={{ width: 110, flex: "none" }}>
          {d.strategy_id}
        </span>
        <span className="grow" title={d.reason}>
          {d.reason}
        </span>
        {d.edge_score !== null && <span className="cell small faint">edge {fmtNum(d.edge_score, 2)}</span>}
      </div>
      <div className="line2 ellipsis">
        {d.llm ? (
          <span className="ellipsis" title={d.llm.rationale}>
            LLM {d.llm.decision} (×{fmtNum(d.llm.size_multiplier, 2)}, {fmtPct(d.llm.confidence, { decimals: 0 })}): {d.llm.rationale}
          </span>
        ) : (
          <span className="faint">{d.order_id ? `order ${d.order_id}` : "no LLM review"}</span>
        )}
      </div>
    </div>
  );
});

function matches(kind: FeedKind, x: unknown, f: string): boolean {
  if (!f) return true;
  const F = f.toUpperCase();
  switch (kind) {
    case "news":
      return (x as NewsItem).symbols.some((s) => s.toUpperCase().includes(F));
    case "flow":
      return (x as FlowEvent).underlying.toUpperCase().includes(F);
    case "signals":
      return (x as SignalRow).symbol.toUpperCase().includes(F) || (x as SignalRow).model.toUpperCase().includes(F);
    case "decisions":
      return (x as Decision).symbol.toUpperCase().includes(F) || (x as Decision).strategy_id.toUpperCase().includes(F);
  }
}

function dirOf(kind: FeedKind, x: unknown): number {
  switch (kind) {
    case "news":
      return sentimentOf(x as NewsItem) ?? 0;
    case "flow":
      return (x as FlowEvent).direction;
    case "signals":
      return (x as SignalRow).score;
    case "decisions":
      return 0;
  }
}

export default function Feeds() {
  const params = useParams();
  const navigate = useNavigate();
  const storedTab = useUiStore((s) => s.feedTab) as FeedKind;
  const setStoredTab = useUiStore((s) => s.setFeedTab);
  const tab: FeedKind = (TABS.find((t) => t.id === params.tab)?.id ?? (TABS.find((t) => t.id === storedTab)?.id || "news")) as FeedKind;
  const [filter, setFilter] = useState("");
  const [dir, setDir] = useState<"all" | "bull" | "bear">("all");
  const [sweepsOnly, setSweepsOnly] = useState(false);
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState<unknown[] | null>(null);
  const [openDecision, setOpenDecision] = useState<Decision | null>(null);

  const feeds = useFeedsStore();
  const live = feeds[tab] as unknown[];

  useEffect(() => {
    setStoredTab(tab);
    void useFeedsStore.getState().ensureLoaded(tab);
    setPaused(false);
    setFrozen(null);
  }, [tab, setStoredTab]);

  const source = paused && frozen ? frozen : live;
  const items = useMemo(() => {
    return source.filter((x) => {
      if (!matches(tab, x, filter.trim())) return false;
      if (tab === "flow" && sweepsOnly && !(x as FlowEvent).is_sweep) return false;
      if (dir !== "all" && tab !== "decisions") {
        const d = dirOf(tab, x);
        if (dir === "bull" ? d <= 0 : d >= 0) return false;
      }
      return true;
    });
  }, [source, tab, filter, dir, sweepsOnly]);

  const openSymbol = useCallback((s: string) => navigate(symbolHref(s)), [navigate]);

  const getKey = useCallback(
    (x: unknown, i: number) => {
      switch (tab) {
        case "news":
          return (x as NewsItem).id;
        case "flow":
          return flowKey(x as FlowEvent) + i;
        case "signals":
          return signalKey(x as SignalRow) + i;
        default:
          return (x as Decision).id;
      }
    },
    [tab],
  );

  const renderRow = useCallback(
    (x: unknown, _i: number, style: CSSProperties) => {
      switch (tab) {
        case "news": {
          const n = x as NewsItem;
          return <NewsRow key={n.id} n={n} style={style} />;
        }
        case "flow": {
          const f = x as FlowEvent;
          return <FlowRow key={flowKey(f) + _i} f={f} style={style} onOpen={openSymbol} />;
        }
        case "signals": {
          const s = x as SignalRow;
          return <SignalRowView key={signalKey(s) + _i} s={s} style={style} onOpen={openSymbol} />;
        }
        default: {
          const d = x as Decision;
          return <DecisionRow key={d.id} d={d} style={style} onOpen={setOpenDecision} />;
        }
      }
    },
    [tab, openSymbol],
  );

  const err = feeds.errors[tab];
  const loading = feeds.loading[tab] && !live.length;

  return (
    <div className="page" style={{ paddingBottom: 16 }}>
      <div className="page-header" style={{ marginBottom: 12 }}>
        <div>
          <h1>Feeds</h1>
          <div className="sub">Live news, options flow, model signals and auto-trader decisions. Newest first, last 500 of each.</div>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={t.id === tab} className={`tab${t.id === tab ? " active" : ""}`} onClick={() => navigate(`/feeds/${t.id}`)}>
            {t.label}
            <span className="count">{(feeds[t.id] as unknown[]).length}</span>
          </button>
        ))}
      </div>
      <div className="feed-toolbar">
        <input className="input input-sm" style={{ width: 200 }} placeholder={tab === "signals" ? "Symbol or model" : tab === "decisions" ? "Symbol or strategy" : "Filter symbol"} value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter" />
        {tab !== "decisions" && (
          <div className="seg" role="group" aria-label="Direction">
            <button className={dir === "all" ? "active" : ""} onClick={() => setDir("all")}>
              All
            </button>
            <button className={dir === "bull" ? "active" : ""} onClick={() => setDir("bull")}>
              Bullish
            </button>
            <button className={dir === "bear" ? "active" : ""} onClick={() => setDir("bear")}>
              Bearish
            </button>
          </div>
        )}
        {tab === "flow" && (
          <label className="checkbox small">
            <input type="checkbox" checked={sweepsOnly} onChange={(e) => setSweepsOnly(e.target.checked)} /> Sweeps only
          </label>
        )}
        <span className="spacer" />
        <span className="small muted">{items.length} shown</span>
        <button
          className="btn btn-sm"
          onClick={() => {
            if (paused) {
              setPaused(false);
              setFrozen(null);
            } else {
              setFrozen(live);
              setPaused(true);
            }
          }}
          aria-pressed={paused}
        >
          {paused ? <Icon name="play" size={10} /> : <Icon name="pause" size={10} />}
          {paused ? "Resume" : "Pause"}
        </button>
        <button className="icon-btn" title="Reload from server" aria-label="Reload" onClick={() => void useFeedsStore.getState().ensureLoaded(tab, true)}>
          {feeds.loading[tab] ? <span className="spinner" /> : <Icon name="refresh" size={14} />}
        </button>
      </div>
      {err && !live.length && <ErrorBox error={err} onRetry={() => void useFeedsStore.getState().ensureLoaded(tab, true)} />}
      {tab === "flow" && (
        <div className="vrow" style={{ position: "relative", height: 26, color: "var(--text-3)", fontSize: "var(--fs-xs)", textTransform: "uppercase", letterSpacing: "0.05em", borderBottom: "1px solid var(--border)" }}>
          <span className="edge flat" />
          <span className="time">Time</span>
          <span className="symcell">Sym</span>
          <span className="cell" style={{ width: 42, textAlign: "left" }}>
            Type
          </span>
          <span className="cell" style={{ width: 70 }}>
            Strike
          </span>
          <span className="cell" style={{ width: 84 }}>
            Expiry
          </span>
          <span className="cell" style={{ width: 120 }}>
            Size @ px
          </span>
          <span className="cell" style={{ width: 80 }}>
            Premium
          </span>
          <span className="cell" style={{ width: 46 }}>
            Side
          </span>
          <span className="cell" style={{ width: 58 }} />
          <span className="grow">Unusual</span>
          <span className="cell" style={{ width: 60 }}>
            Source
          </span>
        </div>
      )}
      <div className="card" style={{ height: "calc(100vh - 290px)", minHeight: 320, overflow: "hidden" }}>
        <VirtualList
          items={items}
          rowHeight={ROW_H[tab]}
          height="fill"
          getKey={getKey}
          renderRow={renderRow}
          ariaLabel={`${tab} feed`}
          empty={
            loading ? (
              <Empty>Loading…</Empty>
            ) : (
              <Empty title={filter ? "No matches" : "Nothing yet"}>
                {filter ? "Try a different filter." : tab === "flow" ? "Flow appears during market hours (snapshot-based on the free options feed)." : "New items stream in live."}
              </Empty>
            )
          }
        />
      </div>
      {openDecision && (
        <Modal title="Decision" onClose={() => setOpenDecision(null)} wide>
          <DecisionItem d={openDecision} />
          <dl className="kv" style={{ marginTop: 12 }}>
            <dt>Decision id</dt>
            <dd className="mono">{openDecision.id}</dd>
            <dt>Order id</dt>
            <dd className="mono">{openDecision.order_id ?? "—"}</dd>
          </dl>
        </Modal>
      )}
    </div>
  );
}
