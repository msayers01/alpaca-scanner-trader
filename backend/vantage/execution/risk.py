"""RiskManager: hard pre-trade checks and position sizing. Every order goes through here.

Sizing (docs/QUANT.md §4, §8): the smallest of
  fractional-Kelly dollars, per-trade risk budget / per-unit risk, max position %, buying power,
  remaining options allocation — then floored to whole contracts (never rounded up).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from vantage.core import clock
from vantage.core.types import ParamSpec, TradeIdea, apply_params

RISK_SCHEMA: list[ParamSpec] = [
    ParamSpec("kelly_multiplier", "float", 0.25, "Kelly multiplier (κ)", "Fraction of full Kelly to bet. 0.25 = quarter Kelly.",
              "Sizing", min=0.0, max=1.0, step=0.05),
    ParamSpec("max_risk_per_trade", "float", 0.02, "Max risk per trade", "Fraction of equity that may be lost if the stop "
              "is hit (options: at least half the premium is counted as at risk)", "Sizing", min=0.001, max=0.25, step=0.005),
    ParamSpec("max_position_pct", "float", 0.10, "Max position size", "Fraction of equity in any single position",
              "Sizing", min=0.01, max=1.0, step=0.01),
    ParamSpec("min_one_contract", "bool", True, "Allow 1-contract minimum", "If Kelly sizes a positive-edge option "
              "below one contract, buy one anyway — but only when it fits the per-trade risk budget, position cap, "
              "options allocation and buying power", "Sizing"),
    ParamSpec("max_options_allocation", "float", 0.50, "Max options allocation", "Fraction of equity in option premium",
              "Sizing", min=0.0, max=1.0, step=0.05),
    ParamSpec("max_open_positions", "int", 8, "Max open positions", "", "Exposure", min=1, max=100, step=1),
    ParamSpec("max_per_underlying", "int", 1, "Max positions per underlying", "", "Exposure", min=1, max=10, step=1),
    ParamSpec("max_trades_per_day", "int", 8, "Max new entries per day", "", "Exposure", min=0, max=200, step=1),
    ParamSpec("max_new_per_cycle", "int", 2, "Max new entries per cycle", "", "Exposure", min=1, max=20, step=1),
    ParamSpec("max_daily_loss", "float", 0.04, "Daily loss kill switch", "Disarm and stop new entries if equity falls this "
              "much vs. the previous close", "Kill switch", min=0.005, max=0.5, step=0.005),
    ParamSpec("max_drawdown", "float", 0.18, "Drawdown kill switch", "Disarm if equity falls this much from its peak",
              "Kill switch", min=0.02, max=0.9, step=0.01),
    ParamSpec("min_option_volume", "int", 10, "Min option volume", "", "Liquidity", min=0, max=100000, step=10),
    ParamSpec("min_open_interest", "int", 100, "Min open interest", "", "Liquidity", min=0, max=100000, step=10),
    ParamSpec("max_spread_pct", "float", 0.10, "Max spread %", "Bid-ask spread / mid", "Liquidity", min=0.005, max=1.0, step=0.005),
    ParamSpec("min_edge_score", "float", 1.0, "Min edge score", "Ideas below this are skipped", "Filters", min=-100, max=100, step=0.5),
    ParamSpec("allow_daytrades", "enum", "auto", "Day trades", "auto = unlimited (FINRA eliminated the PDT rule "
              "effective 2026-06-04) but backs off if the broker still rejects orders under legacy PDT protection "
              "during its transition; never = no same-day round trips; always = never back off", "Filters",
              options=["auto", "never", "always"]),
    ParamSpec("entry_start_min", "int", 10, "No entries first N min", "Minutes after the open before new entries",
              "Timing", min=0, max=390, step=1),
    ParamSpec("entry_end_min", "int", 15, "No entries last N min", "Minutes before the close after which no entries",
              "Timing", min=0, max=390, step=1),
    ParamSpec("earnings_blackout_days", "int", 2, "Earnings blackout (days)", "Skip non-event strategies this close to "
              "earnings", "Timing", min=0, max=30, step=1),
    ParamSpec("fomc_blackout", "bool", True, "FOMC blackout", "No entries 13:30-15:00 ET on FOMC decision days", "Timing"),
    ParamSpec("cycle_seconds", "int", 30, "Cycle interval (s)", "", "Engine", min=5, max=3600, step=5),
    ParamSpec("aggression", "float", 0.25, "Order aggression", "Start limit at mid + a*half-spread", "Engine", min=0, max=1, step=0.05),
    ParamSpec("max_aggression", "float", 0.8, "Max aggression", "Re-price ladder stops here", "Engine", min=0, max=1.5, step=0.05),
    ParamSpec("order_timeout_s", "int", 90, "Order timeout (s)", "Cancel unfilled entries after", "Engine", min=10, max=3600, step=5),
    ParamSpec("llm_enabled", "bool", True, "LLM gate", "Send gated ideas to the local LLM for veto/shrink", "LLM"),
    ParamSpec("on_llm_failure", "enum", "skip", "If LLM fails", "skip = no trade; approve = proceed at half size",
              "LLM", options=["skip", "approve"]),
]

DEFAULT_RISK = apply_params(RISK_SCHEMA, None)


@dataclass
class OrderPlan:
    idea: TradeIdea
    symbol: str
    asset_class: str
    side: str
    qty: float | None
    notional: float | None
    limit_price: float
    risk_dollars: float
    cost: float
    sizing: dict[str, Any] = field(default_factory=dict)


@dataclass
class DayState:
    day: date | None = None
    entries: int = 0
    start_equity: float = 0.0
    peak_equity: float = 0.0


# FINRA eliminated the pattern-day-trader rule (no $25k minimum, no day-trade count limit) effective
# 2026-06-04 (Regulatory Notice 26-10); intraday margin is now monitored on actual exposure, which the broker
# enforces through buying power. Firms may keep legacy PDT protection during a transition until 2027-10-20,
# so "auto" allows unlimited day trades until the broker actually rejects one for PDT reasons
# (`broker_pdt` = True), then falls back to the legacy allowance.
# Alpaca: "trade denied due to pattern day trading protection" (code 40310100). Deliberately narrow so the
# new intraday-margin errors (e.g. "insufficient day trading buying power") are NOT mistaken for PDT.
PDT_REJECTION_MARKERS = ("pattern day", "day trading protection", "40310100")


def is_pdt_rejection(message: str) -> bool:
    m = message.lower()
    return any(k in m for k in PDT_REJECTION_MARKERS)


def daytrade_capable(account: dict[str, Any], policy: str, broker_pdt: bool = False) -> bool:
    if policy == "never":
        return False
    if policy == "always" or not broker_pdt:
        return True
    # broker still enforcing legacy PDT protection
    if account.get("equity", 0.0) >= 25_000:
        return True
    return bool(account.get("pattern_day_trader") and account.get("daytrading_buying_power", 0) > 0)


def can_close_same_day(account: dict[str, Any], policy: str, broker_pdt: bool = False) -> bool:
    """Closing a position opened today is a day trade. Unlimited unless the user disabled day trades or the
    broker is enforcing legacy PDT protection (then only within its 3-per-5-days allowance)."""
    if daytrade_capable(account, policy, broker_pdt):
        return True
    return broker_pdt and policy != "never" and account.get("daytrade_count", 0) < 3


class RiskManager:
    def __init__(self, cfg: dict[str, Any] | None = None, mode: str = "sim"):
        self.cfg = apply_params(RISK_SCHEMA, cfg)
        self.mode = mode
        self.day = DayState()
        self.killed = False
        self.kill_reason: str | None = None
        self.broker_pdt = False  # set when the broker rejects an order under legacy PDT protection

    def update(self, values: dict[str, Any]) -> None:
        self.cfg = apply_params(RISK_SCHEMA, {**self.cfg, **values})

    # ------------------------------------------------------------------ kill switch
    def roll_day(self, account: dict[str, Any], today: date) -> None:
        if self.day.day != today:
            self.day = DayState(today, 0, account.get("last_equity") or account.get("equity", 0.0),
                                max(self.day.peak_equity, account.get("equity", 0.0)))

    def check_kill(self, account: dict[str, Any], peak_equity: float) -> str | None:
        eq = account.get("equity", 0.0)
        base = account.get("last_equity") or self.day.start_equity or eq
        if base > 0 and (eq - base) / base <= -self.cfg["max_daily_loss"]:
            return f"daily loss {100 * (eq - base) / base:.2f}% breached limit {-100 * self.cfg['max_daily_loss']:.1f}%"
        if peak_equity > 0 and (eq - peak_equity) / peak_equity <= -self.cfg["max_drawdown"]:
            return f"drawdown {100 * (eq - peak_equity) / peak_equity:.2f}% breached limit {-100 * self.cfg['max_drawdown']:.1f}%"
        return None

    def kill(self, reason: str) -> None:
        self.killed = True
        self.kill_reason = reason

    def reset_kill(self) -> None:
        self.killed = False
        self.kill_reason = None

    # ------------------------------------------------------------------ entry checks + sizing
    def size_and_check(
        self,
        idea: TradeIdea,
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        pending_symbols: set[str],
        market: clock.MarketState,
        days_to_earnings: int | None = None,
        fomc_today: bool = False,
        intraday: bool = False,
        tier_fractional: bool = True,
    ) -> tuple[OrderPlan | None, str]:
        c = self.cfg
        if self.killed:
            return None, f"kill switch active: {self.kill_reason}"
        if account.get("trading_blocked"):
            return None, "account trading blocked"
        # --- timing
        if self.mode != "sim":
            if not market.is_open:
                return None, "market closed"
            if market.minutes_since_open is not None and market.minutes_since_open < c["entry_start_min"]:
                return None, f"inside first {c['entry_start_min']} minutes of session"
            if market.minutes_to_close is not None and market.minutes_to_close < c["entry_end_min"]:
                return None, f"inside last {c['entry_end_min']} minutes of session"
            if fomc_today and c["fomc_blackout"]:
                now = clock.et_now().time()
                if clock.time(13, 30) <= now <= clock.time(15, 0):
                    return None, "FOMC decision blackout window"
        # --- exposure
        if self.day.entries >= c["max_trades_per_day"]:
            return None, f"daily entry cap {c['max_trades_per_day']} reached"
        held = {p["symbol"] for p in positions}
        if idea.symbol in held or idea.symbol in pending_symbols:
            return None, "already holding or working an order in this instrument"
        n_open = len(positions) + len(pending_symbols)
        if n_open >= c["max_open_positions"]:
            return None, f"max open positions {c['max_open_positions']} reached"
        same_und = sum(1 for p in positions if p.get("underlying") == idea.underlying)
        same_und += sum(1 for s in pending_symbols if s.startswith(idea.underlying))
        if same_und >= c["max_per_underlying"]:
            return None, f"max positions per underlying ({c['max_per_underlying']}) reached for {idea.underlying}"
        if idea.edge_score < c["min_edge_score"]:
            return None, f"edge score {idea.edge_score:.2f} below minimum {c['min_edge_score']:.2f}"
        # --- events / day-trading
        event_ok = bool(idea.signals.get("event_ok"))
        if days_to_earnings is not None and 0 <= days_to_earnings <= c["earnings_blackout_days"] and not event_ok:
            return None, f"earnings in {days_to_earnings}d (blackout {c['earnings_blackout_days']}d)"
        if intraday and not daytrade_capable(account, c["allow_daytrades"], self.broker_pdt):
            if c["allow_daytrades"] == "never":
                return None, "intraday strategy skipped: day trades disabled (risk setting 'Day trades' = never)"
            return None, "intraday strategy skipped: broker is still enforcing legacy PDT protection on this account"
        # --- liquidity
        if idea.asset_class == "option":
            ct = idea.contract
            if ct is None:
                return None, "option idea without contract"
            if ct.spread_pct > c["max_spread_pct"]:
                return None, f"spread {ct.spread_pct * 100:.1f}% > {c['max_spread_pct'] * 100:.1f}%"
            if ct.open_interest < c["min_open_interest"] and self.mode != "sim":
                return None, f"open interest {ct.open_interest:.0f} < {c['min_open_interest']}"
            if ct.volume < c["min_option_volume"] and self.mode != "sim":
                return None, f"volume {ct.volume:.0f} < {c['min_option_volume']}"
        # --- sizing
        equity = float(account.get("equity", 0.0))
        if equity <= 0:
            return None, "no equity"
        mult = max(0.0, min(1.0, idea.size_multiplier))
        if idea.target_fraction is not None:  # strategy sizes explicitly (e.g. Rags to Riches); caps below still bind
            kelly = max(0.0, min(1.0, idea.target_fraction)) * mult
        else:
            kelly = max(0.0, idea.kelly_fraction) * c["kelly_multiplier"] * mult
        kelly_dollars = equity * kelly
        cap_dollars = equity * c["max_position_pct"]
        risk_budget = equity * c["max_risk_per_trade"]
        price = float(idea.limit_price)
        if price <= 0:
            return None, "invalid price"
        if idea.asset_class == "option":
            premium = price * 100
            stop = idea.stop_pct if idea.stop_pct else 1.0
            risk_per = premium * max(stop, 0.5)
            opt_val = sum(abs(p.get("market_value", 0.0)) for p in positions if p.get("asset_class") == "option")
            alloc_left = max(0.0, equity * c["max_options_allocation"] - opt_val)
            bp = float(account.get("options_buying_power") or account.get("buying_power") or 0.0)
            limits = {
                "kelly": kelly_dollars / premium,
                "risk_budget": risk_budget / risk_per,
                "max_position": cap_dollars / premium,
                "options_allocation": alloc_left / premium,
                "buying_power": bp / premium,
            }
            binding = min(limits, key=limits.get)  # type: ignore[arg-type]
            n = math.floor(limits[binding] + 1e-9)
            sizing = {k: round(v, 3) for k, v in limits.items()} | {"binding": binding, "premium": premium,
                                                                   "kelly_scaled": round(kelly, 4)}
            if n < 1 and c["min_one_contract"] and kelly > 0:
                hard = {k: v for k, v in limits.items() if k != "kelly"}
                if min(hard.values()) >= 1.0:
                    n = 1
                    sizing["binding"] = "min_one_contract"
                    binding = f"1-contract minimum (Kelly allowed {limits['kelly']:.2f}; within risk budget)"
            if n < 1:
                return None, (f"size rounds to 0 contracts (binding: {binding} allows {limits[binding]:.2f}; "
                              f"premium ${premium:.0f})")
            return OrderPlan(idea, idea.symbol, "option", "buy", float(n), None, price, n * risk_per, n * premium, sizing), \
                f"{n} contract(s) @ {price:.2f} (bound by {binding})"
        # equity (long only)
        stop = idea.stop_pct or 0.08
        bp = float(account.get("buying_power") or 0.0)
        limits = {
            "kelly": kelly_dollars,
            "risk_budget": risk_budget / stop,
            "max_position": cap_dollars,
            "buying_power": bp * 0.98,
        }
        binding = min(limits, key=limits.get)  # type: ignore[arg-type]
        dollars = limits[binding]
        sizing = {k: round(v, 2) for k, v in limits.items()} | {"binding": binding, "kelly_scaled": round(kelly, 4)}
        if tier_fractional:
            if dollars < 1.0:
                return None, f"notional ${dollars:.2f} below $1 (binding: {binding})"
            qty = round(dollars / price, 6)
            return OrderPlan(idea, idea.symbol, "equity", "buy", qty, None, price, dollars * stop, qty * price, sizing), \
                f"{qty:.4f} sh (${dollars:.2f}) bound by {binding}"
        n = math.floor(dollars / price)
        if n < 1:
            return None, f"size rounds to 0 shares (binding: {binding} allows ${dollars:.2f}, price ${price:.2f})"
        return OrderPlan(idea, idea.symbol, "equity", "buy", float(n), None, price, n * price * stop, n * price, sizing), \
            f"{n} sh @ {price:.2f} bound by {binding}"

    def record_entry(self) -> None:
        self.day.entries += 1
