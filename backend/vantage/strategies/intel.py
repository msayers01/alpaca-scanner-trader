"""Sentiment & Macro Catalyst: long calls/puts when independent intel sources agree AND price confirms.

Design rules (docs/QUANT.md §7.2):
* never trades on one noisy source — it needs ``min_agree`` components (social, mover statements, prediction markets, VIX
  regime) pointing the same way, at least one of them a *primary* (social/statement) catalyst;
* price must already be moving in the trade direction (confirmation) but not be extended (anti-chase);
* regime filters only ever shrink or veto: bullish entries are blocked in a VIX panic and halved when stressed; high
  event risk shrinks size; a loud, one-sided retail crowd blocks entries in its own direction (contrarian guard);
* the LLM gate (on by default) can still veto or shrink; risk checks and execution are unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from vantage.core.types import ExitSignal, ManagedPosition, ParamSpec, TradeIdea
from vantage.strategies.base import Strategy, StrategyContext, default_exit

log = logging.getLogger(__name__)

PRIMARY = ("social_sentiment", "statement_shock")
SECONDARY = ("prediction_tilt", "vix_regime")
WEIGHTS = {"statement_shock": 0.40, "social_sentiment": 0.35, "prediction_tilt": 0.15, "vix_regime": 0.10}


def composite(ctx: StrategyContext, sym: str) -> dict[str, Any]:
    """Confidence-weighted blend of the four intel signals for one symbol plus per-component detail."""
    comps: dict[str, tuple[float, float]] = {}
    for m in WEIGHTS:
        s = ctx.signals.latest.get((sym, m)) if ctx.signals else None
        if s is not None and s.confidence > 0:
            comps[m] = (s.score, s.confidence)
    z = sum(WEIGHTS[m] * c * s for m, (s, c) in comps.items()) / sum(WEIGHTS.values())
    return {"score": z, "components": comps}


class IntelCatalyst(Strategy):
    id = "intel_catalyst"
    name = "Sentiment & Macro Catalyst"
    description = ("Buys a call or put when at least two independent intel sources agree — retail sentiment/attention, "
                   "statements by market movers (Trump, the Fed…), prediction-market repricing, VIX regime — AND price is "
                   "already confirming. Blocks bullish entries in a VIX panic, halves them when stressed, and avoids "
                   "loud one-sided retail crowds. LLM-gated by default.")
    asset_class = "option"
    style = "swing"
    default_llm_gate = True
    backtest_support = "none"
    backtest_note = ("Social posts, mover statements and prediction-market history aren't available to the backtester, "
                     "so this strategy can only be evaluated forward (paper) with the Intel page's IC tracking.")
    params_schema = [
        ParamSpec("composite_min", "float", 0.20, "Min composite score", "Weighted blend of the four intel signals", "Entry",
                  min=0.05, max=1, step=0.01),
        ParamSpec("component_min", "float", 0.15, "Component threshold", "|score x confidence| for a component to count as agreeing",
                  "Entry", min=0.02, max=1, step=0.01),
        ParamSpec("min_agree", "int", 2, "Min agreeing components", "At least one must be social or a statement", "Entry", min=1, max=4, step=1),
        ParamSpec("confirm_move_pct", "float", 0.003, "Price confirmation", "Move since the prior close in the trade direction", "Entry",
                  min=0, max=0.05, step=0.001),
        ParamSpec("max_move_pct", "float", 0.06, "Max extension (anti-chase)", "Skip if price already moved this much", "Entry",
                  min=0.01, max=0.3, step=0.005),
        ParamSpec("block_bull_in_panic", "bool", True, "No bullish entries in a VIX panic", "", "Regime"),
        ParamSpec("stress_size", "float", 0.5, "Size when VIX stressed (bullish)", "Multiplier ≤ 1", "Regime", min=0.1, max=1, step=0.05),
        ParamSpec("event_risk_shrink", "float", 0.6, "Event-risk shrink level", "Above this size is cut by 30 %", "Regime",
                  min=0.1, max=1, step=0.05),
        ParamSpec("ev_min", "float", 0.05, "Min EV on premium", "", "Entry", min=-1, max=5, step=0.01),
        ParamSpec("max_underlyings", "int", 4, "Names per cycle", "", "Entry", min=1, max=30, step=1),
        ParamSpec("horizon_days", "float", 4.0, "Horizon (trading days)", "", "Contract", min=1, max=30, step=1),
        ParamSpec("min_dte", "int", 7, "Min DTE", "", "Contract", min=1, max=120, step=1),
        ParamSpec("max_dte", "int", 35, "Max DTE", "", "Contract", min=2, max=365, step=1),
        ParamSpec("min_delta", "float", 0.35, "Min |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
        ParamSpec("max_delta", "float", 0.65, "Max |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
    ]
    exit_defaults = {"stop_pct": 0.35, "target_pct": 0.8, "trail_activate_pct": 0.4, "trail_pct": 0.25,
                     "time_stop_days": 5, "min_dte_exit": 3, "exit_on_flip": True, "flip_z": 1.0}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        from vantage.strategies.library import (  # local: library imports this module last
            _best_affordable,
            _scan_params,
        )

        if ctx.intel is None or not ctx.intel.enabled or ctx.backtest:
            return []
        p = self.params
        macro = ctx.intel.macro()
        vix = macro.get("vix") or {}
        label = vix.get("label")
        cands: list[tuple[float, str, dict[str, Any]]] = []
        for u in ctx.universe:
            c = composite(ctx, u)
            if abs(c["score"]) >= p["composite_min"]:
                cands.append((abs(c["score"]), u, c))
        cands.sort(reverse=True)
        ideas: list[TradeIdea] = []
        for _, u, c in cands[: p["max_underlyings"]]:
            direction = 1 if c["score"] > 0 else -1
            agree = {m: s * cf for m, (s, cf) in c["components"].items() if s * direction > 0 and abs(s * cf) >= p["component_min"]}
            against = {m: s * cf for m, (s, cf) in c["components"].items() if s * direction < 0 and abs(s * cf) >= 2 * p["component_min"]}
            if len(agree) < p["min_agree"] or not any(m in PRIMARY for m in agree) or against:
                continue
            soc = ctx.signals.latest.get((u, "social_sentiment"))
            if soc is not None and soc.meta.get("crowded") and soc.score * direction > 0:
                continue  # loud one-sided retail crowd in our direction: positioning is stretched
            if direction > 0 and label == "panic" and p["block_bull_in_panic"]:
                continue
            price = ctx.market.price(u)
            bars = await ctx.market.daily_bars(u, 10)
            if not price or len(bars) < 2:
                continue
            prev = bars[-2].c if bars[-1].t >= _today_start(ctx) else bars[-1].c
            move = price / prev - 1.0 if prev else 0.0
            if move * direction < p["confirm_move_pct"] or abs(move) > p["max_move_pct"]:
                continue
            fc = ctx.signals.forecast(u, p["horizon_days"])
            cs = await ctx.scanner.evaluate(u, _scan_params(p), side="calls" if direction > 0 else "puts", forecast=fc)
            cs = [x for x in cs if x["ev_pct"] >= p["ev_min"] and x["kelly"] > 0]
            if not cs:
                continue
            best = _best_affordable(cs, ctx, p["stop_pct"])
            mult, notes = 1.0, []
            if direction > 0 and label == "stressed":
                mult *= p["stress_size"]
                notes.append(f"VIX stressed ({vix.get('vix', 0):.1f}) → size ×{p['stress_size']:g}")
            if macro["event_risk"] >= p["event_risk_shrink"]:
                mult *= 0.7
                notes.append(f"event risk {macro['event_risk']:.2f} → size ×0.7")
            parts = ", ".join(f"{m.replace('_', ' ')} {v:+.2f}" for m, v in sorted(agree.items(), key=lambda kv: -abs(kv[1])))
            rationale = (f"Intel composite {c['score']:+.2f} ({parts}); price {move * 100:+.1f}% confirms; "
                         f"{best['type']} {best['strike']:g} {best['expiration']} EV {best['ev_pct'] * 100:+.1f}%"
                         + (f"; {'; '.join(notes)}" if notes else ""))
            idea = self.option_idea(best, rationale, {"intel": {"composite": round(c["score"], 3),
                                                                  "agree": {k: round(v, 3) for k, v in agree.items()},
                                                                  "move": round(move, 4), "vix": label,
                                                                  "event_risk": macro["event_risk"]}},
                                    self.use_llm_gate, direction, p["horizon_days"])
            idea.size_multiplier = min(1.0, mult)
            ideas.append(idea)
        return ideas

    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None:
        s = ctx.signals.latest.get((pos.underlying, "statement_shock")) if ctx.signals else None
        if s is not None and s.confidence >= 0.4 and s.score * pos.direction <= -0.5:
            return ExitSignal(f"a mover statement now cuts against the position (score {s.score:+.2f})", urgent=True)
        return default_exit(ctx, pos, self.params)


def _today_start(ctx: StrategyContext) -> int:
    from vantage.core import clock

    return clock.to_ms(clock.et_dt(clock.today_et(), clock.time(0, 0))) if ctx.mode != "sim" else ctx.now - 86_400_000
