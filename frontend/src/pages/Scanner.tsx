import { Fragment, memo, useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../lib/api";
import type { ScanCandidate, ScanResult } from "../lib/types";
import { channels } from "../lib/ws";
import { describeOcc, fmtAgo, fmtCompact, fmtExpiry, fmtNum, fmtPct, fmtPrice, fmtUsd, signClass } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { useChannel } from "../hooks/useChannel";
import { defaultsOf, ParamForm, type ParamValues } from "../components/ui/ParamForm";
import { Empty, ErrorBox, Loading, Tip } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";
import { toast } from "../components/ui/Toast";

type SortKey =
  | "contract"
  | "type"
  | "strike"
  | "dte"
  | "mid"
  | "spread_pct"
  | "iv"
  | "iv_rank"
  | "delta"
  | "ev_pct"
  | "pop"
  | "kelly"
  | "edge_score"
  | "volume";

const COLS: { key: SortKey; label: string; num?: boolean; tip?: string }[] = [
  { key: "contract", label: "Contract" },
  { key: "type", label: "Type" },
  { key: "strike", label: "Strike", num: true },
  { key: "dte", label: "Exp / DTE", num: true },
  { key: "mid", label: "Mid", num: true },
  { key: "spread_pct", label: "Spread", num: true, tip: "(ask − bid) / mid. Wide spreads are a hidden cost on entry and exit." },
  { key: "iv", label: "IV", num: true, tip: "Implied volatility (annualised) from the option's mid price." },
  { key: "iv_rank", label: "IV rank", num: true, tip: "(IV − min) / (max − min) over the stored ATM-IV history. Low = options cheap vs their own past." },
  { key: "delta", label: "Δ", num: true, tip: "Option delta: ≈ change in option price per $1 move in the underlying." },
  { key: "ev_pct", label: "EV", num: true, tip: "Expected return on premium at the planned exit horizon, under our physical forecast (μ, σ), net of spread costs." },
  { key: "pop", label: "PoP", num: true, tip: "Probability that the option is worth more than you paid at the horizon (after exit costs)." },
  { key: "kelly", label: "Kelly", num: true, tip: "Full-Kelly fraction of equity that maximises E[log wealth] on the payoff distribution. Live sizing uses a fraction κ of this." },
  { key: "edge_score", label: "Edge", num: true, tip: "Composite ranking score combining EV, Kelly, liquidity and vol-cheapness. Higher is better." },
  { key: "volume", label: "Vol / OI", num: true },
];

function sortValue(c: ScanCandidate, k: SortKey): number | string {
  if (k === "contract") return c.contract;
  if (k === "type") return c.type;
  if (k === "iv_rank") return c.iv_rank ?? -1;
  return c[k];
}

const CandidateRow = memo(function CandidateRow({ c, expanded, onToggle, onOpen }: { c: ScanCandidate; expanded: boolean; onToggle: (k: string) => void; onOpen: (c: ScanCandidate) => void }) {
  return (
    <Fragment>
      <tr className={`clickable${expanded ? " selected" : ""}`} onClick={() => onToggle(c.contract)}>
        <td className="sym">
          <button
            className="link-btn"
            style={{ color: "var(--text)", fontWeight: 600 }}
            onClick={(e) => {
              e.stopPropagation();
              onOpen(c);
            }}
            title="Open on the symbol page with this contract selected"
          >
            {describeOcc(c.contract)}
          </button>
        </td>
        <td className={c.type === "call" ? "up" : "down"}>{c.type.toUpperCase()}</td>
        <td className="num">{fmtPrice(c.strike)}</td>
        <td className="num">
          {fmtExpiry(c.expiration)} <span className="faint">{c.dte}d</span>
        </td>
        <td className="num">{fmtPrice(c.mid)}</td>
        <td className={`num ${c.spread_pct > 0.1 ? "warn-text" : ""}`}>{fmtPct(c.spread_pct, { decimals: 1 })}</td>
        <td className="num">{fmtPct(c.iv, { decimals: 1 })}</td>
        <td className="num">{c.iv_rank === null ? "—" : fmtPct(c.iv_rank, { decimals: 0 })}</td>
        <td className="num">{fmtNum(c.delta, 2)}</td>
        <td className={`num ${signClass(c.ev_pct)}`}>{fmtPct(c.ev_pct, { signed: true, decimals: 1 })}</td>
        <td className="num">{fmtPct(c.pop, { decimals: 0 })}</td>
        <td className="num">{fmtPct(c.kelly, { decimals: 1 })}</td>
        <td className="num" style={{ fontWeight: 600 }}>
          {fmtNum(c.edge_score, 2)}
        </td>
        <td className="num">
          {fmtCompact(c.volume)} <span className="faint">/ {fmtCompact(c.open_interest)}</span>
        </td>
      </tr>
      {expanded && (
        <tr className="expand">
          <td colSpan={COLS.length}>
            <div className="grid-2" style={{ gap: 24, padding: "6px 0" }}>
              <div>
                <div className="label" style={{ marginBottom: 4 }}>
                  Why this contract
                </div>
                {c.reasons.length ? (
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {c.reasons.map((r, i) => (
                      <li key={i} className="small">
                        {r}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="faint small">No reasons provided.</div>
                )}
              </div>
              <dl className="kv">
                <dt>Spot / breakeven</dt>
                <dd>
                  {fmtUsd(c.spot)} / {fmtUsd(c.breakeven)}
                </dd>
                <dt>Bid × ask</dt>
                <dd>
                  {fmtPrice(c.bid)} × {fmtPrice(c.ask)}
                </dd>
                <dt>Forecast return (horizon)</dt>
                <dd className={signClass(c.forecast_return)}>{fmtPct(c.forecast_return, { signed: true })}</dd>
                <dt>Forecast vol / IV÷RV</dt>
                <dd>
                  {fmtPct(c.forecast_vol, { decimals: 1 })} / {c.iv_rv_ratio === null ? "—" : fmtNum(c.iv_rv_ratio, 2)}
                </dd>
                <dt>Theta (per day)</dt>
                <dd>{fmtUsd(c.theta * 100)} / contract</dd>
                <dt>Modelled entry fill</dt>
                <dd>{c.entry_price !== undefined ? fmtUsd(c.entry_price) : "—"}</dd>
                <dt>Premium (1 contract)</dt>
                <dd>{fmtUsd((c.entry_price ?? c.ask) * 100)}</dd>
              </dl>
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn btn-sm" onClick={() => onOpen(c)}>
                Open chart &amp; ticket <Icon name="chevron-right" size={12} />
              </button>
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
});

export default function Scanner() {
  const navigate = useNavigate();
  const schema = useAsync((signal) => api.scannerParams({ signal }), []);
  const last = useAsync((signal) => api.scanner({ signal }), []);
  const [result, setResult] = useState<ScanResult | null>(null);
  const [values, setValues] = useState<ParamValues>({});
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "edge_score", dir: -1 });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<"all" | "call" | "put">("all");
  const [symFilter, setSymFilter] = useState("");
  const [showParams, setShowParams] = useState(true);
  const [fresh, setFresh] = useState(false);

  const defaults = useMemo(() => defaultsOf(schema.data ?? []), [schema.data]);

  useEffect(() => {
    if (last.data) setResult((r) => r ?? last.data ?? null);
  }, [last.data]);

  // initialise form: defaults overlaid with the last run's params
  useEffect(() => {
    if (!schema.data) return;
    setValues((v) => (Object.keys(v).length ? v : { ...defaults, ...pick(last.data?.params ?? {}, defaults) }));
  }, [schema.data, defaults, last.data]);

  useChannel<ScanResult>(channels.scanner, (r) => {
    setResult(r);
    setFresh(true);
    window.setTimeout(() => setFresh(false), 1500);
  });

  const onChange = useCallback((name: string, v: unknown) => setValues((s) => ({ ...s, [name]: v })), []);
  const onToggle = useCallback((k: string) => setExpanded((cur) => (cur === k ? null : k)), []);
  const onOpen = useCallback(
    (c: ScanCandidate) => navigate(`/symbol/${encodeURIComponent(c.underlying)}?contract=${encodeURIComponent(c.contract)}`),
    [navigate],
  );

  const run = async () => {
    setRunning(true);
    setRunError(null);
    try {
      const r = await api.runScanner(values);
      setResult(r);
      toast.success(`Scan complete: ${r.candidates.length} candidates in ${fmtNum(r.duration_ms / 1000, 1)} s`);
    } catch (e) {
      setRunError(errorMessage(e));
    } finally {
      setRunning(false);
    }
  };

  const rows = useMemo(() => {
    const list = (result?.candidates ?? []).filter(
      (c) => (typeFilter === "all" || c.type === typeFilter) && (!symFilter || c.underlying.toUpperCase().includes(symFilter.toUpperCase())),
    );
    const { key, dir } = sort;
    return [...list].sort((a, b) => {
      const x = sortValue(a, key);
      const y = sortValue(b, key);
      if (typeof x === "string" || typeof y === "string") return String(x).localeCompare(String(y)) * dir;
      return (x - y) * dir;
    });
  }, [result, sort, typeFilter, symFilter]);

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Options scanner</h1>
          <div className="sub">Ranks long calls and puts by physical-measure expected value, Kelly fraction and liquidity.</div>
        </div>
        <div className="row">
          {result && (
            <span className={`small ${fresh ? "up" : "muted"}`}>
              Last scan {fmtAgo(result.t)} · {fmtNum(result.duration_ms, 0)} ms
            </span>
          )}
          <button className="btn btn-primary" onClick={() => void run()} disabled={running || !schema.data}>
            {running ? <span className="spinner" /> : <Icon name="play" size={12} />}
            Run scan
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div className="card-header">
          <button className="link-btn row" style={{ color: "var(--text)" }} onClick={() => setShowParams((v) => !v)} aria-expanded={showParams}>
            <Icon name={showParams ? "chevron-down" : "chevron-right"} size={14} />
            <span className="card-title">Parameters</span>
          </button>
          <span className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={() => setValues(defaults)} disabled={!schema.data}>
            Reset to defaults
          </button>
        </div>
        {showParams && (
          <div className="card-body">
            {schema.error ? (
              <ErrorBox error={schema.error} onRetry={schema.reload} />
            ) : !schema.data ? (
              <Loading />
            ) : (
              <ParamForm schema={schema.data} values={values} onChange={onChange} baseline={defaults} columns />
            )}
          </div>
        )}
      </div>

      {runError && (
        <div style={{ marginBottom: 16 }}>
          <ErrorBox error={`Scan failed: ${runError}`} onRetry={() => void run()} />
        </div>
      )}
      {result && result.errors.length > 0 && (
        <div className="notice warn" style={{ marginBottom: 16 }}>
          <strong>{result.errors.length} warnings:</strong> {result.errors.slice(0, 5).join(" · ")}
          {result.errors.length > 5 && " …"}
        </div>
      )}

      <div className="feed-toolbar">
        <div className="seg" role="group" aria-label="Type filter">
          {(["all", "call", "put"] as const).map((t) => (
            <button key={t} className={typeFilter === t ? "active" : ""} onClick={() => setTypeFilter(t)}>
              {t === "all" ? "All" : t === "call" ? "Calls" : "Puts"}
            </button>
          ))}
        </div>
        <input className="input input-sm" style={{ width: 160 }} placeholder="Filter underlying" value={symFilter} onChange={(e) => setSymFilter(e.target.value)} aria-label="Filter by underlying" />
        <span className="small muted">{rows.length} candidates</span>
      </div>

      <div className="card">
        {last.loading && !result ? (
          <Loading label="Loading last scan" />
        ) : !result ? (
          last.error ? (
            <ErrorBox error={last.error} onRetry={last.reload} />
          ) : (
            <Empty title="No scan yet">Set parameters and press Run scan.</Empty>
          )
        ) : rows.length === 0 ? (
          <Empty title="No candidates">Nothing passed the filters. Loosen EV/PoP/liquidity thresholds or widen the universe.</Empty>
        ) : (
          <div className="table-wrap" style={{ maxHeight: "70vh", overflowY: "auto" }}>
            <table className="tbl compact">
              <thead>
                <tr>
                  {COLS.map((c) => {
                    const active = sort.key === c.key;
                    return (
                      <th
                        key={c.key}
                        className={`sortable${c.num ? " num" : ""}${active ? " sorted" : ""}`}
                        aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
                      >
                        <button
                          className="th-btn"
                          onClick={() => setSort((s) => (s.key === c.key ? { key: c.key, dir: s.dir === 1 ? -1 : 1 } : { key: c.key, dir: c.num ? -1 : 1 }))}
                        >
                          {c.label}
                          {active ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
                        </button>
                        {c.tip && (
                          <>
                            {" "}
                            <Tip text={c.tip} />
                          </>
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <CandidateRow key={c.contract} c={c} expanded={expanded === c.contract} onToggle={onToggle} onOpen={onOpen} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function pick(src: Record<string, unknown>, keysFrom: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(keysFrom)) if (k in src) out[k] = src[k];
  return out;
}
