"""Account tiers and per-tier presets (docs/QUANT.md §8)."""

from __future__ import annotations

from typing import Any

TIERS: list[dict[str, Any]] = [
    {
        "tier": "micro",
        "label": "Micro (< $2k)",
        "min_equity": 0,
        "max_equity": 2_000,
        "description": "Fractional-share trend and ETF pullback trades plus the occasional cheap option. Few positions, "
                       "quarter-Kelly shaded down because estimation error dominates at this size.",
        "fractional": True,
        "risk": {"kelly_multiplier": 0.20, "max_risk_per_trade": 0.05, "max_position_pct": 0.30,
                 "max_options_allocation": 0.40, "max_open_positions": 3, "max_per_underlying": 1,
                 "max_trades_per_day": 3, "max_daily_loss": 0.06, "max_drawdown": 0.25, "allow_daytrades": "auto",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge"],
    },
    {
        "tier": "small",
        "label": "Small ($2k – $10k)",
        "min_equity": 2_000,
        "max_equity": 10_000,
        "description": "Adds flow-following and post-earnings drift option trades.",
        "fractional": True,
        "risk": {"kelly_multiplier": 0.25, "max_risk_per_trade": 0.03, "max_position_pct": 0.15,
                 "max_options_allocation": 0.50, "max_open_positions": 5, "max_per_underlying": 1,
                 "max_trades_per_day": 5, "max_daily_loss": 0.05, "max_drawdown": 0.20, "allow_daytrades": "auto",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge", "flow_follow", "earnings_drift"],
    },
    {
        "tier": "growth",
        "label": "Growth ($10k – $25k)",
        "min_equity": 10_000,
        "max_equity": 25_000,
        "description": "All strategies including the LLM analyst and intraday opening-range breakouts.",
        "fractional": True,
        "risk": {"kelly_multiplier": 0.25, "max_risk_per_trade": 0.02, "max_position_pct": 0.10,
                 "max_options_allocation": 0.50, "max_open_positions": 8, "max_per_underlying": 1,
                 "max_trades_per_day": 8, "max_daily_loss": 0.04, "max_drawdown": 0.18, "allow_daytrades": "auto",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge", "flow_follow", "earnings_drift", "llm_analyst",
                       "orb_intraday"],
    },
    {
        "tier": "pro",
        "label": "Pro (≥ $25k)",
        "min_equity": 25_000,
        "max_equity": None,
        "description": "Everything, with the most positions and the tightest per-trade risk.",
        "fractional": False,
        "risk": {"kelly_multiplier": 0.30, "max_risk_per_trade": 0.015, "max_position_pct": 0.08,
                 "max_options_allocation": 0.50, "max_open_positions": 12, "max_per_underlying": 2,
                 "max_trades_per_day": 15, "max_daily_loss": 0.03, "max_drawdown": 0.15, "allow_daytrades": "auto",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge", "flow_follow", "earnings_drift", "llm_analyst",
                       "orb_intraday"],
    },
]
# Manual-only preset (never auto-selected by equity): permissive global caps so the Rags to Riches strategy's own
# sizing, goal and floor govern the account. Kill switches stay on but far wider.
TIERS.append({
    "tier": "rags",
    "label": "Rags to Riches",
    "min_equity": 0,
    "max_equity": None,
    "manual": True,
    "description": "High-risk small-account mode: only the Rags to Riches day-trading strategy runs, sizing up to the "
                   "whole balance per trade until your dollar goal (then it stops) or floor is hit. Most attempts like "
                   "this lose money — use the odds estimator on the strategy card first.",
    "fractional": True,
    "risk": {"kelly_multiplier": 1.0, "max_risk_per_trade": 1.0, "max_position_pct": 1.0,
             "max_options_allocation": 1.0, "max_open_positions": 3, "max_per_underlying": 1,
             "max_trades_per_day": 40, "max_new_per_cycle": 1, "max_daily_loss": 0.60, "max_drawdown": 0.95,
             "allow_daytrades": "auto", "min_edge_score": 0.0, "max_spread_pct": 0.35, "min_open_interest": 0,
             "min_option_volume": 50, "min_one_contract": True, "entry_start_min": 5, "entry_end_min": 10,
             "earnings_blackout_days": 0, "cycle_seconds": 15, "aggression": 0.5, "max_aggression": 1.0,
             "order_timeout_s": 45},
    "strategies": ["rags_to_riches"],
})
TIER_BY_ID = {t["tier"]: t for t in TIERS}


def tier_for_equity(equity: float) -> str:
    for t in TIERS:
        if t.get("manual"):
            continue
        if equity >= t["min_equity"] and (t["max_equity"] is None or equity < t["max_equity"]):
            return t["tier"]
    return "micro"


def presets_wire() -> list[dict[str, Any]]:
    return [{k: t[k] for k in ("tier", "label", "description", "min_equity", "max_equity")} | {"manual": bool(t.get("manual"))}
            for t in TIERS]
