"""Daily-bar event-driven backtester that runs the SAME Strategy objects (docs/QUANT.md §10).

Simplifications (reported in ``notes``): daily bars only (intraday strategies are not supported),
fills at the next session's open with slippage + commission, options are priced synthetically
with Black-Scholes using forecast vol × an IV/RV ratio and a fixed spread, and news / options-flow /
earnings-calendar signals are unavailable historically.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from vantage.core import clock
from vantage.core.types import (
    Bar,
    ManagedPosition,
    OptionContract,
    Signal,
    TradeIdea,
    apply_params,
    make_occ,
    parse_occ,
)
from vantage.execution.risk import RiskManager
from vantage.quant import bs, stats
from vantage.quant import vol as qvol
from vantage.scanner.options_scanner import SCAN_SCHEMA, _score_chain
from vantage.signals.base import SignalContext, SymbolData, squash
from vantage.signals.engine import SignalEngine
from vantage.signals.library import MODELS
from vantage.strategies.base import StrategyContext
from vantage.strategies.library import REGISTRY
from vantage.strategies.presets import TIER_BY_ID, tier_for_equity

DAY_MS = 86_400_000
WARMUP = 260
SIZING_MODES = ("equal_weight", "live")


@dataclass
class BTPos:
    symbol: str
    underlying: str
    asset_class: str
    qty: float
    entry_price: float
    entry_i: int
    strategy_id: str
    idea: TradeIdea
    peak_plpc: float = 0.0
    strike: float = 0.0
    is_call: bool = True
    expiry: date | None = None
    last_price: float = 0.0


class _Market:
    def __init__(self, data: dict[str, list[Bar]]):
        self.data = data
        self.i: dict[str, int] = {}
        self.today: date | None = None

    def price(self, s: str) -> float | None:
        bars = self.data.get(s)
        i = self.i.get(s)
        return bars[i].c if bars and i is not None and i >= 0 else None

    async def ensure_price(self, s: str) -> float | None:
        return self.price(s)

    async def daily_bars(self, s: str, days: int = 500, max_age_s: float = 0) -> list[Bar]:
        bars = self.data.get(s, [])
        i = self.i.get(s, -1)
        return bars[max(0, i + 1 - days) : i + 1]

    @staticmethod
    def ohlcv(bars: list[Bar]) -> dict[str, np.ndarray]:
        from vantage.data.market import MarketDataService

        return MarketDataService.ohlcv(bars)


class _Scanner:
    def __init__(self, market: _Market, signals: SignalEngine, r: float, iv_rv: float, spread: float):
        self.market = market
        self.signals = signals
        self.r = r
        self.iv_rv = iv_rv
        self.spread = spread
        self.today: date = date.today()

    def synth_chain(self, u: str, spot: float, sigma: float, min_dte: int, max_dte: int) -> list[OptionContract]:
        out = []
        d = self.today
        exps = []
        while (d - self.today).days <= max_dte:
            d += timedelta(days=1)
            if d.weekday() == 4 and (d - self.today).days >= min_dte:
                exps.append(d)
        inc = 1.0 if spot < 100 else 2.5 if spot < 300 else 5.0
        center = round(spot / inc) * inc
        strikes = np.array([center + inc * k for k in range(-12, 13) if center + inc * k > 0])
        for e in exps:
            T = (e - self.today).days / 365
            m = np.log(strikes / spot)
            iv = np.clip(sigma * self.iv_rv * (1 - 0.6 * m + 1.5 * m**2), 0.05, 3)
            for is_call in (True, False):
                px = np.asarray(bs.bs_price(spot, strikes, T, self.r, 0.0, iv, is_call))
                g = bs.bs_greeks(spot, strikes, T, self.r, 0.0, iv, is_call)
                for j, k in enumerate(strikes):
                    mid = float(px[j])
                    if mid < 0.05:
                        continue
                    half = max(0.01, mid * self.spread / 2)
                    out.append(OptionContract(make_occ(u, e, "call" if is_call else "put", float(k)), u,
                                              "call" if is_call else "put", float(k), e, round(mid - half, 2),
                                              round(mid + half, 2), mid, 1000, 5000, float(iv[j]), float(g["delta"][j]),
                                              float(g["gamma"][j]), float(g["theta"][j]), float(g["vega"][j])))
        return out

    async def evaluate(self, underlying: str, p: dict[str, Any] | None = None, side: str | None = None,
                       forecast: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        p = apply_params(SCAN_SCHEMA, {**(p or {}), "min_open_interest": 0, "min_volume": 0})
        spot = self.market.price(underlying)
        if not spot:
            return []
        fc = forecast or self.signals.forecast(underlying, p["horizon_days"])
        side = side or ("calls" if fc["z"] > 0 else "puts")
        sigma = self.signals.vol_forecast.get(underlying) or 0.3
        chain = self.synth_chain(underlying, spot, sigma, int(p["min_dte"]), int(p["max_dte"]))
        return _score_chain(chain, underlying, spot, fc, side, p, self.r, None, sigma * self.iv_rv, None, None,
                            today=self.today)


def _bar_date(b: Bar) -> date:
    return datetime.fromtimestamp(b.t / 1000, tz=UTC).astimezone(clock.ET).date()


async def run_backtest(c: Any, strategy_id: str, symbols: list[str], start: date, end: date, params: dict[str, Any],
                       initial_equity: float, n_trials: int = 1, iv_rv: float = 1.10,
                       sizing: str = "equal_weight") -> dict[str, Any]:
    cls = REGISTRY[strategy_id]
    if cls.backtest_support == "none":
        raise ValueError(f"{cls.name} can't be backtested: {cls.backtest_note}")
    if sizing not in SIZING_MODES:
        raise ValueError(f"sizing must be one of {', '.join(SIZING_MODES)}")
    fetch_from = start - timedelta(days=int(WARMUP * 1.5) + 10)
    t0 = clock.to_ms(clock.et_dt(fetch_from, clock.OPEN))
    t1 = clock.to_ms(clock.et_dt(end, clock.CLOSE))
    want = list(dict.fromkeys([*symbols, "SPY"]))
    data = await c.broker.get_bars_multi(want, "1Day", t0, t1)
    risk_cfg = dict(c.autotrader.risk.cfg)
    return await asyncio.to_thread(_run_sync, strategy_id, symbols, start, end, params, initial_equity, data, risk_cfg,
                                   c.settings.risk_free_rate, n_trials, iv_rv, sizing)


def _run_sync(*a: Any) -> dict[str, Any]:
    return asyncio.run(_simulate(*a))


async def _simulate(strategy_id: str, symbols: list[str], start: date, end: date, params: dict[str, Any],
                    initial_equity: float, data: dict[str, list[Bar]], risk_cfg: dict[str, Any], r: float,
                    n_trials: int, iv_rv: float = 1.10, sizing: str = "equal_weight") -> dict[str, Any]:
    notes = ["Daily bars; fills at next open with 5 bps slippage (equities) or half-spread (options)."]
    strat = REGISTRY[strategy_id](params)
    strat.use_llm_gate = False
    if strat.asset_class == "option":
        notes.append(f"Options priced synthetically: Black-Scholes with IV = forecast vol × {iv_rv:.2f} and a 4% spread. "
                     "Treat option results as indicative only.")
    notes.append("News, options-flow and earnings-calendar signals are not available historically and are omitted.")
    spy = data.get("SPY", [])
    dates = sorted({_bar_date(b) for b in spy if start <= _bar_date(b) <= end})
    if not dates:
        all_d = sorted({_bar_date(b) for s in symbols for b in data.get(s, []) if start <= _bar_date(b) <= end})
        dates = all_d
    if len(dates) < 20:
        raise ValueError("not enough data in the selected range")
    idx = {s: {_bar_date(b): i for i, b in enumerate(data.get(s, []))} for s in want_syms(symbols)}
    market = _Market(data)
    engine = SignalEngine(market, None, None, None, None, _NullStore(), _NullBus(), "backtest", lambda: symbols)
    scanner = _Scanner(market, engine, r, iv_rv, 0.04)
    funnel: dict[str, int] = {"ideas": 0, "entered": 0}
    reject_reasons: dict[str, int] = {}
    cfg = {**risk_cfg, "max_trades_per_day": 1000}
    n_sym = max(1, len(symbols))
    if sizing == "equal_weight":
        # Signal-quality view: each symbol gets an equal slice of capital (shares) or an equal premium budget
        # (options, capped at 20% of equity per trade) instead of the live per-trade caps, which on a small
        # single-symbol test keep the curve almost flat.
        slice_ = 1.0 / n_sym
        budget = slice_ if strat.asset_class == "equity" else min(slice_, 0.20)
        cfg.update(kelly_multiplier=1.0, max_open_positions=max(n_sym, 1), max_per_underlying=1,
                   max_new_per_cycle=n_sym, min_one_contract=True, max_position_pct=budget,
                   max_risk_per_trade=budget, max_options_allocation=1.0)
        notes.append(f"Sizing: equal weight — up to {slice_:.0%} of equity per symbol"
                     + (" in shares." if strat.asset_class == "equity" else f" (≤ {budget:.0%} premium per option trade)."))
    else:
        notes.append("Sizing: live risk settings (Kelly multiplier, per-trade risk and position caps from the "
                     "Auto-trader page) — what the bot would actually do.")
    risk = RiskManager(cfg, "sim")
    cash = initial_equity
    positions: dict[str, BTPos] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[int, float]] = []
    exposure_days = 0
    commission = 0.65  # per option contract
    slip = 0.0005

    def opt_value(p: BTPos, spot: float, d: date, sigma: float) -> float:
        T = max((p.expiry - d).days, 0) / 365 if p.expiry else 0
        if T <= 0:
            return max(0.0, (spot - p.strike) if p.is_call else (p.strike - spot))
        return float(bs.bs_price(spot, p.strike, T, r, 0.0, sigma * iv_rv, p.is_call))

    def mark(p: BTPos, d: date) -> float:
        spot = market.price(p.underlying) or p.last_price
        if p.asset_class == "equity":
            return spot
        return opt_value(p, spot, d, engine.vol_forecast.get(p.underlying, 0.3))

    for k, d in enumerate(dates):
        for s in want_syms(symbols):
            if d in idx[s]:
                market.i[s] = idx[s][d]
        scanner.today = d
        # ---- signals as of today's close
        ctx_sig = SignalContext(now=clock.to_ms(clock.et_dt(d, clock.CLOSE)), session_open=False)
        for s in symbols:
            i = market.i.get(s)
            if i is None or i < WARMUP // 2:
                continue
            bars = data[s][max(0, i - 400) : i + 1]
            arr = _Market.ohlcv(bars)
            o, h, lo, cc = arr["o"], arr["h"], arr["l"], arr["c"]
            yz = qvol.yang_zhang(o, h, lo, cc, 20)
            ew = qvol.ewma_vol(np.diff(np.log(cc[-120:])))
            vals = [v for v in (yz, ew) if v is not None and math.isfinite(v)]
            if vals:
                engine.vol_forecast[s] = float(np.mean(vals))
                engine.realized_vol[s] = float(vals[0])
            sd = SymbolData(s, float(cc[-1]), arr, vol_forecast=engine.vol_forecast.get(s))
            for m in MODELS:
                if m.kind != "daily" or m.id in ("rel_strength", "vol_premium"):
                    continue
                try:
                    out = m.fn(sd, ctx_sig)
                except Exception:  # noqa: BLE001
                    out = None
                if out:
                    engine.latest[(s, m.id)] = Signal(m.id, s, squash(out[0]), out[1], m.horizon_days, ctx_sig.now, out[2])
        # ---- mark-to-market
        pos_value = sum(mark(p, d) * p.qty * (100 if p.asset_class == "option" else 1) for p in positions.values())
        equity = cash + pos_value
        equity_curve.append((clock.to_ms(clock.et_dt(d, clock.CLOSE)), equity))
        if positions:
            exposure_days += 1
        if k == len(dates) - 1:
            break
        nd = dates[k + 1]
        account = {"equity": equity, "last_equity": equity, "cash": cash, "buying_power": cash,
                   "options_buying_power": cash, "daytrade_count": 0, "pattern_day_trader": False}
        tier = tier_for_equity(equity)
        pos_dicts = [{"symbol": p.symbol, "underlying": p.underlying, "asset_class": p.asset_class,
                      "market_value": mark(p, d) * p.qty * (100 if p.asset_class == "option" else 1)}
                     for p in positions.values()]
        ms = clock.MarketState(True, "regular", 0, 0, ctx_sig.now, 120.0, 240.0)
        ctx = StrategyContext("backtest", market, None, engine, scanner, None, None, None, None, account, pos_dicts,
                              tier, risk.cfg, symbols, ms, now=ctx_sig.now, backtest=True)
        # ---- exits (execute at next open)
        for sym, p in list(positions.items()):
            cur = mark(p, d)
            plpc = cur / p.entry_price - 1 if p.entry_price else 0.0
            p.peak_plpc = max(p.peak_plpc, plpc)
            mp = ManagedPosition(sym, p.underlying, p.asset_class, p.qty, p.entry_price, cur, cur * p.qty, plpc,  # type: ignore[arg-type]
                                 strategy_id, p.idea.direction, clock.to_ms(clock.et_dt(dates[p.entry_i], clock.OPEN)),
                                 p.peak_plpc, p.idea.stop_pct, p.idea.target_pct, p.idea.trail_pct,
                                 p.idea.trail_activate_pct, p.idea.time_stop_days, p.idea.min_dte_exit, p.expiry)
            sig = await strat.manage(ctx, mp)
            expired = p.expiry is not None and p.expiry <= nd
            if sig is None and not expired:
                continue
            nidx = idx[p.underlying].get(nd)
            nopen = data[p.underlying][nidx].o if nidx is not None else market.price(p.underlying) or cur
            if p.asset_class == "equity":
                px = nopen * (1 - slip)
                cash += px * p.qty
            else:
                v = opt_value(p, nopen, nd, engine.vol_forecast.get(p.underlying, 0.3))
                px = max(0.0, v * (1 - 0.02))
                cash += px * p.qty * 100 - commission * p.qty
            mult = 100 if p.asset_class == "option" else 1
            trades.append({"id": f"bt{len(trades)}", "symbol": sym, "strategy_id": strategy_id, "asset_class": p.asset_class,
                           "direction": p.idea.direction, "entry_t": clock.to_ms(clock.et_dt(dates[p.entry_i], clock.OPEN)),
                           "exit_t": clock.to_ms(clock.et_dt(nd, clock.OPEN)), "entry_price": round(p.entry_price, 4),
                           "exit_price": round(px, 4), "qty": p.qty, "pnl": round((px - p.entry_price) * p.qty * mult, 2),
                           "pnl_pct": round(px / p.entry_price - 1, 4) if p.entry_price else None,
                           "reason_exit": sig.reason if sig else "expired"})
            del positions[sym]
        # ---- entries (execute at next open)
        if d < start:
            continue
        ideas = await strat.generate(ctx)
        funnel["ideas"] += len(ideas)
        ideas.sort(key=lambda i: -i.edge_score)
        new = 0
        for idea in ideas:
            if new >= risk.cfg["max_new_per_cycle"] or idea.symbol in positions:
                continue
            if any(p.underlying == idea.underlying for p in positions.values()):
                continue
            plan, why = risk.size_and_check(idea, account, pos_dicts, set(), ms, None, False, False,
                                             TIER_BY_ID[tier]["fractional"])
            if plan is None:
                key = why.split("(")[0].strip()[:60]
                reject_reasons[key] = reject_reasons.get(key, 0) + 1
                continue
            nidx = idx[idea.underlying].get(nd)
            if nidx is None:
                continue
            nopen = data[idea.underlying][nidx].o
            if plan.asset_class == "equity":
                px = nopen * (1 + slip)
                qty = plan.qty if plan.qty else 0
                qty = min(qty, cash / px)
                if qty * px < 1:
                    continue
                cash -= px * qty
                positions[idea.symbol] = BTPos(idea.symbol, idea.underlying, "equity", qty, px, k + 1, strategy_id, idea,
                                               last_price=px)
            else:
                occ = parse_occ(idea.symbol)
                if occ is None:
                    continue
                tmp = BTPos(idea.symbol, idea.underlying, "option", plan.qty or 0, 0, k + 1, strategy_id, idea,
                            strike=occ[3], is_call=occ[2] == "call", expiry=occ[1])
                v = opt_value(tmp, nopen, nd, engine.vol_forecast.get(idea.underlying, 0.3))
                px = v * 1.02
                n = int(min(plan.qty or 0, cash // (px * 100 + commission))) if px > 0 else 0
                if n < 1:
                    continue
                cash -= px * 100 * n + commission * n
                tmp.qty, tmp.entry_price = n, px
                positions[idea.symbol] = tmp
            new += 1
            risk.record_entry()

    eq = np.array([v for _, v in equity_curve])
    t = [ts for ts, _ in equity_curve]
    bench = []
    if spy:
        sp = {_bar_date(b): b.c for b in spy}
        base = next((sp[d] for d in dates if d in sp), None)
        if base:
            bench = [{"t": t[i], "v": round(initial_equity * sp[d] / base, 2)} for i, d in enumerate(dates) if d in sp]
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])
    st = stats.sharpe_stats(rets)
    pnls = [tr["pnl"] for tr in trades]
    n = max(int(st.get("n", len(rets))), 2)
    metrics = {
        "cagr": stats.cagr(eq), "sharpe": stats.sharpe(rets), "sortino": stats.sortino(rets),
        "max_drawdown": stats.max_drawdown(eq), "calmar": stats.calmar(eq),
        "win_rate": stats.win_rate(np.array(pnls)) if pnls else 0.0,
        "profit_factor": stats.profit_factor(np.array(pnls)) if pnls else 0.0,
        "trades": len(trades), "exposure": exposure_days / max(1, len(dates)),
        "psr": stats.probabilistic_sharpe_ratio(st["sr"], n, st["skew"], st["kurt"], 0.0),
        "dsr": stats.deflated_sharpe_ratio(st["sr"], n, st["skew"], st["kurt"], max(1, n_trials), 1.0 / n),
        "n_trials": n_trials,
    }
    funnel["entered"] = len(trades) + len(positions)
    top = sorted(reject_reasons.items(), key=lambda kv: -kv[1])[:3]
    notes.append(f"Funnel: {funnel['ideas']} ideas generated, {funnel['entered']} entered" + (
        "; top risk rejections: " + "; ".join(f"{k} ×{v}" for k, v in top) if top else "") + ".")
    if funnel["ideas"] == 0:
        notes.append("No ideas passed the strategy's entry filters (e.g. EV after costs never cleared the minimum) — "
                     "the strategy correctly stayed flat. Try a lower IV/RV assumption or looser thresholds.")
    if not trades and not positions:
        metrics["psr"] = 0.0
        metrics["dsr"] = 0.0
    if n_trials > 1:
        notes.append(f"DSR deflates for {n_trials} distinct parameter/symbol sets tried for this strategy.")
    return {"strategy_id": strategy_id, "sizing": sizing, "funnel": {**funnel, "top_rejections": dict(top)},
            "equity": [{"t": a, "v": round(b, 2)} for a, b in equity_curve],
            "benchmark": bench, "metrics": {k: (float(v) if isinstance(v, (int, float, np.floating)) and math.isfinite(v)
                                                else 0.0) for k, v in metrics.items()},
            "trades": trades[-500:], "notes": notes}


def want_syms(symbols: list[str]) -> list[str]:
    return list(dict.fromkeys([*symbols, "SPY"]))


class _NullStore:
    def write(self, *a: Any, **k: Any) -> None:
        return None

    async def query(self, *a: Any, **k: Any) -> list[Any]:
        return []


class _NullBus:
    def publish(self, *a: Any, **k: Any) -> None:
        return None
