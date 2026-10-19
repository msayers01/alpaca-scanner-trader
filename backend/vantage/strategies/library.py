"""Preset strategies (docs/QUANT.md §7). Strategies only emit TradeIdeas; they never place orders."""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from vantage.core.types import ExitSignal, ManagedPosition, ParamSpec, TradeIdea
from vantage.llm import prompts
from vantage.quant import indicators, kelly
from vantage.strategies.base import Strategy, StrategyContext, default_exit, dossier

log = logging.getLogger(__name__)

ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "SMH", "XLK", "XLV"]


def _scan_params(p: dict[str, Any], **extra: Any) -> dict[str, Any]:
    keys = ("min_dte", "max_dte", "min_delta", "max_delta", "horizon_days", "max_spread_pct")
    out = {k: p[k] for k in keys if k in p}
    out.update(extra)
    return out


def _best_affordable(cands: list[dict[str, Any]], ctx: StrategyContext, stop_pct: float) -> dict[str, Any] | None:
    """Best-edge contract that the account can actually size to >= 1 contract.

    Mirrors the risk manager's sizing (fractional Kelly, risk budget, position cap) so small accounts get the
    best *affordable* contract (cheaper strikes / shorter tenor) instead of an idea that rounds to zero.
    Falls back to the overall best so the risk manager can log why nothing fit.
    """
    if not cands:
        return None
    eq = float(ctx.account.get("equity") or 0.0)
    r = ctx.risk
    ok = []
    for c in cands:
        premium = c["entry_price"] * 100
        if premium <= 0:
            continue
        by_kelly = c["kelly"] * r.get("kelly_multiplier", 0.25) * eq
        by_risk = eq * r.get("max_risk_per_trade", 0.02) / max(stop_pct, 0.5)
        by_cap = eq * r.get("max_position_pct", 0.1)
        if premium <= min(by_kelly, by_risk, by_cap):
            ok.append(c)
    return max(ok or cands, key=lambda c: c["edge_score"])


def _sig(ctx: StrategyContext, sym: str, model: str) -> float | None:
    s = ctx.signals.latest.get((sym, model)) if ctx.signals else None
    return s.score if s else None


# ============================================================================ options
class OptionsEdge(Strategy):
    id = "options_edge"
    name = "Directional Options Edge"
    description = ("Buys the call or put with the highest physical-measure EV when the ensemble forecast is strong, "
                   "volatility is not expensive vs. our forecast, and the contract is liquid.")
    asset_class = "option"
    style = "swing"
    params_schema = [
        ParamSpec("z_min", "float", 1.0, "Min |forecast z|", "Ensemble strength required", "Entry", min=0, max=5, step=0.1),
        ParamSpec("ev_min", "float", 0.08, "Min EV on premium", "", "Entry", min=0, max=5, step=0.01),
        ParamSpec("pop_min", "float", 0.40, "Min probability of profit", "", "Entry", min=0, max=1, step=0.01),
        ParamSpec("iv_rv_max", "float", 1.35, "Max IV / forecast vol", "Don't buy expensive volatility", "Entry", min=0.5, max=5, step=0.05),
        ParamSpec("horizon_days", "float", 10.0, "Horizon (trading days)", "", "Contract", min=1, max=60, step=1),
        ParamSpec("min_dte", "int", 14, "Min DTE", "", "Contract", min=1, max=365, step=1),
        ParamSpec("max_dte", "int", 60, "Max DTE", "", "Contract", min=2, max=730, step=1),
        ParamSpec("min_delta", "float", 0.30, "Min |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
        ParamSpec("max_delta", "float", 0.60, "Max |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
        ParamSpec("max_underlyings", "int", 8, "Underlyings per cycle", "Strongest forecasts evaluated each cycle",
                  "Entry", min=1, max=100, step=1),
    ]
    exit_defaults = {"stop_pct": 0.40, "target_pct": 0.80, "trail_activate_pct": 0.40, "trail_pct": 0.25,
                     "time_stop_days": 14, "min_dte_exit": 7}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        p = self.params
        h = p["horizon_days"]
        fcs = []
        for u in ctx.universe:
            fc = ctx.signals.forecast(u, h)
            if abs(fc["z"]) >= p["z_min"]:
                fcs.append(fc)
        fcs.sort(key=lambda f: -abs(f["z"]))
        ideas = []
        for fc in fcs[: p["max_underlyings"]]:
            side = "calls" if fc["z"] > 0 else "puts"
            cands = await ctx.scanner.evaluate(fc["symbol"], _scan_params(p), side=side, forecast=fc)
            cands = [c for c in cands if c["ev_pct"] >= p["ev_min"] and c["pop"] >= p["pop_min"] and c["kelly"] > 0
                     and (c["iv_rv_ratio"] is None or c["iv_rv_ratio"] <= p["iv_rv_max"])]
            if not cands:
                continue
            best = _best_affordable(cands, ctx, p["stop_pct"])
            direction = 1 if fc["z"] > 0 else -1
            rationale = (f"Ensemble z {fc['z']:+.2f} → E[r] {fc['exp_return'] * 100:+.2f}%/{h:.0f}d; "
                         f"{best['type']} {best['strike']:g} {best['expiration']} EV {best['ev_pct'] * 100:+.1f}%, "
                         f"PoP {best['pop'] * 100:.0f}%, IV/RV {best['iv_rv_ratio']}")
            ideas.append(self.option_idea(best, rationale, {"forecast": {k: fc[k] for k in ("z", "exp_return", "prob_up", "vol")}},
                                          self.use_llm_gate, direction, h))
        return ideas


class FlowFollow(Strategy):
    id = "flow_follow"
    name = "Smart-Money Flow"
    description = ("Follows large aggressive options flow (sweeps, ask-side premium) when the trend does not disagree, "
                   "expressed with a shorter-dated option. LLM-gated by default.")
    asset_class = "option"
    style = "swing"
    default_llm_gate = True
    params_schema = [
        ParamSpec("flow_min", "float", 0.35, "Min flow score", "options_flow signal score", "Entry", min=0, max=1, step=0.05),
        ParamSpec("flow_conf_min", "float", 0.3, "Min flow confidence", "Grows with premium traded", "Entry", min=0, max=1, step=0.05),
        ParamSpec("min_prints", "int", 3, "Min qualifying prints", "Ignore one-off prints", "Entry", min=1, max=100, step=1),
        ParamSpec("trend_veto", "float", 0.3, "Trend veto", "Skip if trend score opposes by more than this", "Entry", min=0, max=1, step=0.05),
        ParamSpec("ev_min", "float", 0.05, "Min EV on premium", "", "Entry", min=-1, max=5, step=0.01),
        ParamSpec("horizon_days", "float", 5.0, "Horizon (trading days)", "", "Contract", min=1, max=30, step=1),
        ParamSpec("min_dte", "int", 7, "Min DTE", "", "Contract", min=1, max=120, step=1),
        ParamSpec("max_dte", "int", 35, "Max DTE", "", "Contract", min=2, max=365, step=1),
        ParamSpec("min_delta", "float", 0.30, "Min |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
        ParamSpec("max_delta", "float", 0.65, "Max |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
    ]
    exit_defaults = {"stop_pct": 0.35, "target_pct": 1.0, "trail_activate_pct": 0.5, "trail_pct": 0.3,
                     "time_stop_days": 7, "min_dte_exit": 3}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        p = self.params
        ideas = []
        for u in ctx.universe:
            sig = ctx.signals.latest.get((u, "options_flow"))
            if sig is None or abs(sig.score) < p["flow_min"] or sig.confidence < p["flow_conf_min"]:
                continue
            if int(sig.meta.get("n", 0)) < p["min_prints"]:
                continue
            direction = 1 if sig.score > 0 else -1
            trend = _sig(ctx, u, "trend_kalman") or 0.0
            if trend * direction < -p["trend_veto"]:
                continue
            fc = ctx.signals.forecast(u, p["horizon_days"])
            cands = await ctx.scanner.evaluate(u, _scan_params(p), side="calls" if direction > 0 else "puts", forecast=fc)
            cands = [c for c in cands if c["ev_pct"] >= p["ev_min"] and c["kelly"] > 0]
            if not cands:
                continue
            best = _best_affordable(cands, ctx, p["stop_pct"])
            meta = sig.meta
            rationale = (f"Flow {sig.score:+.2f} (bull ${meta.get('bull', 0):,.0f} vs bear ${meta.get('bear', 0):,.0f}, "
                         f"sweeps {meta.get('sweeps_bull', 0)}/{meta.get('sweeps_bear', 0)}); trend {trend:+.2f}; "
                         f"{best['type']} {best['strike']:g} EV {best['ev_pct'] * 100:+.1f}%")
            ideas.append(self.option_idea(best, rationale, {"flow": meta}, self.use_llm_gate, direction,
                                          p["horizon_days"]))
        return ideas


class EarningsDrift(Strategy):
    id = "earnings_drift"
    name = "Post-Earnings Drift"
    description = ("Enters 1–3 sessions after a large, volume-confirmed earnings/news gap in the gap direction "
                   "(post-earnings-announcement drift), using an option dated past the drift window.")
    asset_class = "option"
    style = "swing"
    default_llm_gate = True
    params_schema = [
        ParamSpec("drift_min", "float", 0.35, "Min drift score", "event_drift signal score", "Entry", min=0, max=1, step=0.05),
        ParamSpec("max_age_days", "int", 3, "Max sessions since event", "", "Entry", min=1, max=20, step=1),
        ParamSpec("ev_min", "float", 0.05, "Min EV on premium", "", "Entry", min=-1, max=5, step=0.01),
        ParamSpec("use_equity", "bool", False, "Trade shares instead", "Buy stock (long-only) instead of options", "Entry"),
        ParamSpec("horizon_days", "float", 15.0, "Horizon (trading days)", "", "Contract", min=1, max=60, step=1),
        ParamSpec("min_dte", "int", 30, "Min DTE", "", "Contract", min=1, max=365, step=1),
        ParamSpec("max_dte", "int", 75, "Max DTE", "", "Contract", min=2, max=730, step=1),
        ParamSpec("min_delta", "float", 0.35, "Min |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
        ParamSpec("max_delta", "float", 0.65, "Max |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
    ]
    exit_defaults = {"stop_pct": 0.40, "target_pct": 1.0, "trail_activate_pct": 0.5, "trail_pct": 0.3,
                     "time_stop_days": 28, "min_dte_exit": 10}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        p = self.params
        ideas: list[TradeIdea] = []
        for u in ctx.universe:
            sig = ctx.signals.latest.get((u, "event_drift"))
            if sig is None or abs(sig.score) < p["drift_min"] or sig.meta.get("age_days", 99) > p["max_age_days"]:
                continue
            direction = 1 if sig.score > 0 else -1
            fc = ctx.signals.forecast(u, p["horizon_days"])
            rationale = (f"Event gap z {sig.meta.get('event_z')} with {sig.meta.get('rel_volume')}x volume "
                         f"{sig.meta.get('age_days')} session(s) ago; drift score {sig.score:+.2f}")
            if p["use_equity"]:
                if direction < 0:
                    continue
                price = ctx.market.price(u)
                if not price:
                    continue
                sigma = fc["vol"] * math.sqrt(p["horizon_days"] / 252)
                mu = max(fc["exp_return"], 0.002 * abs(sig.score) * 10)
                ideas.append(TradeIdea(self.id, u, u, "equity", 1, price, edge_score=100 * mu / max(sigma, 1e-3) * 2,
                                       expected_return=mu, win_prob=fc["prob_up"],
                                       kelly_fraction=max(0.0, kelly.kelly_gaussian(mu, sigma)), horizon_days=p["horizon_days"],
                                       rationale=rationale, use_llm_gate=self.use_llm_gate,
                                       signals={"event": sig.meta}, stop_pct=0.08, target_pct=0.2,
                                       time_stop_days=p["time_stop_days"]))
                continue
            cands = await ctx.scanner.evaluate(u, _scan_params(p, avoid_earnings=True), side="calls" if direction > 0 else "puts",
                                               forecast=fc)
            cands = [c for c in cands if c["ev_pct"] >= p["ev_min"] and c["kelly"] > 0]
            if cands:
                best = _best_affordable(cands, ctx, p["stop_pct"])
                ideas.append(self.option_idea(best, rationale, {"event": sig.meta}, self.use_llm_gate,
                                              direction, p["horizon_days"]))
        return ideas


# ============================================================================ equities
class TsmomEquity(Strategy):
    id = "tsmom_equity"
    name = "Vol-Targeted Trend"
    description = ("Long stocks/ETFs where time-series momentum and the Kalman trend agree; position sized to a target "
                   "volatility (fractional shares for small accounts) and exited on trend failure or a 3-ATR trailing stop.")
    asset_class = "equity"
    style = "swing"
    params_schema = [
        ParamSpec("tsmom_min", "float", 0.25, "Min momentum score", "", "Entry", min=0, max=1, step=0.05),
        ParamSpec("trend_min", "float", 0.15, "Min trend score", "", "Entry", min=0, max=1, step=0.05),
        ParamSpec("target_vol", "float", 0.15, "Target position vol", "Annualised volatility budget per position", "Sizing",
                  min=0.02, max=1, step=0.01),
        ParamSpec("atr_mult", "float", 3.0, "Trailing stop (ATR)", "", "Exits", min=1, max=10, step=0.5),
        ParamSpec("horizon_days", "float", 20.0, "Horizon (trading days)", "", "Entry", min=1, max=120, step=1),
    ]
    exit_defaults = {"stop_pct": 0.10, "target_pct": 0.6, "trail_activate_pct": 0.0, "trail_pct": 0.08,
                     "time_stop_days": 90, "exit_on_flip": True, "flip_z": 0.5}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        p = self.params
        ideas = []
        for u in ctx.universe:
            ts, tr = _sig(ctx, u, "tsmom"), _sig(ctx, u, "trend_kalman")
            if ts is None or tr is None or ts < p["tsmom_min"] or tr < p["trend_min"]:
                continue
            price = ctx.market.price(u)
            if not price:
                continue
            fc = ctx.signals.forecast(u, p["horizon_days"])
            sigma_ann = fc["vol"]
            mu_ann = max(fc["mu_annual"], 0.0)
            k_full = kelly.kelly_gaussian(mu_ann, sigma_ann) if sigma_ann > 0 else 0.0
            vt = p["target_vol"] / sigma_ann if sigma_ann > 0 else 0.0
            frac = max(0.0, min(k_full, vt / max(ctx.risk.get("kelly_multiplier", 0.25), 1e-6)))
            bars = await ctx.market.daily_bars(u, 60)
            trail = None
            if len(bars) > 20:
                h = np.array([b.h for b in bars])
                lo = np.array([b.l for b in bars])
                c = np.array([b.c for b in bars])
                atr = float(indicators.atr(h, lo, c, 14)[-1])
                if math.isfinite(atr):
                    trail = min(0.3, p["atr_mult"] * atr / price)
            sigma_h = sigma_ann * math.sqrt(p["horizon_days"] / 252)
            ideas.append(TradeIdea(
                self.id, u, u, "equity", 1, price,
                edge_score=round(100 * fc["exp_return"] / max(sigma_h, 1e-4) + 5 * (ts + tr), 3),
                expected_return=fc["exp_return"], win_prob=fc["prob_up"], kelly_fraction=frac,
                horizon_days=p["horizon_days"],
                rationale=f"TSMOM {ts:+.2f}, Kalman trend {tr:+.2f}; vol {sigma_ann * 100:.0f}% → vol-target weight "
                          f"{vt * 100:.0f}% of equity",
                stop_pct=p["stop_pct"], target_pct=p["target_pct"], trail_pct=trail or p["trail_pct"],
                trail_activate_pct=0.0, time_stop_days=p["time_stop_days"],
                signals={"tsmom": ts, "trend": tr}, use_llm_gate=self.use_llm_gate,
            ))
        return ideas

    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None:
        ts = _sig(ctx, pos.underlying, "tsmom")
        if ts is not None and ts < -0.1:
            return ExitSignal(f"momentum turned negative ({ts:+.2f})")
        return default_exit(ctx, pos, self.params)


class MeanrevETF(Strategy):
    id = "meanrev_etf"
    name = "ETF Pullback Reversion"
    description = ("Buys broad/sector ETFs in long-term uptrends after short-term oversold pullbacks (RSI(2), IBS); "
                   "exits on the bounce above the 5-day average or after 5 days.")
    asset_class = "equity"
    style = "swing"
    params_schema = [
        ParamSpec("symbols", "str", ",".join(ETFS), "ETFs", "Comma-separated list", "Entry"),
        ParamSpec("rsi_max", "float", 10.0, "RSI(2) below", "", "Entry", min=1, max=50, step=1),
        ParamSpec("ibs_max", "float", 0.2, "or IBS below", "", "Entry", min=0.01, max=0.5, step=0.01),
        ParamSpec("win_prob", "float", 0.64, "Prior win rate", "Historical hit-rate of this setup (for Kelly)", "Sizing",
                  min=0.4, max=0.9, step=0.01),
        ParamSpec("payoff", "float", 1.0, "Prior win/loss ratio", "", "Sizing", min=0.2, max=5, step=0.05),
    ]
    exit_defaults = {"stop_pct": 0.06, "target_pct": 0.08, "trail_activate_pct": 10.0, "trail_pct": 0.5,
                     "time_stop_days": 7, "exit_on_flip": False}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        p = self.params
        syms = [s.strip().upper() for s in str(p["symbols"]).split(",") if s.strip()]
        ideas = []
        for u in syms:
            bars = await ctx.market.daily_bars(u, 260)
            if len(bars) < 210:
                continue
            c = np.array([b.c for b in bars])
            h = np.array([b.h for b in bars])
            lo = np.array([b.l for b in bars])
            price = ctx.market.price(u) or float(c[-1])
            c[-1] = price
            sma200 = float(np.mean(c[-200:]))
            rsi2 = float(indicators.rsi(c, 2)[-1])
            ibs = float(indicators.ibs(h[-1:], lo[-1:], c[-1:])[-1])
            if price <= sma200 or not (rsi2 < p["rsi_max"] or ibs < p["ibs_max"]):
                continue
            kf = max(0.0, kelly.kelly_binary(p["win_prob"], p["payoff"], 1.0))
            ideas.append(TradeIdea(
                self.id, u, u, "equity", 1, price,
                edge_score=round(10 * (p["win_prob"] * p["payoff"] - (1 - p["win_prob"])) + (10 - min(rsi2, 10)) / 2, 3),
                expected_return=0.01, win_prob=p["win_prob"], kelly_fraction=kf, horizon_days=4,
                rationale=f"{u} above SMA200 ({sma200:.2f}); RSI(2) {rsi2:.1f}, IBS {ibs:.2f} → oversold in uptrend",
                stop_pct=p["stop_pct"], target_pct=p["target_pct"], time_stop_days=p["time_stop_days"],
                signals={"rsi2": rsi2, "ibs": ibs}, use_llm_gate=self.use_llm_gate,
            ))
        return ideas

    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None:
        bars = await ctx.market.daily_bars(pos.underlying, 10)
        if len(bars) >= 6:
            c = np.array([b.c for b in bars])
            c[-1] = pos.current_price
            if pos.current_price > float(np.mean(c[-5:])) and pos.unrealized_plpc > 0:
                return ExitSignal("closed above 5-day average (reversion complete)")
        return default_exit(ctx, pos, self.params)


class OrbIntraday(Strategy):
    id = "orb_intraday"
    name = "Opening-Range Breakout"
    description = ("Intraday: trades the break of the first 5-minute range in the direction of the first candle on "
                   "names with high relative volume; stop at the opposite side of the range, flat by the close. "
                   "Requires day-trading capacity.")
    asset_class = "equity"
    style = "intraday"
    params_schema = [
        ParamSpec("orb_min", "float", 0.45, "Min ORB score", "", "Entry", min=0, max=1, step=0.05),
        ParamSpec("rel_volume_min", "float", 1.5, "Min relative volume", "", "Entry", min=0.5, max=20, step=0.1),
        ParamSpec("latest_entry_min", "int", 120, "Latest entry (min after open)", "", "Entry", min=5, max=390, step=5),
        ParamSpec("flat_before_close_min", "int", 10, "Flatten N min before close", "", "Exits", min=1, max=120, step=1),
    ]
    exit_defaults = {"stop_pct": 0.02, "target_pct": 0.06, "trail_activate_pct": 0.02, "trail_pct": 0.015,
                     "time_stop_days": 0.3, "exit_on_flip": False}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        p = self.params
        ms = ctx.market_state
        if ctx.mode != "sim" and (ms.minutes_since_open is None or ms.minutes_since_open > p["latest_entry_min"]):
            return []
        ideas = []
        for u in ctx.universe:
            sig = ctx.signals.latest.get((u, "orb"))
            if sig is None or sig.score < p["orb_min"] or sig.meta.get("rel_volume", 0) < p["rel_volume_min"]:
                continue  # long-only: bullish breaks
            price = ctx.market.price(u)
            if not price:
                continue
            or_low = sig.meta.get("or_low") or price * 0.98
            stop = max(0.003, (price - or_low) / price)
            ideas.append(TradeIdea(
                self.id, u, u, "equity", 1, price, edge_score=round(10 * sig.score * sig.confidence, 3),
                expected_return=2 * stop, win_prob=0.45, kelly_fraction=max(0.0, kelly.kelly_binary(0.45, 2.0, 1.0)),
                horizon_days=0.3, rationale=f"ORB break above {sig.meta.get('or_high')} with {sig.meta.get('rel_volume')}x volume",
                stop_pct=stop, target_pct=3 * stop, trail_pct=p["trail_pct"], trail_activate_pct=stop,
                time_stop_days=p["time_stop_days"], signals={"orb": sig.meta, "intraday": True},
                use_llm_gate=self.use_llm_gate,
            ))
        return ideas

    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None:
        ms = ctx.market_state
        if ms.minutes_to_close is not None and ms.minutes_to_close <= self.params["flat_before_close_min"]:
            return ExitSignal("intraday strategy: flat before the close", urgent=True)
        return default_exit(ctx, pos, self.params)


# ============================================================================ LLM
class LLMAnalyst(Strategy):
    id = "llm_analyst"
    name = "LLM Analyst"
    description = ("Sends a full evidence dossier for the strongest ensemble names to the local LLM; trades only when the "
                   "LLM agrees with the quantitative direction AND the scanner finds a positive-EV contract. Size is "
                   "the deterministic Kelly scaled down by the LLM's conviction.")
    asset_class = "option"
    style = "swing"
    params_schema = [
        ParamSpec("top_n", "int", 4, "Names reviewed per cycle", "", "Entry", min=1, max=20, step=1),
        ParamSpec("z_min", "float", 0.8, "Min |forecast z|", "", "Entry", min=0, max=5, step=0.1),
        ParamSpec("conviction_min", "float", 0.6, "Min LLM conviction", "", "Entry", min=0, max=1, step=0.05),
        ParamSpec("review_minutes", "int", 60, "Re-review interval (min)", "Don't ask about the same name more often", "Entry",
                  min=1, max=1440, step=5),
        ParamSpec("horizon_days", "float", 10.0, "Horizon (trading days)", "", "Contract", min=1, max=60, step=1),
        ParamSpec("min_dte", "int", 14, "Min DTE", "", "Contract", min=1, max=365, step=1),
        ParamSpec("max_dte", "int", 60, "Max DTE", "", "Contract", min=2, max=730, step=1),
        ParamSpec("min_delta", "float", 0.30, "Min |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
        ParamSpec("max_delta", "float", 0.60, "Max |delta|", "", "Contract", min=0.05, max=0.95, step=0.01),
    ]
    exit_defaults = {"stop_pct": 0.40, "target_pct": 0.9, "trail_activate_pct": 0.45, "trail_pct": 0.25,
                     "time_stop_days": 14, "min_dte_exit": 7}

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self._reviewed: dict[str, int] = {}

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        if ctx.llm is None or not ctx.llm.enabled or ctx.backtest:
            return []
        p = self.params
        fcs = [ctx.signals.forecast(u, p["horizon_days"]) for u in ctx.universe]
        fcs = [f for f in fcs if abs(f["z"]) >= p["z_min"]]
        fcs.sort(key=lambda f: -abs(f["z"]))
        ideas = []
        for fc in fcs[: p["top_n"]]:
            u = fc["symbol"]
            if ctx.now - self._reviewed.get(u, 0) < p["review_minutes"] * 60_000:
                continue
            self._reviewed[u] = ctx.now
            res = await ctx.llm.complete_json("analyst", prompts.ANALYST_SYSTEM,
                                              "Dossier (JSON):\n" + _json(dossier(ctx, u)), prompts.ANALYST_SCHEMA,
                                              cache_ttl_s=600)
            if not res.ok or not res.data:
                continue
            act, conv = res.data["action"], float(res.data["conviction"])
            if act == "pass" or conv < p["conviction_min"]:
                continue
            direction = 1 if act == "long_call" else -1
            if direction * fc["z"] <= 0:
                continue  # LLM may only filter the quantitative direction, never reverse it
            cands = await ctx.scanner.evaluate(u, _scan_params(p), side="calls" if direction > 0 else "puts", forecast=fc)
            cands = [c for c in cands if c["ev_pct"] > 0 and c["kelly"] > 0]
            if not cands:
                continue
            best = _best_affordable(cands, ctx, p["stop_pct"])
            idea = self.option_idea(best, f"LLM {act} (conviction {conv:.2f}): {res.data['rationale'][:300]}",
                                    {"llm_analyst": res.data}, False, direction, p["horizon_days"])
            idea.size_multiplier = conv
            idea.signals["llm_latency_ms"] = res.latency_ms
            ideas.append(idea)
        return ideas


def _json(d: Any) -> str:
    from vantage.core.util import dumps, sanitize

    return dumps(sanitize(d)).decode()


REGISTRY: dict[str, type[Strategy]] = {
    s.id: s for s in (OptionsEdge, FlowFollow, EarningsDrift, TsmomEquity, MeanrevETF, OrbIntraday, LLMAnalyst)
}
