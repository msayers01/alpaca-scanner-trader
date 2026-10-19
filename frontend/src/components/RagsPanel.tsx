// Rags to Riches strategy card extras: goal progress (log scale) and a Monte Carlo odds estimator.
import { useEffect, useState } from "react";
import { api, errorMessage } from "../lib/api";
import type { GoalOdds, StrategyInfo } from "../lib/types";
import { fmtPct, fmtUsd } from "../lib/format";
import { useAccountStore } from "../store/account";
import { Tip } from "./ui/States";

function pct(v: number | null | undefined, d = 0) {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : fmtPct(v, { decimals: d });
}

function GoalProgress({ s }: { s: StrategyInfo }) {
  const equity = useAccountStore((st) => st.account?.equity ?? null);
  const goal = Number(s.params.goal_equity ?? 0);
  const floor = Number(s.params.floor_equity ?? 0);
  const start = s.runtime?.start_equity ?? null;
  const halted = s.runtime?.halted ?? null;
  // log scale: equal distance for each doubling, which is how compounding progresses
  const progress =
    equity && start && goal > start && equity > 0 ? Math.max(0, Math.min(1, Math.log(equity / start) / Math.log(goal / start))) : null;
  return (
    <div className="rags-progress">
      <div className="row small" style={{ gap: 12 }}>
        <span className="muted">Start {start ? fmtUsd(start) : "—"}</span>
        <span className="spacer" />
        <span>
          Now <strong>{equity !== null ? fmtUsd(equity) : "—"}</strong>
        </span>
        <span className="spacer" />
        <span className="muted">
          Floor {fmtUsd(floor)} · Goal <strong style={{ color: "var(--text)" }}>{fmtUsd(goal)}</strong>
        </span>
      </div>
      <div className="rags-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress === null ? undefined : Math.round(progress * 100)}>
        <div className="fill" style={{ width: `${(progress ?? 0) * 100}%` }} />
      </div>
      <div className="xsmall faint">
        {halted
          ? `Stopped: ${halted}`
          : progress === null
            ? "Progress appears once the strategy is switched on (start equity is recorded then)."
            : `${pct(progress)} of the way on a log scale (each doubling counts the same)${
                equity && goal > equity ? ` · needs ×${(goal / equity).toFixed(1)} more` : ""
              }`}
      </div>
    </div>
  );
}

function Odds({ s }: { s: StrategyInfo }) {
  const equity = useAccountStore((st) => st.account?.equity ?? null);
  const eff = s.extra?.effective;
  const goal = Number(s.params.goal_equity ?? 0);
  const floor = Number(s.params.floor_equity ?? 0);
  const [edgePp, setEdgePp] = useState(5);
  const [edgeSd, setEdgeSd] = useState(4);
  const [days, setDays] = useState(250);
  const [useRealized, setUseRealized] = useState(false);
  const [res, setRes] = useState<GoalOdds | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const realized = s.stats.trades >= 20 && s.stats.win_rate !== null ? s.stats.win_rate : null;
  const start = equity && equity > floor && equity < goal ? equity : Math.max(floor * 2, 50);

  useEffect(() => {
    if (!eff || !(goal > start && start > floor && floor > 0)) {
      setRes(null);
      return;
    }
    const ctrl = new AbortController();
    const t = window.setTimeout(() => {
      api
        .goalOdds(
          {
            start,
            goal,
            floor,
            position_frac: eff.position_pct,
            stop_frac: eff.stop_pct,
            reward_r: eff.reward_r,
            edge_pp: edgePp / 100,
            edge_sd: edgeSd / 100,
            win_rate: useRealized && realized !== null ? realized : undefined,
            trades_per_day: eff.max_trades_per_day * 0.6, // caps are rarely all used; assume ~60 %
            max_days: days,
          },
          { signal: ctrl.signal },
        )
        .then((r) => {
          setRes(r);
          setErr(null);
        })
        .catch((e) => !ctrl.signal.aborted && setErr(errorMessage(e)));
    }, 350);
    return () => {
      window.clearTimeout(t);
      ctrl.abort();
    };
  }, [eff, goal, floor, start, edgePp, edgeSd, days, useRealized, realized]);

  if (!eff) return null;
  const overKelly = res?.kelly_ratio != null && res.kelly_ratio > 1.5;
  const noEdge = res !== null && res.win_rate <= res.breakeven_win_rate;
  return (
    <div className="rags-odds">
      <div className="row" style={{ gap: 6, marginBottom: 8 }}>
        <strong>Odds estimator</strong>
        <Tip text="Monte Carlo of 20,000 simulated paths using this strategy's current size, stop and target. Each trade either hits the target (R × stop) or the stop (with slippage and costs). With no edge, an R-multiple target is hit only 1/(1+R) of the time; your edge is added on top, and its uncertainty is modelled per path. Not a promise — real edges are unknown and change." />
      </div>
      <div className="rags-inputs">
        <label className="field">
          <span className="label">
            Edge over chance: {edgePp > 0 ? "+" : ""}
            {edgePp} pts
          </span>
          <input type="range" min={-5} max={15} step={1} value={edgePp} onChange={(e) => setEdgePp(Number(e.target.value))} disabled={useRealized} />
        </label>
        <label className="field">
          <span className="label">Edge uncertainty: ±{edgeSd} pts</span>
          <input type="range" min={0} max={10} step={1} value={edgeSd} onChange={(e) => setEdgeSd(Number(e.target.value))} />
        </label>
        <label className="field">
          <span className="label">Time limit: {days} trading days</span>
          <input type="range" min={20} max={750} step={10} value={days} onChange={(e) => setDays(Number(e.target.value))} />
        </label>
      </div>
      {realized !== null && (
        <label className="checkbox small" style={{ marginTop: 4 }}>
          <input type="checkbox" checked={useRealized} onChange={(e) => setUseRealized(e.target.checked)} /> Use my realised win rate ({pct(realized, 1)} over {s.stats.trades} trades)
        </label>
      )}
      {err && <div className="small down">{err}</div>}
      {res && (
        <>
          <div className="rags-result">
            <div>
              <div className="big up">{pct(res.p_goal, 1)}</div>
              <div className="xsmall muted">reach {fmtUsd(goal, { decimals: 0 })} first</div>
            </div>
            <div>
              <div className="big down">{pct(res.p_floor, 1)}</div>
              <div className="xsmall muted">hit the {fmtUsd(floor, { decimals: 0 })} floor</div>
            </div>
            <div>
              <div className="big">{pct(res.p_unresolved, 1)}</div>
              <div className="xsmall muted">neither within {days} days</div>
            </div>
            <div>
              <div className="big">{res.median_days_to_goal ? `${Math.round(res.median_days_to_goal)}d` : "—"}</div>
              <div className="xsmall muted">median time when it works</div>
            </div>
          </div>
          <div className="xsmall muted" style={{ lineHeight: 1.6 }}>
            Starting from {fmtUsd(start)}. Win rate {pct(res.win_rate, 1)} vs {pct(res.baseline_win_rate, 1)} by pure chance and{" "}
            {pct(res.breakeven_win_rate, 1)} needed to break even after costs and stop slippage. Per trade: {fmtPct(res.win_multiplier - 1, { decimals: 1, signed: true })} on a win,{" "}
            {fmtPct(res.loss_multiplier - 1, { decimals: 1, signed: true })} on a loss. {pct(res.p_negative_edge)} of simulated paths had no real edge.
          </div>
          {noEdge && (
            <div className="callout danger small" style={{ marginTop: 8 }}>
              At this win rate the strategy loses money on average — compounding then works against you and most paths end at the floor.
            </div>
          )}
          {overKelly && !noEdge && (
            <div className="callout small" style={{ marginTop: 8 }}>
              Position size is {res.kelly_ratio?.toFixed(1)}× the growth-optimal (Kelly) size for this edge — more size adds variance faster than growth.
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function RagsPanel({ s }: { s: StrategyInfo }) {
  const eff = s.extra?.effective;
  return (
    <div className="rags-panel">
      <div className="callout danger small">
        <strong>High risk.</strong> This mode deliberately risks most of the balance on each trade to try to grow a tiny account fast. Turning $50 into thousands needs a long run of
        wins; most attempts end at the floor. Run it in SIM or paper first and only fund it with money you can afford to lose entirely.
      </div>
      <GoalProgress s={s} />
      {eff && (
        <div className="xsmall muted" style={{ margin: "6px 0 10px" }}>
          Active settings ({String(s.params.aggressiveness)}): {fmtPct(eff.position_pct, { decimals: 0 })} of equity per share trade,{" "}
          {fmtPct(eff.option_pct, { decimals: 0 })} per option trade, stop {fmtPct(eff.stop_pct, { decimals: 1 })}, target {eff.reward_r}R, up to {eff.max_trades_per_day} trades/day
          ≥ {eff.min_minutes_between} min apart, stop for the day at −{fmtPct(eff.daily_stop_pct, { decimals: 0 })} or {eff.max_consecutive_losses} losses in a row.
        </div>
      )}
      <Odds s={s} />
    </div>
  );
}
