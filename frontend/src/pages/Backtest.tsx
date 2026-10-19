import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../lib/api";
import type { BacktestMetrics, BacktestResult } from "../lib/types";
import { etYmd, fmtDate, fmtNum, fmtPct, fmtUsd, signClass } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { EquityCurveChart } from "../components/charts/EquityCurveChart";
import { diffParams, ParamForm, type ParamValues } from "../components/ui/ParamForm";
import { NumberInput } from "../components/ui/ParamForm";
import { Empty, ErrorBox, Loading, Switch, Tip } from "../components/ui/States";
import { Icon } from "../components/ui/Icon";

const METRICS: { key: keyof BacktestMetrics; label: string; fmt: (v: number) => string; tip: string; signed?: boolean }[] = [
  { key: "cagr", label: "CAGR", fmt: (v) => fmtPct(v, { signed: true }), tip: "Compound annual growth rate of the equity curve.", signed: true },
  { key: "sharpe", label: "Sharpe", fmt: (v) => fmtNum(v, 2), tip: "Annualised mean daily return ÷ standard deviation (√252 scaling, rf = 0).", signed: true },
  { key: "sortino", label: "Sortino", fmt: (v) => fmtNum(v, 2), tip: "Like Sharpe but divides by downside deviation only.", signed: true },
  { key: "max_drawdown", label: "Max drawdown", fmt: (v) => fmtPct(-Math.abs(v)), tip: "Largest peak-to-trough fall of the equity curve." },
  { key: "calmar", label: "Calmar", fmt: (v) => fmtNum(v, 2), tip: "CAGR ÷ max drawdown.", signed: true },
  { key: "win_rate", label: "Win rate", fmt: (v) => fmtPct(v, { decimals: 1 }), tip: "Share of closed trades with positive P&L." },
  { key: "profit_factor", label: "Profit factor", fmt: (v) => fmtNum(v, 2), tip: "Gross profit ÷ gross loss. Above 1 means winners outweigh losers." },
  { key: "trades", label: "Trades", fmt: (v) => fmtNum(v, 0), tip: "Number of closed round-trip trades." },
  { key: "exposure", label: "Exposure", fmt: (v) => fmtPct(v, { decimals: 0 }), tip: "Share of time with at least one open position." },
  {
    key: "psr",
    label: "PSR",
    fmt: (v) => fmtPct(v, { decimals: 1 }),
    tip: "Probabilistic Sharpe Ratio (Bailey & López de Prado 2012): the probability that the TRUE Sharpe is above 0, given the sample length, skewness and fat tails of the returns. PSR = N((SR − 0)·√(n−1) / √(1 − γ3·SR + (γ4−1)/4·SR²)). Above ~95% is strong evidence of skill.",
  },
  {
    key: "dsr",
    label: "DSR",
    fmt: (v) => fmtPct(v, { decimals: 1 }),
    tip: "Deflated Sharpe Ratio: PSR measured against the best Sharpe you would expect by luck after trying N parameter sets (N = backtests you have run for this strategy). It corrects for overfitting by trial-and-error. Treat < 50% as 'probably noise'.",
  },
];

const yearAgo = () => {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 1);
  return d.toISOString().slice(0, 10);
};

export default function Backtest() {
  const strategies = useAsync((signal) => api.strategies({ signal }), []);
  const [strategyId, setStrategyId] = useState("");
  const [symbols, setSymbols] = useState("SPY");
  const [start, setStart] = useState(yearAgo);
  const [end, setEnd] = useState(() => etYmd(Date.now()));
  const [equity, setEquity] = useState(10_000);
  const [ivRv, setIvRv] = useState(1.1);
  const [override, setOverride] = useState(false);
  const [params, setParams] = useState<ParamValues>({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);

  const strat = useMemo(() => strategies.data?.find((s) => s.id === strategyId) ?? null, [strategies.data, strategyId]);

  useEffect(() => {
    if (!strategyId && strategies.data?.length) setStrategyId(strategies.data[0].id);
  }, [strategies.data, strategyId]);
  useEffect(() => {
    if (strat) setParams(strat.params);
  }, [strat]);

  const onParam = useCallback((n: string, v: unknown) => setParams((p) => ({ ...p, [n]: v })), []);
  const symList = symbols
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const valid = !!strategyId && symList.length > 0 && start < end && equity > 0;

  const run = async () => {
    if (!valid) return;
    setRunning(true);
    setError(null);
    try {
      const r = await api.backtest({
        strategy_id: strategyId,
        symbols: symList,
        start,
        end,
        initial_equity: equity,
        iv_rv: strat?.asset_class === "option" ? ivRv : undefined,
        params: override && strat ? diffParams(params, strat.params) : undefined,
      });
      setResult(r);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setRunning(false);
    }
  };

  const m = result?.metrics;
  const totalPnl = result?.trades.reduce((s, t) => s + (t.pnl ?? 0), 0) ?? 0;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Backtest</h1>
          <div className="sub">Event-driven bar backtest using the same strategy code as the auto-trader. Fills at next bar open plus slippage and commission.</div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div className="card-body">
          {strategies.error ? (
            <ErrorBox error={strategies.error} onRetry={strategies.reload} />
          ) : !strategies.data ? (
            <Loading />
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void run();
              }}
            >
              <div className="form-grid">
                <label className="field">
                  <span className="label">Strategy</span>
                  <select className="select" value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                    {strategies.data.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="label">Symbols</span>
                  <input className="input" value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="SPY, QQQ, AAPL" />
                </label>
                <label className="field">
                  <span className="label">Start</span>
                  <input className="input" type="date" value={start} onChange={(e) => setStart(e.target.value)} max={end} />
                </label>
                <label className="field">
                  <span className="label">End</span>
                  <input className="input" type="date" value={end} onChange={(e) => setEnd(e.target.value)} min={start} />
                </label>
                <label className="field">
                  <span className="label">Initial equity ($)</span>
                  <NumberInput value={equity} onChange={setEquity} min={100} className="input input-num" ariaLabel="Initial equity" />
                </label>
                {strat?.asset_class === "option" && (
                  <label className="field" title="Synthetic option pricing: implied vol = forecast realised vol × this ratio. Index options typically trade around 1.1–1.2 (the variance risk premium).">
                    <span className="label">Assumed IV / RV</span>
                    <NumberInput value={ivRv} onChange={setIvRv} min={0.5} max={2} step={0.05} className="input input-num" ariaLabel="Assumed implied to realised volatility ratio" />
                  </label>
                )}
              </div>
              {strat && <div className="small muted" style={{ marginTop: 12 }}>{strat.description}</div>}
              <div className="row" style={{ marginTop: 16 }}>
                <Switch checked={override} onChange={setOverride} label="Override strategy parameters" disabled={!strat?.schema.length} />
                <span className="spacer" />
                {start >= end && <span className="small warn-text">Start must be before end</span>}
                <button className="btn btn-primary" type="submit" disabled={!valid || running}>
                  {running ? <span className="spinner" /> : <Icon name="play" size={12} />}
                  {running ? "Running…" : "Run backtest"}
                </button>
              </div>
              {override && strat && (
                <div style={{ marginTop: 16 }}>
                  <ParamForm schema={strat.schema} values={params} onChange={onParam} baseline={strat.params} columns />
                </div>
              )}
            </form>
          )}
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: 16 }}>
          <ErrorBox error={`Backtest failed: ${error}`} onRetry={() => void run()} />
        </div>
      )}

      {!result && !running && !error && <Empty title="No results yet">Pick a strategy, symbols and a date range, then run.</Empty>}
      {running && !result && <Loading label="Running backtest — large ranges can take a while" />}

      {result && m && (
        <div className="stack-lg">
          <section>
            <div className="row" style={{ marginBottom: 8 }}>
              <div className="section-title" style={{ margin: 0 }}>
                Equity vs benchmark
              </div>
              <span className="row small muted" style={{ gap: 12 }}>
                <span className="row" style={{ gap: 4 }}>
                  <span className="kind-swatch" style={{ background: "var(--accent)" }} /> Strategy
                </span>
                <span className="row" style={{ gap: 4 }}>
                  <span className="kind-swatch" style={{ background: "var(--text-3)" }} /> Benchmark
                </span>
              </span>
              <span className="spacer" />
              <span className={`small num ${signClass(totalPnl)}`}>Net P&amp;L {fmtUsd(totalPnl, { signed: true })}</span>
            </div>
            <div className="card" style={{ padding: 8 }}>
              {result.equity.length ? <EquityCurveChart equity={result.equity} benchmark={result.benchmark} height={320} /> : <Empty>No equity points returned.</Empty>}
            </div>
          </section>

          <div className="stats">
            {METRICS.map((x) => {
              const v = m[x.key];
              return (
                <div key={x.key} className="stat">
                  <div className="label">
                    {x.label} <Tip text={x.tip} />
                  </div>
                  <div className={`value ${x.signed ? signClass(v) : ""}`}>{Number.isFinite(v) ? x.fmt(v) : "—"}</div>
                </div>
              );
            })}
          </div>

          {result.notes.length > 0 && (
            <div className="notice">
              <strong style={{ color: "var(--text)" }}>Notes</strong>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {result.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </div>
          )}

          <section>
            <div className="section-title">Trades ({result.trades.length})</div>
            <div className="card">
              {result.trades.length === 0 ? (
                <Empty>No trades were taken.</Empty>
              ) : (
                <div className="table-wrap" style={{ maxHeight: 480, overflowY: "auto" }}>
                  <table className="tbl compact">
                    <thead>
                      <tr>
                        <th>Symbol</th>
                        <th>Dir</th>
                        <th>Entry</th>
                        <th className="num">Entry px</th>
                        <th>Exit</th>
                        <th className="num">Exit px</th>
                        <th className="num">Qty</th>
                        <th className="num">P&amp;L</th>
                        <th className="num">P&amp;L %</th>
                        <th>Exit reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.map((t) => (
                        <tr key={t.id}>
                          <td className="sym">{t.symbol}</td>
                          <td className={t.direction > 0 ? "up" : "down"}>{t.direction > 0 ? "LONG" : "SHORT"}</td>
                          <td className="num">{fmtDate(t.entry_t)}</td>
                          <td className="num">{fmtUsd(t.entry_price)}</td>
                          <td className="num">{t.exit_t ? fmtDate(t.exit_t) : "open"}</td>
                          <td className="num">{fmtUsd(t.exit_price)}</td>
                          <td className="num">{fmtNum(t.qty, Number.isInteger(t.qty) ? 0 : 3)}</td>
                          <td className={`num ${signClass(t.pnl)}`}>{fmtUsd(t.pnl, { signed: true })}</td>
                          <td className={`num ${signClass(t.pnl_pct)}`}>{fmtPct(t.pnl_pct, { signed: true, decimals: 1 })}</td>
                          <td className="small muted">{t.reason_exit ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
