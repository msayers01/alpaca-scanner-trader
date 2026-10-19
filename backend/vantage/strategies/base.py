"""Strategy interface, context and the shared exit policy (docs/ARCHITECTURE.md §6.2)."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

from vantage.core import clock
from vantage.core.types import (
    ExitSignal,
    ManagedPosition,
    OptionContract,
    ParamSpec,
    TradeIdea,
    apply_params,
    parse_occ,
)
from vantage.core.util import now_ms

DAY_MS = 86_400_000


@dataclass
class StrategyContext:
    mode: str
    market: Any
    options: Any
    signals: Any
    scanner: Any
    news: Any
    flow: Any
    calendar: Any
    llm: Any
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    tier: str
    risk: dict[str, Any]
    universe: list[str]
    market_state: clock.MarketState
    now: int = field(default_factory=now_ms)
    backtest: bool = False

    @property
    def session_open(self) -> bool:
        return self.market_state.is_open or self.mode == "sim"


EXIT_SCHEMA: list[ParamSpec] = [
    ParamSpec("stop_pct", "float", 0.40, "Stop loss", "Exit when the position loses this fraction", "Exits", min=0.01, max=1.0, step=0.01),
    ParamSpec("target_pct", "float", 0.80, "Profit target", "Exit when the position gains this fraction", "Exits", min=0.01, max=10.0, step=0.05),
    ParamSpec("trail_activate_pct", "float", 0.40, "Trailing stop activates at", "", "Exits", min=0.0, max=10.0, step=0.05),
    ParamSpec("trail_pct", "float", 0.25, "Trailing stop", "Give-back from the peak value once active", "Exits", min=0.01, max=0.9, step=0.01),
    ParamSpec("time_stop_days", "float", 10.0, "Time stop (days)", "Exit after holding this many calendar days", "Exits", min=0.1, max=365, step=0.5),
    ParamSpec("min_dte_exit", "int", 7, "Exit at DTE ≤", "Options only — avoid the gamma/theta cliff", "Exits", min=0, max=60, step=1),
    ParamSpec("exit_on_flip", "bool", True, "Exit on forecast flip", "Exit when the ensemble forecast reverses strongly", "Exits"),
    ParamSpec("flip_z", "float", 0.75, "Flip threshold (z)", "", "Exits", min=0.1, max=4, step=0.05),
    ParamSpec("exit_before_earnings", "bool", True, "Exit before earnings", "Close options the day before earnings", "Exits"),
]


class Strategy(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    asset_class: ClassVar[str] = "option"
    style: ClassVar[str] = "swing"
    default_llm_gate: ClassVar[bool] = False
    params_schema: ClassVar[list[ParamSpec]] = []
    exit_defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = apply_params(self.schema(), {**self.exit_defaults, **(params or {})})
        self.use_llm_gate = self.default_llm_gate

    @classmethod
    def schema(cls) -> list[ParamSpec]:
        own = {p.name for p in cls.params_schema}
        exits = [ParamSpec(**{**p.to_wire(), "default": cls.exit_defaults.get(p.name, p.default)})  # type: ignore[arg-type]
                 for p in EXIT_SCHEMA if p.name not in own]
        return list(cls.params_schema) + exits

    @abstractmethod
    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]: ...

    def exit_params(self) -> dict[str, Any]:
        p = self.params
        return {
            "stop_pct": p["stop_pct"],
            "target_pct": p["target_pct"],
            "trail_pct": p["trail_pct"],
            "trail_activate_pct": p["trail_activate_pct"],
            "time_stop_days": p["time_stop_days"],
            "min_dte_exit": p["min_dte_exit"] if self.asset_class == "option" else None,
        }

    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None:
        return default_exit(ctx, pos, self.params)

    # ------------------------------------------------------------------ helpers
    def option_idea(self, cand: dict[str, Any], rationale: str, signals: dict[str, Any], use_llm: bool,
                    direction: int, horizon: float) -> TradeIdea:
        occ = parse_occ(cand["contract"])
        assert occ is not None
        contract = OptionContract(
            symbol=cand["contract"], underlying=cand["underlying"], type=cand["type"], strike=cand["strike"],
            expiration=date.fromisoformat(cand["expiration"]), bid=cand["bid"], ask=cand["ask"],
            volume=cand["volume"], open_interest=cand["open_interest"], iv=cand["iv"], delta=cand["delta"],
            theta=cand.get("theta"),
        )
        return TradeIdea(
            strategy_id=self.id, symbol=cand["contract"], underlying=cand["underlying"], asset_class="option",
            direction=direction, limit_price=cand["entry_price"], edge_score=cand["edge_score"],
            expected_return=cand["ev_pct"], win_prob=cand["pop"], kelly_fraction=cand["kelly"], horizon_days=horizon,
            rationale=rationale, contract=contract, signals=signals | {"scan": {k: cand[k] for k in (
                "ev_pct", "pop", "kelly", "iv", "iv_rank", "iv_rv_ratio", "delta", "dte", "spread_pct", "breakeven")}},
            use_llm_gate=use_llm, spread_pct=cand["spread_pct"], **self.exit_params(),
        )


def default_exit(ctx: StrategyContext, pos: ManagedPosition, p: dict[str, Any]) -> ExitSignal | None:
    pl = pos.unrealized_plpc
    stop = pos.stop_pct if pos.stop_pct is not None else p.get("stop_pct")
    target = pos.target_pct if pos.target_pct is not None else p.get("target_pct")
    trail = pos.trail_pct if pos.trail_pct is not None else p.get("trail_pct")
    act = pos.trail_activate_pct if pos.trail_activate_pct is not None else p.get("trail_activate_pct")
    tstop = pos.time_stop_days if pos.time_stop_days is not None else p.get("time_stop_days")
    if stop is not None and pl <= -stop:
        return ExitSignal(f"stop loss hit ({pl * 100:.1f}% ≤ -{stop * 100:.0f}%)", urgent=True)
    if target is not None and pl >= target:
        return ExitSignal(f"profit target hit ({pl * 100:.1f}% ≥ {target * 100:.0f}%)")
    if trail and act is not None and pos.peak_plpc >= act:
        give_back = (1 + pl) / (1 + pos.peak_plpc) - 1
        if give_back <= -trail:
            return ExitSignal(f"trailing stop: {give_back * 100:.1f}% from peak +{pos.peak_plpc * 100:.0f}%")
    if tstop and pos.entry_t and (ctx.now - pos.entry_t) / DAY_MS >= tstop:
        return ExitSignal(f"time stop ({tstop:g} days)")
    if pos.asset_class == "option" and pos.expiration is not None:
        dte = (pos.expiration - clock.today_et()).days
        mde = pos.min_dte_exit if pos.min_dte_exit is not None else p.get("min_dte_exit")
        if mde is not None and dte <= mde:
            return ExitSignal(f"DTE {dte} ≤ {mde}: exiting before the theta/gamma cliff")
        if p.get("exit_before_earnings", True) and ctx.calendar and not pos.meta.get("event_ok"):
            de = ctx.calendar.days_to_earnings(pos.underlying)
            if de is not None and 0 <= de <= 1 and de < dte:
                return ExitSignal(f"earnings in {de}d — avoiding IV crush / binary risk")
    if p.get("exit_on_flip", True) and ctx.signals:
        fc = ctx.signals.forecast(pos.underlying, 10)
        if fc["contributions"] and fc["z"] * pos.direction <= -float(p.get("flip_z", 0.75)):
            return ExitSignal(f"forecast flipped against position (z {fc['z']:+.2f})")
    return None


def dossier(ctx: StrategyContext, symbol: str, idea: TradeIdea | None = None) -> dict[str, Any]:
    """Compact, LLM-friendly evidence bundle for one underlying."""
    fc = ctx.signals.forecast(symbol, idea.horizon_days if idea else 10) if ctx.signals else {}
    sigs = [s for s in ctx.signals.signals_for(symbol)] if ctx.signals else []
    news = ctx.news.recent(symbol, 8) if ctx.news else []
    flow = ctx.flow.flow_score(symbol) if ctx.flow else {}
    de = ctx.calendar.days_to_earnings(symbol) if ctx.calendar else None
    price = ctx.market.price(symbol) if ctx.market else None
    d: dict[str, Any] = {
        "symbol": symbol,
        "price": price,
        "forecast": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in fc.items() if k != "contributions"},
        "top_contributors": fc.get("contributions", [])[:6],
        "signals": [{"model": s["model"], "score": s["score"], "confidence": s["confidence"],
                     "meta": {k: v for k, v in list(s["meta"].items())[:5]}} for s in sigs],
        "volatility": {"atm_iv": ctx.signals.atm_iv.get(symbol) if ctx.signals else None,
                       "forecast_vol": ctx.signals.vol_forecast.get(symbol) if ctx.signals else None},
        "news": [{"age_h": round((ctx.now - n.t) / 3600_000, 1), "headline": n.headline, "sentiment": n.sentiment,
                  "llm": (n.llm or {}).get("rationale")} for n in news],
        "options_flow": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in flow.items()},
        "events": {"days_to_earnings": de},
        "portfolio": {"equity": round(ctx.account.get("equity", 0), 2), "open_positions": len(ctx.positions),
                      "tier": ctx.tier},
    }
    if idea is not None:
        d["proposed_trade"] = {
            "strategy": idea.strategy_id, "instrument": idea.symbol, "asset_class": idea.asset_class,
            "direction": "bullish" if idea.direction > 0 else "bearish", "limit_price": idea.limit_price,
            "expected_return": round(idea.expected_return, 4), "win_prob": round(idea.win_prob, 3),
            "kelly_fraction": round(idea.kelly_fraction, 4), "horizon_days": idea.horizon_days,
            "stop_pct": idea.stop_pct, "target_pct": idea.target_pct, "rationale": idea.rationale,
            "scan": idea.signals.get("scan"),
        }
    return d


def finite(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default
