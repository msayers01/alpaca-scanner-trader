"""Long call / long put scanner. Ranks contracts by physical-measure expected value at the planned
exit horizon, net of spread costs, with growth-optimal (Kelly) sizing. See docs/QUANT.md §5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from datetime import date, timedelta
from typing import Any

import numpy as np

from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.types import OptionContract, ParamSpec, apply_params
from vantage.core.util import now_ms, sanitize
from vantage.quant import bs, option_ev
from vantage.quant import vol as qvol

log = logging.getLogger(__name__)

SCAN_SCHEMA: list[ParamSpec] = [
    ParamSpec("source", "enum", "universe", "Symbols", "Which symbol list to scan", "Universe",
              options=["universe", "watchlist", "both"]),
    ParamSpec("side", "enum", "auto", "Side", "auto = follow the ensemble forecast; both = rank calls and puts",
              "Universe", options=["auto", "calls", "puts", "both"]),
    ParamSpec("min_abs_z", "float", 0.0, "Min |forecast z|", "Skip underlyings with a weaker ensemble signal (auto side)",
              "Universe", min=0, max=4, step=0.1),
    ParamSpec("min_dte", "int", 7, "Min DTE", "Minimum days to expiration", "Contract", min=0, max=365, step=1),
    ParamSpec("max_dte", "int", 60, "Max DTE", "Maximum days to expiration", "Contract", min=1, max=730, step=1),
    ParamSpec("min_delta", "float", 0.25, "Min |delta|", "", "Contract", min=0.01, max=0.99, step=0.01),
    ParamSpec("max_delta", "float", 0.70, "Max |delta|", "", "Contract", min=0.01, max=0.99, step=0.01),
    ParamSpec("max_premium", "float", 0.0, "Max premium ($/contract)", "0 = no limit; premium x100", "Contract",
              min=0, max=100000, step=10),
    ParamSpec("max_spread_pct", "float", 0.10, "Max spread %", "Bid-ask spread as fraction of mid", "Liquidity",
              min=0.005, max=1.0, step=0.005),
    ParamSpec("min_open_interest", "int", 100, "Min open interest", "", "Liquidity", min=0, max=100000, step=10),
    ParamSpec("min_volume", "int", 10, "Min volume (today)", "", "Liquidity", min=0, max=100000, step=10),
    ParamSpec("horizon_days", "float", 10.0, "Holding horizon (trading days)", "Planned exit horizon for EV",
              "Model", min=1, max=60, step=1),
    ParamSpec("aggression", "float", 0.25, "Fill aggression", "Entry at mid + a*half-spread, exit at mid - a*half-spread",
              "Model", min=0, max=1, step=0.05),
    ParamSpec("iv_mode", "enum", "constant", "IV at exit", "constant = IV unchanged; forecast = IV mean-reverts toward "
              "forecast vol x VRP", "Model", options=["constant", "forecast"]),
    ParamSpec("earnings_iv_crush", "float", 0.30, "Earnings IV crush", "Fractional IV drop applied if earnings occur "
              "before the planned exit", "Model", min=0, max=0.9, step=0.05),
    ParamSpec("avoid_earnings", "bool", False, "Exclude earnings inside expiry", "", "Model"),
    ParamSpec("min_ev", "float", 0.0, "Min EV %", "Minimum expected return on premium", "Output", min=-1, max=5, step=0.01),
    ParamSpec("top_n", "int", 60, "Max results", "", "Output", min=5, max=500, step=5),
    ParamSpec("auto_interval_s", "int", 180, "Auto-scan every (s)", "0 disables background scanning", "Output",
              min=0, max=3600, step=30),
]


class OptionsScanner:
    def __init__(self, market: Any, options: Any, signals: Any, calendar: Any, flow: Any, bus: Bus,
                 universe_fn: Any, watchlist_fn: Any, risk_free: float = 0.04, mode: str = "sim"):
        self.market = market
        self.options = options
        self.signals = signals
        self.calendar = calendar
        self.flow = flow
        self.bus = bus
        self.universe_fn = universe_fn
        self.watchlist_fn = watchlist_fn
        self.r = risk_free
        self.mode = mode
        self.params = apply_params(SCAN_SCHEMA, None)
        if mode == "sim":
            self.params["min_open_interest"] = 50
        self.last: dict[str, Any] = {"t": 0, "duration_ms": 0, "params": self.params, "candidates": [], "errors": []}
        self._task: asyncio.Task[None] | None = None
        self._sem = asyncio.Semaphore(4)
        self._run_lock = asyncio.Lock()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scanner")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        await asyncio.sleep(8)
        while True:
            iv = int(self.params.get("auto_interval_s", 180))
            if iv > 0 and (clock.market_state().is_open or self.mode == "sim"):
                try:
                    await self.run()
                except Exception:  # noqa: BLE001
                    log.exception("auto scan failed")
            await asyncio.sleep(max(iv, 30))

    # ------------------------------------------------------------------ public
    async def run(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._run_lock:
            if overrides:
                self.params = apply_params(SCAN_SCHEMA, {**self.params, **overrides})
            p = dict(self.params)
            t0 = time.perf_counter()
            src = p["source"]
            syms: list[str] = []
            if src in ("universe", "both"):
                syms += self.universe_fn()
            if src in ("watchlist", "both"):
                syms += self.watchlist_fn()
            syms = list(dict.fromkeys(syms))
            errors: list[str] = []
            results = await asyncio.gather(*(self._guarded(u, p, errors) for u in syms))
            cands = [c for lst in results for c in lst]
            cands.sort(key=lambda c: -c["edge_score"])
            cands = cands[: int(p["top_n"])]
            self.last = sanitize({
                "t": now_ms(),
                "duration_ms": int((time.perf_counter() - t0) * 1000),
                "params": p,
                "candidates": cands,
                "errors": errors[:20],
            })
            self.bus.publish("scanner", self.last)
            return self.last

    async def _guarded(self, u: str, p: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
        async with self._sem:
            try:
                return await self.evaluate(u, p)
            except Exception as e:  # noqa: BLE001
                log.debug("scan %s failed", u, exc_info=True)
                errors.append(f"{u}: {e}")
                return []

    async def evaluate(self, underlying: str, p: dict[str, Any] | None = None, side: str | None = None,
                       forecast: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Score all eligible contracts of one underlying. ``side`` overrides p['side']."""
        p = apply_params(SCAN_SCHEMA, p or self.params)
        u = underlying.upper()
        spot = await self.market.ensure_price(u)
        if not spot:
            return []
        h_days = float(p["horizon_days"])
        fc = forecast or self.signals.forecast(u, h_days)
        side = side or p["side"]
        if side == "auto":
            if abs(fc["z"]) < p["min_abs_z"] or fc["z"] == 0:
                return []
            side = "calls" if fc["z"] > 0 else "puts"
        today = clock.today_et()
        chain = await self.options.chain(u, today + timedelta(days=int(p["min_dte"])), today + timedelta(days=int(p["max_dte"])))
        if not chain:
            return []
        iv_hist = await self.options.iv_history(u)
        atm_now = self.signals.atm_iv.get(u)
        if atm_now is None:
            from vantage.data.options import atm_iv

            atm_now = atm_iv(chain, spot, 30)
        self.options.record_iv(u, atm_now, self.signals.realized_vol.get(u))
        ivr = qvol.iv_rank(atm_now, iv_hist) if atm_now and len(iv_hist) >= 20 else None
        dte_earn = self.calendar.days_to_earnings(u) if self.calendar else None
        flow = self.flow.flow_score(u) if self.flow else None
        return await asyncio.to_thread(_score_chain, chain, u, spot, fc, side, p, self.r, ivr, atm_now, dte_earn, flow)


def _score_chain(chain: list[OptionContract], u: str, spot: float, fc: dict[str, Any], side: str, p: dict[str, Any],
                 r: float, ivr: float | None, atm_now: float | None, dte_earn: int | None,
                 flow: dict[str, Any] | None, today: date | None = None) -> list[dict[str, Any]]:
    today = today or clock.today_et()
    want_call = side in ("calls", "both")
    want_put = side in ("puts", "both")
    rows: list[OptionContract] = []
    for c in chain:
        if (c.type == "call" and not want_call) or (c.type == "put" and not want_put):
            continue
        if c.bid <= 0 or c.ask <= 0:
            continue
        if c.spread_pct > p["max_spread_pct"]:
            continue
        if c.open_interest < p["min_open_interest"] or c.volume < p["min_volume"]:
            continue
        if p["max_premium"] > 0 and c.ask * 100 > p["max_premium"]:
            continue
        dte = c.dte(today)
        if dte < p["min_dte"] or dte > p["max_dte"]:
            continue
        if p["avoid_earnings"] and dte_earn is not None and 0 <= dte_earn <= dte:
            continue
        rows.append(c)
    if not rows:
        return []
    S = spot
    K = np.array([c.strike for c in rows])
    T = np.array([max(c.dte(today), 0.25) / 365 for c in rows])
    is_call = np.array([c.type == "call" for c in rows])
    mid = np.array([c.mid for c in rows])
    half = np.array([(c.ask - c.bid) / 2 for c in rows])
    iv = np.array([c.iv if c.iv and c.iv > 0 else np.nan for c in rows])
    miss = ~np.isfinite(iv)
    if miss.any():
        iv[miss] = bs.implied_vol(mid[miss], S, K[miss], T[miss], r, 0.0, is_call[miss])
    ok = np.isfinite(iv) & (iv > 0.01) & (iv < 5)
    delta = np.array([c.delta if c.delta is not None else np.nan for c in rows])
    dmiss = ~np.isfinite(delta) & ok
    if dmiss.any():
        g = bs.bs_greeks(S, K[dmiss], T[dmiss], r, 0.0, iv[dmiss], is_call[dmiss])
        delta[dmiss] = g["delta"]
    absd = np.abs(delta)
    ok &= (absd >= p["min_delta"]) & (absd <= p["max_delta"])
    if not ok.any():
        return []
    idx = np.nonzero(ok)[0]
    a = float(p["aggression"])
    entry = mid[idx] + a * half[idx]
    exit_cost = np.clip((a * half[idx]) / np.maximum(mid[idx], 0.01), 0, 0.9)
    sigma_p = float(fc.get("vol") or 0.3)
    mu = float(fc.get("mu_annual") or 0.0)
    h_years = float(p["horizon_days"]) * 1.45 / 365  # trading -> calendar days
    iv_target = None
    if p["iv_mode"] == "forecast":
        iv_target = np.full(len(idx), sigma_p * 1.1)
    crush = 0.0
    if dte_earn is not None and 0 <= dte_earn <= p["horizon_days"] * 1.45:
        crush = float(p["earnings_iv_crush"])
    res = option_ev.evaluate_long_options(
        S, K[idx], T[idx], is_call[idx], entry, exit_cost, r, 0.0, iv[idx], mu, sigma_p, h_years,
        iv_target=iv_target, iv_crush=crush,
    )
    out = []
    for j, i in enumerate(idx):
        c = rows[i]
        ev = float(res["ev_pct"][j])
        if not math.isfinite(ev) or ev < p["min_ev"]:
            continue
        kelly = float(res["kelly"][j])
        sp = float(c.spread_pct)
        liq = math.exp(-3 * sp) * min(1.0, c.open_interest / 500) ** 0.25
        edge = 100 * ev * liq * (1 if kelly > 0 else 0.25)
        reasons = []
        fz = fc.get("z", 0.0)
        reasons.append(f"Forecast {fc.get('exp_return', 0) * 100:+.2f}% over {p['horizon_days']:.0f}d "
                       f"(z {fz:+.2f}, P(up) {fc.get('prob_up', 0.5) * 100:.0f}%)")
        ivrv = float(iv[i]) / sigma_p if sigma_p > 0 else None
        if ivrv:
            reasons.append(f"IV {iv[i] * 100:.1f}% vs forecast vol {sigma_p * 100:.1f}% (ratio {ivrv:.2f})")
        if ivr is not None:
            reasons.append(f"IV rank {ivr * 100:.0f}")
        reasons.append(f"Spread {sp * 100:.1f}% of mid; OI {c.open_interest:.0f}; vol {c.volume:.0f}")
        if crush:
            reasons.append(f"Earnings in {dte_earn}d before planned exit — {crush * 100:.0f}% IV crush modelled")
        elif dte_earn is not None and 0 <= dte_earn <= c.dte(today):
            reasons.append(f"Earnings in {dte_earn}d (inside expiry, after planned exit)")
        if flow and flow.get("n"):
            reasons.append(f"Options flow imbalance {flow['score']:+.2f} ({flow['n']} prints)")
        theta = c.theta
        if theta is None:
            theta = float(bs.bs_greeks(S, c.strike, T[i], r, 0.0, iv[i], c.type == "call")["theta"])
        out.append({
            "contract": c.symbol,
            "underlying": u,
            "type": c.type,
            "strike": c.strike,
            "expiration": c.expiration.isoformat(),
            "dte": c.dte(today),
            "spot": S,
            "bid": c.bid,
            "ask": c.ask,
            "mid": round(c.mid, 4),
            "spread_pct": round(sp, 4),
            "volume": c.volume,
            "open_interest": c.open_interest,
            "iv": round(float(iv[i]), 4),
            "delta": round(float(delta[i]), 4),
            "theta": round(float(theta), 4),
            "iv_rank": round(ivr, 3) if ivr is not None else None,
            "iv_rv_ratio": round(ivrv, 3) if ivrv else None,
            "forecast_return": fc.get("exp_return", 0.0),
            "forecast_vol": sigma_p,
            "ev_pct": round(ev, 4),
            "pop": round(float(res["pop"][j]), 4),
            "kelly": round(kelly, 4),
            "breakeven": round(float(res["breakeven"][j]), 4),
            "edge_score": round(edge, 3),
            "entry_price": round(float(entry[j]), 4),
            "reasons": reasons,
        })
    return out
