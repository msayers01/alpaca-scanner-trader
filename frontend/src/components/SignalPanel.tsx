import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { SignalRow } from "../lib/types";
import { channels } from "../lib/ws";
import { fmtAgo, fmtNum, fmtPct, signClass } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { useChannel } from "../hooks/useChannel";
import { Empty, ErrorBox, Loading, ScoreBar, Tip } from "./ui/States";

export function SignalPanel({ symbol }: { symbol: string }) {
  const sym = symbol.toUpperCase();
  const forecast = useAsync((signal) => api.forecast(sym, { signal }), [sym]);
  const sigs = useAsync((signal) => api.signals(sym, { signal }), [sym]);
  const [live, setLive] = useState<Record<string, SignalRow>>({});
  const lastFcReload = useRef(0);

  useEffect(() => setLive({}), [sym]);

  useChannel<SignalRow>(channels.signals, (s) => {
    if (!s || s.symbol.toUpperCase() !== sym) return;
    setLive((m) => ({ ...m, [s.model]: s }));
    // refresh the ensemble at most every 10 s while signals stream in
    if (Date.now() - lastFcReload.current > 10_000) {
      lastFcReload.current = Date.now();
      forecast.reload();
    }
  });

  const rows = useMemo(() => {
    const m = new Map<string, SignalRow>();
    for (const r of sigs.data ?? []) m.set(r.model, r);
    for (const r of Object.values(live)) {
      const prev = m.get(r.model);
      m.set(r.model, { ...r, ic: r.ic ?? prev?.ic ?? null });
    }
    return [...m.values()].sort((a, b) => Math.abs(b.score * b.confidence) - Math.abs(a.score * a.confidence));
  }, [sigs.data, live]);

  const f = forecast.data;
  const contribs = useMemo(() => {
    if (!f) return [];
    const list = f.contributions.map((c) => ({ ...c, contrib: c.weight * c.score }));
    const max = Math.max(1e-9, ...list.map((c) => Math.abs(c.contrib)));
    return list.sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib)).map((c) => ({ ...c, norm: c.contrib / max }));
  }, [f]);

  return (
    <div className="grid-2" style={{ gap: 0 }}>
      <div className="card-body" style={{ borderRight: "1px solid var(--border)" }}>
        <div className="row" style={{ marginBottom: 8 }}>
          <span className="card-title">Ensemble forecast</span>
          <Tip text="IC-weighted blend of all signal models (Grinold: E[r] = IC · σ · z). Weights ∝ max(mean IC, 0)/sd(IC), shrunk toward priors until enough history exists." />
        </div>
        {forecast.error ? (
          <ErrorBox error={forecast.error} onRetry={forecast.reload} />
        ) : !f ? (
          forecast.loading ? (
            <Loading />
          ) : (
            <Empty>No forecast yet.</Empty>
          )
        ) : (
          <>
            <div className="stats" style={{ marginBottom: 12 }}>
              <div className="stat">
                <div className="label">Exp. return</div>
                <div className={`value ${signClass(f.exp_return)}`}>{fmtPct(f.exp_return, { signed: true })}</div>
                <div className="hint">{fmtNum(f.horizon_days, 0)}-day horizon</div>
              </div>
              <div className="stat">
                <div className="label">Prob. up</div>
                <div className={`value ${f.prob_up >= 0.5 ? "up" : "down"}`}>{fmtPct(f.prob_up, { decimals: 1 })}</div>
                <div className="hint">N(E[r]/σ_h)</div>
              </div>
              <div className="stat">
                <div className="label">Vol (ann.)</div>
                <div className="value">{fmtPct(f.vol, { decimals: 1 })}</div>
              </div>
              <div className="stat">
                <div className="label">z</div>
                <div className={`value ${signClass(f.z)}`}>{fmtNum(f.z, 2)}</div>
              </div>
            </div>
            <div className="label" style={{ marginBottom: 6 }}>
              Model contributions (weight × score)
            </div>
            {contribs.length === 0 && <div className="faint small">No contributing models.</div>}
            {contribs.map((c) => (
              <div key={c.model} className="row small" style={{ padding: "3px 0" }}>
                <span className="ellipsis" style={{ width: 120 }}>
                  {c.model}
                </span>
                <ScoreBar value={c.norm} width={120} />
                <span className="num faint" style={{ marginLeft: "auto" }}>
                  w {fmtNum(c.weight, 2)} · s {fmtNum(c.score, 2)}
                </span>
              </div>
            ))}
            <div className="xsmall faint" style={{ marginTop: 8 }}>
              Updated {fmtAgo(f.t)}
            </div>
          </>
        )}
      </div>
      <div className="card-body">
        <div className="row" style={{ marginBottom: 8 }}>
          <span className="card-title">Signals</span>
          <Tip text="Each model outputs a score in [−1, 1] (tanh(z/2), positive = bullish) and a confidence in [0, 1]. IC = realised Spearman rank correlation of the score with forward returns." />
        </div>
        {sigs.error ? (
          <ErrorBox error={sigs.error} onRetry={sigs.reload} />
        ) : sigs.loading && !rows.length ? (
          <Loading />
        ) : rows.length === 0 ? (
          <Empty>No signals for {sym}.</Empty>
        ) : (
          <table className="tbl compact">
            <thead>
              <tr>
                <th>Model</th>
                <th>Score</th>
                <th className="num">Conf.</th>
                <th className="num">Horizon</th>
                <th className="num">IC</th>
                <th className="num">Age</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.model}>
                  <td>{r.model}</td>
                  <td>
                    <div className="row">
                      <ScoreBar value={r.score} width={64} />
                      <span className={`num small ${signClass(r.score)}`}>{fmtNum(r.score, 2)}</span>
                    </div>
                  </td>
                  <td className="num">{fmtPct(r.confidence, { decimals: 0 })}</td>
                  <td className="num">{fmtNum(r.horizon_days, r.horizon_days < 1 ? 1 : 0)}d</td>
                  <td className="num">{r.ic === null || r.ic === undefined ? "—" : fmtNum(r.ic, 3)}</td>
                  <td className="num faint">{fmtAgo(r.t)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
