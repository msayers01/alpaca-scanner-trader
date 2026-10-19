"""Rags to Riches — high-risk, high-activity small-account day-trading strategy (docs/QUANT.md §7.1).

Purpose: grow a tiny balance ($20-$50+) quickly toward a user-set dollar goal, then stop (and optionally hand
the account to the normal tier preset). It concentrates capital in the single best intraday setup at a time and
compounds: position size is a fixed fraction of *current* equity.

Playbook (intraday, long-only instruments; bearish views via inverse leveraged ETFs or puts):
  * Opening-range breakout (Crabel; Zarattini & Aziz 2023): break of the first N-minute range in the direction
    of the opening drive, above/below VWAP, with relative volume.
  * VWAP trend pullback: VWAP rising, price pulls back to VWAP and resumes (bounce candle).
  * High/low-of-day momentum: new session high/low on a volume surge (>= 2x the 30-minute average).
  * Market-direction filter: QQQ vs its VWAP adds/subtracts confidence (don't fight the tape).
  * Options power play (optional): cheap 0-2 DTE SPY/QQQ calls/puts in the setup direction - convex payoff,
    premium sized from equity; Black-Scholes-free selection (delta band, spread, affordability).

Risk controls (all user-tunable; presets via `aggressiveness`):
  stop / R-multiple target / trailing after +1R, max hold time, flat before the close, max trades per day,
  minimum spacing, max concurrent positions, daily loss stop, consecutive-loss ("tilt") stop, dollar floor.

HONESTY: compounding a tiny account into thousands requires a long streak of high-variance wins. Most paths end
at the floor. `quant.growth.goal_odds` estimates the probability for the chosen settings; the UI shows it.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

import numpy as np

from vantage.core import clock
from vantage.core.types import ExitSignal, ManagedPosition, OptionContract, ParamSpec, TradeIdea
from vantage.quant import indicators
from vantage.strategies.base import Strategy, StrategyContext, default_exit

log = logging.getLogger(__name__)

AGGRESSION: dict[str, dict[str, float]] = {
    #               share size  option size  trades/day  spacing  min score  stop    reward R  daily stop  tilt
    "moderate": {"position_pct": 0.35, "option_pct": 0.15, "max_trades_per_day": 3, "min_minutes_between": 20,
                 "min_score": 0.60, "stop_pct": 0.015, "reward_r": 2.0, "daily_stop_pct": 0.15, "max_consecutive_losses": 2},
    "aggressive": {"position_pct": 0.70, "option_pct": 0.30, "max_trades_per_day": 6, "min_minutes_between": 10,
                   "min_score": 0.45, "stop_pct": 0.020, "reward_r": 2.5, "daily_stop_pct": 0.25, "max_consecutive_losses": 3},
    "all_in": {"position_pct": 1.00, "option_pct": 0.50, "max_trades_per_day": 12, "min_minutes_between": 5,
               "min_score": 0.30, "stop_pct": 0.025, "reward_r": 3.0, "daily_stop_pct": 0.40, "max_consecutive_losses": 4},
}
TUNED = tuple(AGGRESSION["aggressive"])

# long instrument -> inverse instrument (bearish setups are expressed by buying the inverse)
INVERSE = {"TQQQ": "SQQQ", "QQQ": "SQQQ", "SOXL": "SOXS", "SPXL": "SPXS", "SPY": "SPXS", "TNA": "TZA",
           "IWM": "TZA", "UPRO": "SPXU", "TECL": "TECS", "LABU": "LABD", "FAS": "FAZ"}

WINDOWS = {
    "open_drive": [(5, 90)],  # minutes after 09:30
    "open_and_power_hour": [(5, 90), (300, 375)],
    "all_day": [(5, 375)],
}


class RagsToRiches(Strategy):
    id = "rags_to_riches"
    name = "Rags to Riches"
    description = ("High-risk small-account accelerator ($20+): day-trades the strongest intraday setup (opening-range "
                   "breakouts, VWAP pullbacks, high/low-of-day momentum) on leveraged ETFs and in-play names, plus "
                   "optional cheap 0-2 DTE SPY/QQQ options, compounding most of the balance per trade until your "
                   "dollar goal is reached — then it stops. Most attempts lose money; check the odds estimator.")
    asset_class = "equity"
    style = "intraday"
    backtest_support = "none"
    backtest_note = ("Intraday strategy: needs minute-bar backtesting, which isn't supported yet. Use the odds "
                     "estimator on its strategy card, or run it in SIM / paper first.")
    params_schema = [
        # ---- goal
        ParamSpec("goal_equity", "float", 1000.0, "Goal ($)", "Stop trading once account equity reaches this",
                  "Goal", min=10, max=10_000_000, step=10),
        ParamSpec("floor_equity", "float", 10.0, "Floor ($)", "Stop trading (and flatten) if equity falls to this — "
                  "protects the last dollars from a total wipe-out", "Goal", min=0, max=10_000_000, step=1),
        ParamSpec("flatten_on_goal", "bool", True, "Sell everything at goal", "Lock in the gain by closing this "
                  "strategy's positions when the goal is hit", "Goal"),
        ParamSpec("after_goal", "enum", "auto_tier", "After the goal", "auto_tier = switch to the normal tier preset "
                  "for the new balance; stop = just stop", "Goal", options=["auto_tier", "stop"]),
        # ---- aggressiveness
        ParamSpec("aggressiveness", "enum", "aggressive", "Aggressiveness", "Preset for the sizing / activity knobs "
                  "below. Choose custom to use your own values.", "Aggressiveness",
                  options=["moderate", "aggressive", "all_in", "custom"]),
        ParamSpec("position_pct", "float", 0.70, "Share position size", "Fraction of equity per share trade "
                  "(custom only)", "Aggressiveness", min=0.05, max=1.0, step=0.05),
        ParamSpec("option_pct", "float", 0.30, "Option premium size", "Fraction of equity spent on premium per "
                  "option trade (custom only)", "Aggressiveness", min=0.02, max=1.0, step=0.02),
        ParamSpec("max_trades_per_day", "int", 6, "Max trades per day", "custom only", "Aggressiveness",
                  min=1, max=50, step=1),
        ParamSpec("min_minutes_between", "int", 10, "Min minutes between entries", "custom only", "Aggressiveness",
                  min=0, max=240, step=1),
        ParamSpec("min_score", "float", 0.45, "Min setup score", "0-1 quality bar for a setup; lower = more trades "
                  "(custom only)", "Aggressiveness", min=0.0, max=1.0, step=0.05),
        ParamSpec("stop_pct", "float", 0.020, "Stop (shares)", "Max loss per share trade as a fraction of price "
                  "(structure stops are clamped to 0.5-2x this; custom only)", "Aggressiveness", min=0.003, max=0.2,
                  step=0.001),
        ParamSpec("reward_r", "float", 2.5, "Target (R multiple)", "Profit target in multiples of the stop "
                  "(custom only)", "Aggressiveness", min=0.5, max=10, step=0.25),
        ParamSpec("daily_stop_pct", "float", 0.25, "Daily loss stop", "No new entries after the account is down "
                  "this much today (custom only)", "Aggressiveness", min=0.02, max=0.9, step=0.01),
        ParamSpec("max_consecutive_losses", "int", 3, "Losing-streak stop", "No new entries today after this many "
                  "losses in a row (custom only)", "Aggressiveness", min=1, max=20, step=1),
        # ---- activity / exits
        ParamSpec("max_concurrent", "int", 1, "Max concurrent positions", "1 = all-in on the single best setup",
                  "Activity", min=1, max=3, step=1),
        ParamSpec("session_windows", "enum", "open_and_power_hour", "Trading windows", "open_drive = 9:35-11:00 ET; "
                  "+ power hour 14:30-15:45; all_day = 9:35-15:45", "Activity",
                  options=["open_drive", "open_and_power_hour", "all_day"]),
        ParamSpec("trail_after_r", "float", 1.0, "Trail after (R)", "Start a trailing stop once up this many R",
                  "Activity", min=0.25, max=10, step=0.25),
        ParamSpec("max_hold_minutes", "int", 90, "Max hold (minutes)", "Time stop for any position", "Activity",
                  min=5, max=390, step=5),
        ParamSpec("flat_before_close_min", "int", 5, "Flatten N min before close", "", "Activity", min=1, max=60, step=1),
        ParamSpec("hold_overnight", "bool", False, "Allow overnight holds", "Off = always flat at the close",
                  "Activity"),
        # ---- playbook
        ParamSpec("use_orb", "bool", True, "Opening-range breakout", "", "Playbook"),
        ParamSpec("orb_minutes", "int", 5, "Opening range (min)", "", "Playbook", min=1, max=60, step=1),
        ParamSpec("use_vwap", "bool", True, "VWAP trend pullback", "", "Playbook"),
        ParamSpec("use_hod", "bool", True, "High/low-of-day momentum", "", "Playbook"),
        ParamSpec("symbols", "str", "TQQQ,SOXL,SPXL,TNA,TSLL,NVDL", "Share instruments", "Leveraged ETFs / liquid "
                  "names traded long (comma-separated)", "Playbook"),
        ParamSpec("allow_inverse", "bool", True, "Bearish via inverse ETFs", "Buy SQQQ/SOXS/... on bearish setups",
                  "Playbook"),
        ParamSpec("use_most_actives", "bool", True, "Add today's most-active stocks", "In-play names from the "
                  "broker screener", "Playbook"),
        ParamSpec("most_actives_n", "int", 5, "How many most-actives", "", "Playbook", min=0, max=20, step=1),
        ParamSpec("min_price", "float", 3.0, "Min share price", "Skip penny stocks", "Playbook", min=0.5, max=1000,
                  step=0.5),
        ParamSpec("allow_options", "bool", True, "Options power plays", "Cheap short-dated SPY/QQQ calls/puts",
                  "Options"),
        ParamSpec("option_underlyings", "str", "SPY,QQQ", "Option underlyings", "", "Options"),
        ParamSpec("option_max_dte", "int", 2, "Max days to expiry", "0 = same-day (0DTE)", "Options", min=0, max=14,
                  step=1),
        ParamSpec("option_delta_min", "float", 0.15, "Min |delta|", "", "Options", min=0.02, max=0.9, step=0.01),
        ParamSpec("option_delta_max", "float", 0.45, "Max |delta|", "", "Options", min=0.05, max=0.95, step=0.01),
        ParamSpec("option_max_spread", "float", 0.25, "Max option spread %", "", "Options", min=0.02, max=1.0,
                  step=0.01),
        ParamSpec("option_stop", "float", 0.50, "Option stop", "Exit at this loss on premium", "Options",
                  min=0.1, max=1.0, step=0.05),
        ParamSpec("option_target", "float", 1.00, "Option target", "Exit at this gain on premium (1.0 = double)",
                  "Options", min=0.1, max=20, step=0.1),
    ]
    exit_defaults = {"exit_on_flip": False, "exit_before_earnings": False, "min_dte_exit": 0}

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self._acquired: set[str] = set()
        self._actives: tuple[float, list[str]] = (0.0, [])

    # ------------------------------------------------------------------ settings
    def effective(self) -> dict[str, Any]:
        """Tuned knobs after applying the aggressiveness preset (custom = the individual params)."""
        p = self.params
        preset = AGGRESSION.get(p["aggressiveness"])
        eff = {k: (preset[k] if preset else p[k]) for k in TUNED}
        eff["max_trades_per_day"] = int(eff["max_trades_per_day"])
        eff["max_consecutive_losses"] = int(eff["max_consecutive_losses"])
        return eff

    def extra_info(self) -> dict[str, Any]:
        rt = self.runtime or {}
        return {"effective": self.effective(), "goal_equity": self.params["goal_equity"],
                "floor_equity": self.params["floor_equity"], "start_equity": rt.get("start_equity"),
                "halted": rt.get("halted")}

    def should_halt(self, ctx: StrategyContext) -> dict[str, Any] | None:
        eq = float(ctx.account.get("equity") or 0.0)
        p = self.params
        if eq >= p["goal_equity"]:
            return {"reason": f"goal reached: equity ${eq:,.2f} ≥ goal ${p['goal_equity']:,.2f}",
                    "flatten": p["flatten_on_goal"], "next_preset": p["after_goal"] == "auto_tier"}
        if eq <= p["floor_equity"]:
            return {"reason": f"floor hit: equity ${eq:,.2f} ≤ floor ${p['floor_equity']:,.2f}",
                    "flatten": True, "next_preset": False}
        return None

    # ------------------------------------------------------------------ entries
    def _blocked(self, ctx: StrategyContext, eff: dict[str, Any]) -> str | None:
        p = self.params
        acct = ctx.account
        eq = float(acct.get("equity") or 0.0)
        if eq >= p["goal_equity"] or eq <= p["floor_equity"]:
            return "goal/floor"
        st = ctx.today.get(self.id, {})
        if st.get("entries", 0) >= eff["max_trades_per_day"]:
            return "daily trade cap"
        if ctx.now - st.get("last_entry_t", 0) < eff["min_minutes_between"] * 60_000:
            return "spacing"
        if st.get("consecutive_losses", 0) >= eff["max_consecutive_losses"]:
            return "losing streak"
        last = float(acct.get("last_equity") or eq)
        if last > 0 and (eq - last) / last <= -eff["daily_stop_pct"]:
            return "daily loss stop"
        held = sum(1 for sid in ctx.owned.values() if sid == self.id)
        if held >= p["max_concurrent"]:
            return "max concurrent"
        if ctx.mode != "sim" and not ctx.backtest:
            ms = ctx.market_state
            mso = ms.minutes_since_open
            if not ms.is_open or mso is None:
                return "market closed"
            if not any(a <= mso <= b for a, b in WINDOWS[p["session_windows"]]):
                return "outside trading window"
            if ms.minutes_to_close is not None and ms.minutes_to_close <= p["flat_before_close_min"] + 5:
                return "too close to the close"
        return None

    async def _instruments(self, ctx: StrategyContext) -> tuple[list[str], list[str]]:
        p = self.params
        longs = [s.strip().upper() for s in str(p["symbols"]).split(",") if s.strip()]
        if p["use_most_actives"] and p["most_actives_n"] > 0 and ctx.mode != "sim":
            ts, cached = self._actives
            if time.monotonic() - ts > 600:
                try:
                    cached = await ctx.market.broker.get_most_actives(25)
                except Exception as e:  # noqa: BLE001
                    log.debug("most actives failed: %s", e)
                self._actives = (time.monotonic(), cached)
            picks = []
            for s in cached:
                px = ctx.market.price(s) or await ctx.market.ensure_price(s)
                if px and px >= p["min_price"] and s not in longs:
                    picks.append(s)
                if len(picks) >= p["most_actives_n"]:
                    break
            longs += picks
        opts = [s.strip().upper() for s in str(p["option_underlyings"]).split(",") if s.strip()] if p["allow_options"] else []
        watch = set(longs) | set(opts) | {"QQQ"}
        if p["allow_inverse"]:
            watch |= {INVERSE[s] for s in longs if s in INVERSE}
        new = sorted(watch - self._acquired)
        if new and hasattr(ctx.market, "acquire"):
            await ctx.market.acquire(new)  # stream minute bars for everything we watch
            self._acquired |= set(new)
        return longs, opts

    async def _session(self, ctx: StrategyContext, sym: str) -> dict[str, np.ndarray] | None:
        bars = await ctx.market.intraday_bars(sym, 390)
        if ctx.mode != "sim":
            so, _ = clock.session_bounds_ms(clock.last_session_date())
            bars = [b for b in bars if b.t >= so]
        if len(bars) < self.params["orb_minutes"] + 3:
            return None
        return ctx.market.ohlcv(bars)

    def _setups(self, a: dict[str, np.ndarray], tape: int) -> list[dict[str, Any]]:
        """Score long (+1) and short (-1) setups on session 1-minute bars. tape = QQQ vs VWAP (+1/-1/0)."""
        p = self.params
        o, h, l, c, v = a["o"], a["h"], a["l"], a["c"], a["v"]
        n = len(c)
        vw = indicators.session_vwap(c, v, np.zeros(n))
        avg_v = float(np.mean(v[max(0, n - 31) : n - 1])) if n > 2 else 0.0
        rel = float(v[-1] / avg_v) if avg_v > 0 else 1.0
        atr1 = float(np.mean((h - l)[-14:])) or max(c[-1] * 0.001, 1e-6)
        px = float(c[-1])
        out: list[dict[str, Any]] = []

        def add(name: str, direction: int, score: float, stop_px: float) -> None:
            score += 0.10 * tape * direction if tape else 0.0
            out.append({"setup": name, "direction": direction, "score": float(max(0.0, min(1.0, score))),
                        "stop_px": float(stop_px), "rel_volume": round(rel, 2), "vwap": float(vw[-1])})

        if p["use_orb"]:
            k = int(p["orb_minutes"])
            orh, orl = float(np.max(h[:k])), float(np.min(l[:k]))
            drive = 1 if c[k - 1] > o[0] else -1
            fresh_up = bool(np.any(c[-4:-1] <= orh))
            fresh_dn = bool(np.any(c[-4:-1] >= orl))
            vol_bonus = 0.2 * min(rel / 2.0, 1.5)
            if px > orh and fresh_up and drive > 0 and px > vw[-1]:
                add("orb", 1, 0.40 + vol_bonus + 0.15 * min((px - orh) / atr1, 1.0), max(orl, px - 3 * atr1))
            if px < orl and fresh_dn and drive < 0 and px < vw[-1]:
                add("orb", -1, 0.40 + vol_bonus + 0.15 * min((orl - px) / atr1, 1.0), min(orh, px + 3 * atr1))
        if p["use_vwap"] and n >= 20:
            slope = (vw[-1] - vw[-16]) / max(atr1, 1e-9)
            touched_up = float(np.min(l[-5:])) <= vw[-1] * 1.002
            touched_dn = float(np.max(h[-5:])) >= vw[-1] * 0.998
            bounce_up = c[-1] > o[-1] and c[-1] > c[-2] and px > vw[-1]
            bounce_dn = c[-1] < o[-1] and c[-1] < c[-2] and px < vw[-1]
            strength = min(abs(slope) / 3.0, 1.0)
            if slope > 0 and touched_up and bounce_up:
                add("vwap_pullback", 1, 0.35 + 0.25 * strength + 0.1 * min(rel, 2.0), float(np.min(l[-5:])) - 0.25 * atr1)
            if slope < 0 and touched_dn and bounce_dn:
                add("vwap_pullback", -1, 0.35 + 0.25 * strength + 0.1 * min(rel, 2.0), float(np.max(h[-5:])) + 0.25 * atr1)
        if p["use_hod"] and n >= 15:
            roc5 = px / float(c[-6]) - 1 if n > 6 else 0.0
            if px >= float(np.max(h[:-1])) and rel >= 2.0 and roc5 > 0:
                add("hod_momentum", 1, 0.40 + 0.3 * min(rel / 4.0, 1.0) + 0.1 * (px > vw[-1]), px - 2 * atr1)
            if px <= float(np.min(l[:-1])) and rel >= 2.0 and roc5 < 0:
                add("lod_momentum", -1, 0.40 + 0.3 * min(rel / 4.0, 1.0) + 0.1 * (px < vw[-1]), px + 2 * atr1)
        return out

    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]:
        eff = self.effective()
        if self._blocked(ctx, eff):
            return []
        p = self.params
        longs, opt_unds = await self._instruments(ctx)
        tape = 0
        qqq = await self._session(ctx, "QQQ")
        if qqq is not None:
            qv = indicators.session_vwap(qqq["c"], qqq["v"], np.zeros(len(qqq["c"])))
            tape = 1 if qqq["c"][-1] > qv[-1] else -1
        held = set(ctx.owned)
        ideas: list[TradeIdea] = []
        for sym in dict.fromkeys(longs + opt_unds):
            a = await self._session(ctx, sym)
            if a is None:
                continue
            for s in self._setups(a, tape):
                if s["score"] < eff["min_score"]:
                    continue
                if sym in longs:
                    idea = self._share_idea(ctx, sym, s, eff, held)
                    if idea:
                        ideas.append(idea)
                if sym in opt_unds:
                    idea = await self._option_idea(ctx, sym, s, eff, held)
                    if idea:
                        ideas.append(idea)
        ideas.sort(key=lambda i: -i.edge_score)
        slots = p["max_concurrent"] - sum(1 for sid in ctx.owned.values() if sid == self.id)
        return ideas[: max(0, slots)]

    def _share_idea(self, ctx: StrategyContext, sym: str, s: dict[str, Any], eff: dict[str, Any],
                    held: set[str]) -> TradeIdea | None:
        p = self.params
        inst = sym
        if s["direction"] < 0:
            if not p["allow_inverse"] or sym not in INVERSE:
                return None
            inst = INVERSE[sym]
        if inst in held:
            return None
        px = ctx.market.price(inst)
        if not px or px < p["min_price"]:
            return None
        und_px = ctx.market.price(sym) or px
        stop_frac = abs(und_px - s["stop_px"]) / und_px if und_px else eff["stop_pct"]
        stop_frac = float(min(max(stop_frac, 0.5 * eff["stop_pct"]), 2.0 * eff["stop_pct"]))
        r = eff["reward_r"]
        return TradeIdea(
            strategy_id=self.id, symbol=inst, underlying=inst, asset_class="equity", direction=1, limit_price=px,
            edge_score=round(10 * s["score"], 3), expected_return=stop_frac * (0.45 * r - 0.55), win_prob=0.45,
            kelly_fraction=eff["position_pct"], horizon_days=p["max_hold_minutes"] / 390,
            rationale=(f"{s['setup'].replace('_', ' ')} {'long' if s['direction'] > 0 else 'short via ' + inst} on "
                       f"{sym} (score {s['score']:.2f}, rel vol {s['rel_volume']}x); stop {stop_frac:.2%}, "
                       f"target {r:g}R, size {eff['position_pct']:.0%} of equity"),
            stop_pct=stop_frac, target_pct=r * stop_frac, trail_pct=stop_frac,
            trail_activate_pct=p["trail_after_r"] * stop_frac, time_stop_days=p["max_hold_minutes"] / 1440,
            signals={"setup": s, "intraday": True, "event_ok": True}, target_fraction=eff["position_pct"],
        )

    async def _option_idea(self, ctx: StrategyContext, und: str, s: dict[str, Any], eff: dict[str, Any],
                           held: set[str]) -> TradeIdea | None:
        p = self.params
        if not p["allow_options"] or ctx.options is None:
            return None
        today = clock.today_et()
        try:
            chain = await ctx.options.chain(und, today, today + timedelta(days=int(p["option_max_dte"])))
            if not chain and ctx.mode == "sim":  # the sim lists weekly expiries only
                chain = await ctx.options.chain(und, today, today + timedelta(days=7))
        except Exception as e:  # noqa: BLE001
            log.debug("chain %s failed: %s", und, e)
            return None
        want = "call" if s["direction"] > 0 else "put"
        budget = float(ctx.account.get("equity") or 0.0) * eff["option_pct"]
        cands: list[OptionContract] = []
        for c in chain:
            if c.type != want or c.bid <= 0 or c.ask <= 0 or c.symbol in held:
                continue
            d = abs(c.delta) if c.delta is not None else None
            if d is None or not (p["option_delta_min"] <= d <= p["option_delta_max"]):
                continue
            if c.spread_pct > p["option_max_spread"] or c.ask * 100 > budget:
                continue
            cands.append(c)
        if not cands:
            return None
        best = min(cands, key=lambda c: (abs(abs(c.delta or 0) - 0.30), c.spread_pct))
        entry = round(best.mid + 0.5 * (best.ask - best.mid), 2)
        return TradeIdea(
            strategy_id=self.id, symbol=best.symbol, underlying=und, asset_class="option", direction=s["direction"],
            limit_price=entry, edge_score=round(10 * s["score"] + 0.25, 3),
            expected_return=0.4 * p["option_target"] - 0.6 * p["option_stop"], win_prob=0.4,
            kelly_fraction=eff["option_pct"], horizon_days=p["max_hold_minutes"] / 390,
            rationale=(f"options power play: {s['setup'].replace('_', ' ')} {want} on {und} (score {s['score']:.2f}); "
                       f"{best.symbol} Δ {best.delta:.2f} @ {entry:.2f} (${entry * 100:.0f}/contract), stop "
                       f"-{p['option_stop']:.0%}, target +{p['option_target']:.0%}"),
            contract=best, stop_pct=p["option_stop"], target_pct=p["option_target"], trail_pct=0.35,
            trail_activate_pct=p["option_target"] / 2, time_stop_days=p["max_hold_minutes"] / 1440, min_dte_exit=None,
            signals={"setup": s, "intraday": True, "event_ok": True}, target_fraction=eff["option_pct"],
            spread_pct=best.spread_pct,
        )

    # ------------------------------------------------------------------ exits
    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None:
        p = self.params
        ms = ctx.market_state
        if ctx.mode != "sim" and not p["hold_overnight"] and ms.minutes_to_close is not None \
                and ms.minutes_to_close <= p["flat_before_close_min"]:
            return ExitSignal("Rags to Riches: flat before the close", urgent=True)
        if pos.entry_t and (ctx.now - pos.entry_t) / 60_000 >= p["max_hold_minutes"]:
            return ExitSignal(f"max hold {p['max_hold_minutes']} min reached")
        return default_exit(ctx, pos, {**self.params, "exit_on_flip": False, "exit_before_earnings": False})

