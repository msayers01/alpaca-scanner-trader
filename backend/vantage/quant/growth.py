"""Small-account growth odds: probability of reaching a dollar goal before a dollar floor (docs/QUANT.md §7.1).

Each trade commits a fraction ``f`` of current equity with a stop ``s`` (fraction of the position) and a target of
``R·s`` (an R-multiple). Stops slip by ``slip·s`` on average (fast leveraged ETFs / options gap through stops) and
every round trip costs ``c`` (spread + fees, fraction of the position). With win rate ``p``:

    win : equity × (1 + f·(R·s − c))        loss: equity × (1 − f·(s·(1 + slip) + c))

THE KEY ANCHOR: with no edge (driftless price), a +R·s target is touched before a −s stop with probability
    p0 = 1 / (1 + R)        (gambler's ruin for a driftless walk)
so a 2.5R target is hit only ~28.6 % of the time by chance. Any real strategy edge is a few percentage points on
top of p0 — that is what the estimator asks for (``edge_pp``), instead of a free-form "win rate".

Growth is multiplicative; the expected log return per trade is g(f) = p·ln(1 + f·W) + (1 − p)·ln(1 − f·L) with
W = R·s − c and L = s·(1 + slip) + c, maximised at the Kelly fraction f* = (p·W − (1 − p)·L) / (W·L).
Betting more than ≈2·f* has negative growth even with a positive edge.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def baseline_win_rate(reward_r: float) -> float:
    """Chance a +R target is hit before a −1R stop with zero edge (driftless random walk)."""
    return 1.0 / (1.0 + reward_r)


def _wl(s: float, r: float, c: float, slip: float) -> tuple[float, float]:
    return r * s - c, s * (1.0 + slip) + c


def trade_multipliers(f: float, s: float, r: float, c: float, slip: float = 0.0) -> tuple[float, float]:
    """(equity multiplier on a win, on a loss)."""
    w, l_ = _wl(s, r, c, slip)
    return 1.0 + f * w, 1.0 - f * l_


def log_growth(p: float, f: float, s: float, r: float, c: float, slip: float = 0.0) -> float:
    up, down = trade_multipliers(f, s, r, c, slip)
    if down <= 0 or up <= 0:
        return -math.inf
    return p * math.log(up) + (1 - p) * math.log(down)


def kelly_fraction(p: float, s: float, r: float, c: float, slip: float = 0.0) -> float:
    """Growth-optimal fraction of equity per trade for the binary R-multiple bet (0 if no edge), capped at 1."""
    w, l_ = _wl(s, r, c, slip)
    if w <= 0 or l_ <= 0:
        return 0.0
    return float(max(0.0, min(1.0, (p * w - (1 - p) * l_) / (w * l_))))


def breakeven_win_rate(s: float, r: float, c: float, slip: float = 0.0) -> float:
    w, l_ = _wl(s, r, c, slip)
    return l_ / (w + l_) if w + l_ > 0 else 1.0


def goal_odds(
    start: float,
    goal: float,
    floor: float,
    position_frac: float,
    stop_frac: float,
    reward_r: float,
    win_rate: float | None = None,
    edge_pp: float = 0.05,
    edge_sd: float = 0.04,
    cost_frac: float = 0.0005,
    stop_slippage: float = 0.15,
    max_trades: int = 2000,
    sims: int = 20_000,
    seed: int = 7,
) -> dict[str, Any]:
    """Monte Carlo of independent trades until each path reaches ``goal`` or ``floor`` (or ``max_trades``).

    ``win_rate`` overrides the default ``baseline_win_rate(reward_r) + edge_pp``. ``edge_sd`` models uncertainty
    about the true edge: each simulated path draws its own win rate ~ N(p, edge_sd) (clipped), so a mean edge that
    is only an estimate produces the wider, fatter-tailed outcomes seen in practice.
    """
    if not (0 < floor < start < goal):
        raise ValueError("need 0 < floor < start < goal")
    if not (0 < position_frac <= 1 and 0 < stop_frac < 1 and reward_r > 0):
        raise ValueError("invalid trade parameters")
    p0 = baseline_win_rate(reward_r)
    p = float(win_rate) if win_rate is not None else min(0.99, max(0.0, p0 + edge_pp))
    if not 0 <= p <= 1:
        raise ValueError("win rate must be within [0, 1]")
    up, down = trade_multipliers(position_frac, stop_frac, reward_r, cost_frac, stop_slippage)
    if down <= 0:
        raise ValueError("a single loss would wipe out the account (position × stop ≥ 100 %)")
    rng = np.random.default_rng(seed)
    p_path = np.clip(p + edge_sd * rng.standard_normal(sims), 0.0, 0.99) if edge_sd > 0 else np.full(sims, p)
    eq = np.full(sims, float(start))
    status = np.zeros(sims, dtype=np.int8)  # 0 open, 1 goal, -1 floor
    when = np.full(sims, -1, dtype=np.int32)
    for t in range(1, max_trades + 1):
        live = np.nonzero(status == 0)[0]
        if live.size == 0:
            break
        wins = rng.random(live.size) < p_path[live]
        eq[live] *= np.where(wins, up, down)
        g = live[eq[live] >= goal]
        fl = live[eq[live] <= floor]
        status[g], when[g] = 1, t
        status[fl], when[fl] = -1, t
    goal_t = when[status == 1]
    f_star = kelly_fraction(p, stop_frac, reward_r, cost_frac, stop_slippage)
    w, l_ = _wl(stop_frac, reward_r, cost_frac, stop_slippage)
    return {
        "p_goal": float(np.mean(status == 1)),
        "p_floor": float(np.mean(status == -1)),
        "p_unresolved": float(np.mean(status == 0)),
        "median_trades_to_goal": float(np.median(goal_t)) if goal_t.size else None,
        "p10_trades_to_goal": float(np.percentile(goal_t, 10)) if goal_t.size else None,
        "win_rate": p,
        "edge_sd": edge_sd,
        "p_negative_edge": float(np.mean(p_path < breakeven_win_rate(stop_frac, reward_r, cost_frac, stop_slippage))),
        "baseline_win_rate": p0,
        "breakeven_win_rate": breakeven_win_rate(stop_frac, reward_r, cost_frac, stop_slippage),
        "win_multiplier": up,
        "loss_multiplier": down,
        "expectancy_pct": p * w - (1 - p) * l_,  # expected return per trade on the position
        "log_growth_per_trade": log_growth(p, position_frac, stop_frac, reward_r, cost_frac, stop_slippage),
        "kelly_fraction": f_star,
        "kelly_ratio": (position_frac / f_star) if f_star > 0 else None,
        "sims": sims,
        "max_trades": max_trades,
    }
