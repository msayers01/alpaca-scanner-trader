"""SignalEngine: computes every model for the active symbol set, tracks realised IC online and
produces the ensemble forecast (Grinold: E[r] = IC · σ · z). See docs/QUANT.md §6.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.stats import norm

from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import Signal
from vantage.core.util import now_ms
from vantage.quant import stats, vol
from vantage.signals.base import SignalContext, SymbolData, squash, unsquash
from vantage.signals.library import MODEL_BY_ID, MODELS

log = logging.getLogger(__name__)

PRIOR_IC = 0.03
AVG_CORR = 0.3
DAY_MS = 86_400_000


@dataclass
class ICStat:
    mean: float = math.nan
    sd: float = math.nan
    n_days: int = 0
    n_pairs: int = 0


class SignalEngine:
    def __init__(
        self,
        market: Any,
        options: Any,
        news: Any,
        flow: Any,
        calendar: Any,
        store: Store,
        bus: Bus,
        mode: str,
        symbols_fn: Callable[[], list[str]],
        intel: Any = None,
    ):
        self.market = market
        self.options = options
        self.news = news
        self.flow = flow
        self.calendar = calendar
        self.store = store
        self.bus = bus
        self.mode = mode
        self.symbols_fn = symbols_fn
        self.intel = intel
        self.latest: dict[tuple[str, str], Signal] = {}
        self.vol_forecast: dict[str, float] = {}
        self.realized_vol: dict[str, float] = {}
        self.atm_iv: dict[str, float] = {}
        self.ic: dict[str, ICStat] = defaultdict(ICStat)
        self._logged: dict[tuple[str, str], int] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._data: dict[str, SymbolData] = {}
        self.last_daily = 0.0
        self.last_run_ms: int | None = None
        self.enabled_models: set[str] = {m.id for m in MODELS}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop(), name="signals"))
        self._tasks.append(asyncio.create_task(self._ic_loop(), name="signals-ic"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    async def _loop(self) -> None:
        await asyncio.sleep(1.0)
        while True:
            t0 = time.monotonic()
            try:
                full = time.monotonic() - self.last_daily > 600 or not self._data
                await self.refresh(self.symbols_fn(), full=full)
                if full:
                    self.last_daily = time.monotonic()
            except Exception:  # noqa: BLE001
                log.exception("signal refresh failed")
            await asyncio.sleep(max(5.0, 30.0 - (time.monotonic() - t0)))

    async def _ic_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.update_ic()
            except Exception:  # noqa: BLE001
                log.exception("IC update failed")
            await asyncio.sleep(3600)

    # ------------------------------------------------------------------ data assembly
    async def _build(self, symbols: list[str], full: bool) -> None:
        if full or any(s not in self._data for s in symbols):
            daily = await self.market.daily_bars_multi(symbols, 400)
            arrays = {s: self.market.ohlcv(b) for s, b in daily.items() if len(b) >= 30}
            vols = await asyncio.to_thread(_forecast_vols, arrays)
            self.vol_forecast.update({s: v[0] for s, v in vols.items() if math.isfinite(v[0])})
            self.realized_vol.update({s: v[1] for s, v in vols.items() if math.isfinite(v[1])})
            xs = _cross_sectional(arrays)
            betas = _betas(arrays)
            for s, arr in arrays.items():
                d = self._data.get(s) or SymbolData(s, float(arr["c"][-1]), arr)
                d.daily = arr
                d.extra.update(xs.get(s, {}))
                d.extra["beta"] = betas.get(s, 1.0)
                self._data[s] = d
        for s in symbols:
            d = self._data.get(s)
            if d is None:
                continue
            p = self.market.price(s)
            if p:
                d.price = p
                # keep the last daily close in sync with the live price (today's partial bar)
                d.daily["c"][-1] = p if _is_today(int(d.daily["t"][-1]), self.mode) else d.daily["c"][-1]
            d.vol_forecast = self.vol_forecast.get(s)
            iv = self._atm_iv(s, p or d.price)
            if iv:
                d.atm_iv = iv
                self.atm_iv[s] = iv
            bars = await self.market.intraday_bars(s, 390)
            if bars:
                if self.mode != "sim":
                    so, _ = clock.session_bounds_ms(clock.last_session_date())
                    bars = [b for b in bars if b.t >= so]
                if bars:
                    d.intraday = self.market.ohlcv(bars)
                    v20 = d.daily["v"][-21:-1]
                    if len(v20) and np.mean(v20) > 0:
                        frac = max(len(bars) / 390, 0.05)
                        d.extra["rel_volume_today"] = float(np.sum(d.intraday["v"]) / (np.mean(v20) * frac))

    def _atm_iv(self, s: str, spot: float) -> float | None:
        if not self.options:
            return None
        from vantage.data.options import atm_iv

        contracts = [c for c in self.options.contracts.values() if c.underlying == s]
        return atm_iv(contracts, spot, target_dte=30) if contracts else None

    # ------------------------------------------------------------------ compute
    async def refresh(self, symbols: list[str], full: bool = True) -> None:
        symbols = sorted(set(symbols))
        await self._build(symbols, full)
        ms = clock.market_state()
        ctx = SignalContext(news=self.news, flow=self.flow, calendar=self.calendar, intel=self.intel, now=now_ms(),
                            session_open=ms.is_open or self.mode == "sim")
        now = ctx.now
        for s in symbols:
            d = self._data.get(s)
            if d is None:
                continue
            for m in MODELS:
                if m.id not in self.enabled_models:
                    continue
                if not full and m.kind == "daily" and (s, m.id) in self.latest:
                    continue
                try:
                    out = m.fn(d, ctx)
                except Exception as e:  # noqa: BLE001
                    log.debug("model %s failed on %s: %s", m.id, s, e)
                    continue
                if out is None:
                    continue
                z, conf, meta = out
                sig = Signal(m.id, s, squash(float(z)), float(max(0.0, min(1.0, conf))), m.horizon_days, now, meta)
                prev = self.latest.get((s, m.id))
                self.latest[(s, m.id)] = sig
                if prev is None or abs(prev.score - sig.score) > 0.05:
                    self.bus.publish("signal", sig)
                last_log = self._logged.get((s, m.id), 0)
                if now - last_log > 30 * 60_000 and d.price > 0:
                    self._logged[(s, m.id)] = now
                    self.store.write(
                        "INSERT INTO signals_log(mode,t,symbol,model,score,confidence,horizon_days,price) VALUES(?,?,?,?,?,?,?,?)",
                        (self.mode, now, s, m.id, sig.score, sig.confidence, m.horizon_days, d.price),
                    )
        self.last_run_ms = now

    # ------------------------------------------------------------------ IC tracking
    async def update_ic(self) -> None:
        since = now_ms() - 180 * DAY_MS
        rows = await self.store.query(
            "SELECT t, symbol, model, score, horizon_days, price FROM signals_log WHERE mode=? AND t>=? ORDER BY t",
            (self.mode, since),
        )
        if not rows:
            return
        res = await asyncio.to_thread(_compute_ic, rows, self.mode)
        for model, st in res.items():
            self.ic[model] = st

    # ------------------------------------------------------------------ queries
    def signals_for(self, symbol: str | None = None) -> list[dict[str, Any]]:
        out = []
        for (s, mid), sig in self.latest.items():
            if symbol is None or s == symbol:
                w = sig.to_wire()
                st = self.ic.get(mid)
                w["ic"] = st.mean if st and math.isfinite(st.mean) else None
                out.append(w)
        out.sort(key=lambda r: (r["symbol"], r["model"]))
        return out

    def model_weight(self, model_id: str) -> tuple[float, float]:
        """(weight multiplier, shrunk IC) for a model."""
        spec = MODEL_BY_ID[model_id]
        st = self.ic.get(model_id) or ICStat()
        lam = 30.0 / (30.0 + st.n_days)
        ic_meas = st.mean if math.isfinite(st.mean) else PRIOR_IC
        ic_hat = (1 - lam) * ic_meas + lam * PRIOR_IC
        mult = max(0.0, min(3.0, ic_hat / PRIOR_IC))
        return spec.prior_weight * mult, ic_hat

    def forecast(self, symbol: str, horizon_days: float = 10.0) -> dict[str, Any]:
        sigma_ann = self.vol_forecast.get(symbol) or self.realized_vol.get(symbol) or 0.3
        sigma_h = sigma_ann * math.sqrt(horizon_days / 252)
        contribs = []
        wz, ws, wics = [], [], []
        for m in MODELS:
            if not m.directional:
                continue
            sig = self.latest.get((symbol, m.id))
            if sig is None or sig.confidence <= 0:
                continue
            w, ic_hat = self.model_weight(m.id)
            w *= math.exp(-0.5 * abs(math.log(max(m.horizon_days, 0.05) / horizon_days)))
            if w <= 0:
                continue
            z = unsquash(sig.score)
            ws.append(w)
            wz.append(w * sig.confidence * z)
            wics.append((w, ic_hat, sig.confidence))
            contribs.append({"model": m.id, "weight": round(w * sig.confidence, 4), "score": round(sig.score, 4)})
        if not ws:
            return {"symbol": symbol, "t": now_ms(), "horizon_days": horizon_days, "exp_return": 0.0, "vol": sigma_ann,
                    "prob_up": 0.5, "z": 0.0, "ic": PRIOR_IC, "mu_annual": 0.0, "contributions": []}
        wc = np.array([w * c for w, _, c in wics])
        denom = math.sqrt(float(np.sum(wc**2) + AVG_CORR * (np.sum(wc) ** 2 - np.sum(wc**2))))
        z = float(np.sum(wz)) / denom if denom > 0 else 0.0
        wv = np.array(ws)
        ics = np.array([ic for _, ic, _ in wics])
        d_ic = math.sqrt(float(np.sum(wv**2) + AVG_CORR * (np.sum(wv) ** 2 - np.sum(wv**2))))
        ic = float(np.sum(wv * ics) / d_ic) if d_ic > 0 else PRIOR_IC
        ic = max(0.0, min(0.12, ic))
        er = ic * sigma_h * z
        prob_up = float(norm.cdf(er / sigma_h)) if sigma_h > 0 else 0.5
        contribs.sort(key=lambda c: -abs(c["weight"] * c["score"]))
        return {
            "symbol": symbol,
            "t": now_ms(),
            "horizon_days": horizon_days,
            "exp_return": er,
            "vol": sigma_ann,
            "prob_up": prob_up,
            "z": z,
            "ic": ic,
            "mu_annual": er * 252 / horizon_days,
            "contributions": contribs,
        }

    def model_table(self) -> list[dict[str, Any]]:
        out = []
        for m in MODELS:
            st = self.ic.get(m.id) or ICStat()
            w, ic_hat = self.model_weight(m.id)
            out.append({"id": m.id, "name": m.name, "horizon_days": m.horizon_days, "kind": m.kind,
                        "directional": m.directional, "description": m.description,
                        "enabled": m.id in self.enabled_models, "prior_weight": m.prior_weight,
                        "weight": round(w, 4), "ic": st.mean if math.isfinite(st.mean) else None,
                        "ic_sd": st.sd if math.isfinite(st.sd) else None, "ic_days": st.n_days, "ic_shrunk": round(ic_hat, 4)})
        return out


# ---------------------------------------------------------------------- helpers (thread-safe, pure)
def _is_today(t_ms: int, mode: str) -> bool:
    if mode == "sim":
        return now_ms() - t_ms < DAY_MS
    from datetime import datetime

    return datetime.fromtimestamp(t_ms / 1000, tz=clock.ET).date() == clock.today_et()


def _forecast_vols(arrays: dict[str, dict[str, np.ndarray]]) -> dict[str, tuple[float, float]]:
    out = {}
    for s, a in arrays.items():
        try:
            f = vol.forecast_vol(a["o"], a["h"], a["l"], a["c"], horizon_days=10)
        except Exception:  # noqa: BLE001
            f = math.nan
        try:
            yz = vol.yang_zhang(a["o"], a["h"], a["l"], a["c"], 20)
        except Exception:  # noqa: BLE001
            yz = math.nan
        out[s] = (float(f) if f is not None else math.nan, float(yz) if yz is not None else math.nan)
    return out


def _cross_sectional(arrays: dict[str, dict[str, np.ndarray]]) -> dict[str, dict[str, float]]:
    rets = {}
    for s, a in arrays.items():
        c = a["c"]
        if len(c) > 64:
            rets[s] = math.log(c[-1] / c[-64])
    bench = rets.get("SPY", float(np.median(list(rets.values()))) if rets else 0.0)
    ex = {s: r - bench for s, r in rets.items() if s != "SPY"}
    if len(ex) < 5:
        return {}
    vals = np.array(list(ex.values()))
    mu, sd = float(np.mean(vals)), float(np.std(vals, ddof=1))
    if sd <= 0:
        return {}
    return {s: {"xs_rs_z": (v - mu) / sd, "xs_excess": round(v, 4)} for s, v in ex.items()}


def _betas(arrays: dict[str, dict[str, np.ndarray]], window: int = 60) -> dict[str, float]:
    """OLS beta of each symbol's daily log returns on SPY's over the last ``window`` sessions (1.0 when SPY is missing)."""
    spy = arrays.get("SPY")
    if spy is None or len(spy["c"]) < window + 2:
        return {}
    rm = np.diff(np.log(spy["c"][-(window + 1):]))
    var = float(np.var(rm, ddof=1))
    if var <= 0:
        return {}
    out = {}
    for s, a in arrays.items():
        c = a["c"]
        if len(c) < window + 1:
            continue
        r = np.diff(np.log(c[-(window + 1):]))
        out[s] = max(-3.0, min(3.0, float(np.cov(r, rm, ddof=1)[0, 1] / var)))
    return out


def _compute_ic(rows: list[dict[str, Any]], mode: str) -> dict[str, ICStat]:
    prices: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        if r["price"]:
            prices[r["symbol"]].append((r["t"], r["price"]))
    for s in prices:
        prices[s].sort()
    ts_by_sym = {s: np.array([t for t, _ in v]) for s, v in prices.items()}
    px_by_sym = {s: np.array([p for _, p in v]) for s, v in prices.items()}
    now = now_ms()
    by_model_day: dict[str, dict[int, tuple[list[float], list[float]]]] = defaultdict(lambda: defaultdict(lambda: ([], [])))
    for r in rows:
        h = r["horizon_days"] or 1
        h_ms = int(h * 1.45 * DAY_MS) if h >= 1 else int(h * 6.5 * 3600_000)
        target = r["t"] + h_ms
        if target > now:
            continue
        ts = ts_by_sym.get(r["symbol"])
        if ts is None:
            continue
        j = int(np.searchsorted(ts, target))
        if j >= len(ts) or ts[j] - target > max(h_ms, 3 * 3600_000):
            continue
        fwd = math.log(px_by_sym[r["symbol"]][j] / r["price"])
        day = r["t"] // DAY_MS
        xs, ys = by_model_day[r["model"]][day]
        xs.append(r["score"])
        ys.append(fwd)
    out = {}
    for model, days in by_model_day.items():
        ics = []
        pairs = 0
        for xs, ys in days.values():
            pairs += len(xs)
            if len(xs) >= 5:
                ic = stats.spearman_ic(np.array(xs), np.array(ys))
                if math.isfinite(ic):
                    ics.append(ic)
        if ics:
            out[model] = ICStat(float(np.mean(ics)), float(np.std(ics, ddof=1)) if len(ics) > 1 else math.nan, len(ics), pairs)
    return out
