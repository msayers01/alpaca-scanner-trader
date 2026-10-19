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
                       "no day trades, quarter-Kelly shaded down because estimation error dominates at this size.",
        "fractional": True,
        "risk": {"kelly_multiplier": 0.20, "max_risk_per_trade": 0.05, "max_position_pct": 0.30,
                 "max_options_allocation": 0.40, "max_open_positions": 3, "max_per_underlying": 1,
                 "max_trades_per_day": 3, "max_daily_loss": 0.06, "max_drawdown": 0.25, "allow_daytrades": "never",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge"],
    },
    {
        "tier": "small",
        "label": "Small ($2k – $10k)",
        "min_equity": 2_000,
        "max_equity": 10_000,
        "description": "Adds flow-following and post-earnings drift option trades. Swing horizon only (no PDT risk).",
        "fractional": True,
        "risk": {"kelly_multiplier": 0.25, "max_risk_per_trade": 0.03, "max_position_pct": 0.15,
                 "max_options_allocation": 0.50, "max_open_positions": 5, "max_per_underlying": 1,
                 "max_trades_per_day": 5, "max_daily_loss": 0.05, "max_drawdown": 0.20, "allow_daytrades": "never",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge", "flow_follow", "earnings_drift"],
    },
    {
        "tier": "growth",
        "label": "Growth ($10k – $25k)",
        "min_equity": 10_000,
        "max_equity": 25_000,
        "description": "All swing strategies including the LLM analyst. Day trades only within the broker allowance.",
        "fractional": True,
        "risk": {"kelly_multiplier": 0.25, "max_risk_per_trade": 0.02, "max_position_pct": 0.10,
                 "max_options_allocation": 0.50, "max_open_positions": 8, "max_per_underlying": 1,
                 "max_trades_per_day": 8, "max_daily_loss": 0.04, "max_drawdown": 0.18, "allow_daytrades": "auto",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge", "flow_follow", "earnings_drift", "llm_analyst"],
    },
    {
        "tier": "pro",
        "label": "Pro (≥ $25k)",
        "min_equity": 25_000,
        "max_equity": None,
        "description": "Everything, including intraday opening-range breakouts (pattern-day-trader capacity).",
        "fractional": False,
        "risk": {"kelly_multiplier": 0.30, "max_risk_per_trade": 0.015, "max_position_pct": 0.08,
                 "max_options_allocation": 0.50, "max_open_positions": 12, "max_per_underlying": 2,
                 "max_trades_per_day": 15, "max_daily_loss": 0.03, "max_drawdown": 0.15, "allow_daytrades": "auto",
                 "min_edge_score": 1.0},
        "strategies": ["tsmom_equity", "meanrev_etf", "options_edge", "flow_follow", "earnings_drift", "llm_analyst",
                       "orb_intraday"],
    },
]
TIER_BY_ID = {t["tier"]: t for t in TIERS}


def tier_for_equity(equity: float) -> str:
    for t in TIERS:
        if equity >= t["min_equity"] and (t["max_equity"] is None or equity < t["max_equity"]):
            return t["tier"]
    return "micro"


def presets_wire() -> list[dict[str, Any]]:
    return [{k: t[k] for k in ("tier", "label", "description", "min_equity", "max_equity")} for t in TIERS]
