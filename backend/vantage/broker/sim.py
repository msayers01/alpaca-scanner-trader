"""SIM mode: a synthetic market and a simulated broker implementing the Broker protocol.

The sim market runs 24/7 (so the app is demonstrable any time). Prices follow seeded GBM with
jumps and slowly varying drift; news events nudge drift so signal models have something to find.
Option chains are priced with Black-Scholes on a smile/term-structure surface. Nothing here says
anything about real-world profitability.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from vantage.broker.alpaca_rest import rank_assets
from vantage.broker.base import BrokerError
from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.types import Bar, NewsItem, OptionContract, Quote, Trade, make_occ, parse_occ
from vantage.core.util import new_id, now_ms

log = logging.getLogger(__name__)

MIN_MS = 60_000
DAY_MS = 86_400_000

BASE_PRICES = {
    "SPY": 660.0, "QQQ": 590.0, "IWM": 240.0, "DIA": 460.0, "AAPL": 245.0, "MSFT": 510.0, "NVDA": 185.0,
    "AMZN": 230.0, "META": 720.0, "GOOGL": 245.0, "TSLA": 430.0, "AMD": 220.0, "AVGO": 340.0,
    "NFLX": 1200.0, "JPM": 300.0, "XLF": 53.0, "XLE": 90.0, "SMH": 320.0, "COIN": 330.0, "PLTR": 180.0,
    "UBER": 95.0, "XLK": 280.0, "XLV": 140.0, "TLT": 90.0, "GLD": 380.0,
}
BASE_VOL = {"SPY": 0.15, "QQQ": 0.20, "IWM": 0.22, "DIA": 0.14, "XLF": 0.18, "XLE": 0.25, "TLT": 0.14, "GLD": 0.16}
NAMES = {
    "SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ Trust", "IWM": "iShares Russell 2000 ETF",
    "AAPL": "Apple Inc.", "MSFT": "Microsoft Corp.", "NVDA": "NVIDIA Corp.", "AMZN": "Amazon.com Inc.",
    "META": "Meta Platforms Inc.", "GOOGL": "Alphabet Inc. Class A", "TSLA": "Tesla Inc.",
    "AMD": "Advanced Micro Devices", "AVGO": "Broadcom Inc.", "NFLX": "Netflix Inc.", "JPM": "JPMorgan Chase",
    "XLF": "Financial Select Sector SPDR", "XLE": "Energy Select Sector SPDR", "SMH": "VanEck Semiconductor ETF",
    "COIN": "Coinbase Global", "PLTR": "Palantir Technologies", "UBER": "Uber Technologies",
}

NEWS_TEMPLATES = [
    (0.7, "{s} jumps after {firm} upgrades shares to Buy, lifts target"),
    (0.6, "{s} beats quarterly estimates on revenue and EPS; guidance raised"),
    (0.5, "{s} announces $10B share buyback program"),
    (0.4, "{s} wins major multi-year contract, report says"),
    (0.3, "Unusual call buying spotted in {s} ahead of product event"),
    (-0.7, "{s} falls after {firm} downgrades to Underperform"),
    (-0.6, "{s} misses estimates, cuts full-year outlook"),
    (-0.5, "Regulators open probe into {s} business practices"),
    (-0.4, "{s} CFO departs unexpectedly; shares slide"),
    (-0.3, "Short seller targets {s} in new report"),
    (0.0, "{s} to present at {firm} technology conference next week"),
    (0.0, "{s} files routine 10-Q with the SEC"),
]
FIRMS = ["Morgan Stanley", "Goldman Sachs", "JPMorgan", "Barclays", "UBS", "Jefferies", "Bernstein", "Citi"]


def _seed(*parts: Any) -> int:
    h = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little")


@dataclass
class SimSymbol:
    symbol: str
    price: float
    vol: float
    drift: float = 0.0  # annualised, mean-reverting toward 0, nudged by news
    minutes: deque[Bar] = field(default_factory=lambda: deque(maxlen=12_000))
    daily: list[Bar] = field(default_factory=list)
    prev_close: float = 0.0
    day_open_t: int = 0
    day_volume: float = 0.0


class SimMarket:
    def __init__(self, bus: Bus, symbols: list[str], seed: int = 7):
        self.bus = bus
        self.rng = random.Random(seed)
        self.syms: dict[str, SimSymbol] = {}
        self.quote_subs: set[str] = set()
        self.opt_quote_subs: set[str] = set()
        for s in symbols:
            self.ensure(s)

    # ------------------------------------------------------------------ construction
    def ensure(self, symbol: str) -> SimSymbol:
        symbol = symbol.upper()
        if symbol in self.syms:
            return self.syms[symbol]
        rs = np.random.default_rng(_seed("sim", symbol))
        base = BASE_PRICES.get(symbol, float(20 + rs.uniform(0, 280)))
        vol = BASE_VOL.get(symbol, float(rs.uniform(0.25, 0.55)))
        sym = SimSymbol(symbol, base, vol)
        self._build_history(sym, rs)
        self.syms[symbol] = sym
        return sym

    def _build_history(self, sym: SimSymbol, rs: np.random.Generator) -> None:
        """5y of daily bars ending yesterday, then 7 days of 24/7 minute bars ending now."""
        now = now_ms()
        today = clock.today_et()
        days: list[date] = []
        d = today
        while len(days) < 1260:
            d -= timedelta(days=1)
            if clock.is_trading_day(d):
                days.append(d)
        days.reverse()
        n = len(days)
        mu = 0.10
        dt = 1 / 252
        # regime-switching drift + vol clustering (GARCH-like) + jumps
        sig = np.empty(n)
        var = sym.vol**2
        rets = np.empty(n)
        drift = 0.0
        for i in range(n):
            drift = 0.98 * drift + 0.02 * rs.normal(0, 0.6)
            z = rs.standard_normal()
            jump = rs.normal(-0.01, 0.05) if rs.random() < 0.01 else 0.0
            r = (mu + drift - 0.5 * var) * dt + math.sqrt(var * dt) * z + jump
            rets[i] = r
            var = 0.04 * sym.vol**2 + 0.90 * var + 0.06 * (r**2) / dt
            var = min(max(var, (0.3 * sym.vol) ** 2), (3 * sym.vol) ** 2)
            sig[i] = math.sqrt(var)
        # the current conditional vol drives both intraday ticks and the option IV surface,
        # so implied and realised vol stay consistent (no free "cheap vol" artefacts in SIM)
        sym.vol = float(np.clip(np.mean(sig[-20:]), 0.6 * sym.vol, 1.6 * sym.vol))
        closes = sym.price * np.exp(np.cumsum(rets) - rets.sum())  # end at base price
        prev = closes[0] / math.exp(rets[0])
        for i, dd in enumerate(days):
            c = float(closes[i])
            o = float(prev * math.exp(rs.normal(0, sig[i] * 0.25 * math.sqrt(dt))))
            rng_ = abs(rs.normal(0, sig[i] * math.sqrt(dt))) * max(o, c)
            h = max(o, c) + rng_ * rs.uniform(0.2, 0.8)
            lo = min(o, c) - rng_ * rs.uniform(0.2, 0.8)
            v = float(rs.lognormal(16, 0.4)) * (1 + abs(c / prev - 1) * 20)
            t = clock.to_ms(clock.et_dt(dd, clock.OPEN))
            sym.daily.append(Bar(sym.symbol, t, round(o, 2), round(h, 2), round(lo, 2), round(c, 2), round(v)))
            prev = c
        # minute path over the last 7 days, continuous, ending at current price
        m = 7 * 24 * 60
        sm = sym.vol / math.sqrt(252 * 390)  # per-minute vol in trading-time units
        # weekends/nights move less
        steps = rs.standard_normal(m) * sm * 0.6
        path = sym.price * np.exp(np.cumsum(steps) - steps.sum())
        start = (now // MIN_MS) * MIN_MS - (m - 1) * MIN_MS
        p_prev = float(path[0])
        for i in range(m):
            c = float(path[i])
            hi = max(p_prev, c) * (1 + abs(rs.normal(0, sm * 0.3)))
            lo = min(p_prev, c) * (1 - abs(rs.normal(0, sm * 0.3)))
            sym.minutes.append(Bar(sym.symbol, start + i * MIN_MS, p_prev, hi, lo, c, float(rs.lognormal(9, 0.6))))
            p_prev = c
        sym.price = float(path[-1])
        sym.prev_close = sym.daily[-1].c
        # make the live price continuous with the last daily close + the intraday move
        scale = sym.prev_close / float(path[max(0, m - self._minutes_into_day(now) - 1)])
        for b in sym.minutes:
            b.o *= scale
            b.h *= scale
            b.l *= scale
            b.c *= scale
        sym.price = sym.minutes[-1].c
        sym.day_open_t = self._day_start(now)

    @staticmethod
    def _day_start(t_ms: int) -> int:
        dt = datetime.fromtimestamp(t_ms / 1000, tz=UTC).astimezone(clock.ET)
        start = dt.replace(hour=4, minute=0, second=0, microsecond=0)
        if dt < start:
            start -= timedelta(days=1)
        return int(start.timestamp() * 1000)

    def _minutes_into_day(self, t_ms: int) -> int:
        return int((t_ms - self._day_start(t_ms)) // MIN_MS)

    # ------------------------------------------------------------------ ticking
    def tick(self, dt_s: float) -> None:
        t = now_ms()
        day_start = self._day_start(t)
        for sym in self.syms.values():
            if day_start != sym.day_open_t:  # sim "day" rollover at 04:00 ET
                sym.prev_close = sym.price
                sym.day_open_t = day_start
                sym.day_volume = 0.0
            sym.drift *= math.exp(-dt_s / 1800)  # news drift decays over ~30 min
            sm = sym.vol * math.sqrt(dt_s / (252 * 390 * 60))
            r = sym.drift * dt_s / (252 * 390 * 60) * 60 + sm * self.rng.gauss(0, 1)
            if self.rng.random() < 0.0002:
                r += self.rng.gauss(0, sym.vol * 0.02)
            sym.price = max(0.5, sym.price * math.exp(r))
            p = round(sym.price, 2)
            size = float(int(self.rng.lognormvariate(4, 1)))
            sym.day_volume += size
            mt = (t // MIN_MS) * MIN_MS
            last = sym.minutes[-1] if sym.minutes else None
            if last and last.t == mt:
                last.h = max(last.h, p)
                last.l = min(last.l, p)
                last.c = p
                last.v += size
                bar = last
            else:
                bar = Bar(sym.symbol, mt, p, p, p, p, size)
                sym.minutes.append(bar)
            spread = max(0.01, round(p * 0.0002, 2))
            q = Quote(sym.symbol, t, round(p - spread / 2, 2), round(p + spread / 2, 2),
                      float(self.rng.randint(1, 20) * 100), float(self.rng.randint(1, 20) * 100))
            self.bus.publish(f"quote.{sym.symbol}", q)
            self.bus.publish(f"trade.{sym.symbol}", Trade(sym.symbol, t, p, size))
            self.bus.publish(f"bar.{sym.symbol}", bar)

    def quote(self, symbol: str) -> Quote:
        sym = self.ensure(symbol)
        p = sym.price
        spread = max(0.01, round(p * 0.0002, 2))
        return Quote(symbol, now_ms(), round(p - spread / 2, 2), round(p + spread / 2, 2), 500, 500)

    # ------------------------------------------------------------------ bars
    def bars(self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None) -> list[Bar]:
        sym = self.ensure(symbol)
        end_ms = end_ms or now_ms() + MIN_MS
        if timeframe in ("1Day", "1Week"):
            out = [b for b in sym.daily if start_ms <= b.t <= end_ms]
            today = Bar(symbol, sym.day_open_t, sym.prev_close, max(sym.prev_close, sym.price),
                        min(sym.prev_close, sym.price), sym.price, sym.day_volume)
            if today.t >= start_ms:
                out.append(today)
            if timeframe == "1Week":
                out = _aggregate(out, 7 * DAY_MS)
            return out
        mins = [b for b in sym.minutes if start_ms <= b.t <= end_ms]
        step = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60}.get(timeframe, 1)
        return _aggregate(mins, step * MIN_MS) if step > 1 else [Bar(**_bar_kwargs(b)) for b in mins]

    # ------------------------------------------------------------------ options
    def vol_surface(self, sym: SimSymbol, k_over_s: np.ndarray, t_years: np.ndarray) -> np.ndarray:
        m = np.log(k_over_s)
        base = sym.vol * (1.05 + 0.1 * np.exp(-t_years * 8))  # IV slightly above RV, short-dated premium
        return np.clip(base * (1 - 0.8 * m + 2.0 * m**2), 0.05, 3.0)

    def expirations(self, today: date) -> list[date]:
        exps: list[date] = []
        d = today
        while len(exps) < 8:
            d += timedelta(days=1)
            if d.weekday() == 4:
                exps.append(d)
        for k in range(3, 13):
            y, mth = today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1
            tf = clock.third_friday(y, mth)
            if tf not in exps:
                exps.append(tf)
        return sorted(exps)

    def chain(self, symbol: str, exp_gte: date | None, exp_lte: date | None) -> list[OptionContract]:
        from vantage.quant.bs import bs_greeks, bs_price

        sym = self.ensure(symbol)
        S = sym.price
        today = clock.today_et()
        exps = [e for e in self.expirations(today) if (not exp_gte or e >= exp_gte) and (not exp_lte or e <= exp_lte)]
        inc = 1.0 if S < 100 else 2.5 if S < 300 else 5.0
        center = round(S / inc) * inc
        strikes = np.array([center + inc * i for i in range(-15, 16) if center + inc * i > 0])
        out: list[OptionContract] = []
        t = now_ms()
        for e in exps:
            T = max((e - today).days, 0.5) / 365
            for is_call in (True, False):
                iv = self.vol_surface(sym, strikes / S, np.full_like(strikes, T))
                px = np.asarray(bs_price(S, strikes, T, 0.04, 0.0, iv, is_call))
                g = bs_greeks(S, strikes, T, 0.04, 0.0, iv, is_call)
                for j, k in enumerate(strikes):
                    mid = float(px[j])
                    if mid < 0.02:
                        continue
                    half = max(0.01, mid * (0.015 + 0.02 * abs(math.log(k / S)) * 10)) / 2
                    bid = max(0.01, round(mid - half, 2))
                    ask = round(mid + half, 2)
                    rs = random.Random(_seed(symbol, e, k, is_call, today))
                    moneyness = abs(math.log(k / S))
                    oi = int(rs.lognormvariate(7.5, 1.0) * math.exp(-moneyness * 8) * (1 + 3 * (e - today).days ** -0.5))
                    vol = int(oi * rs.uniform(0.05, 0.6))
                    out.append(
                        OptionContract(
                            symbol=make_occ(symbol, e, "call" if is_call else "put", float(k)),
                            underlying=symbol,
                            type="call" if is_call else "put",
                            strike=float(k),
                            expiration=e,
                            bid=bid,
                            ask=ask,
                            last=round(mid, 2),
                            volume=float(vol),
                            open_interest=float(oi),
                            iv=float(iv[j]),
                            delta=float(g["delta"][j]),
                            gamma=float(g["gamma"][j]),
                            theta=float(g["theta"][j]),
                            vega=float(g["vega"][j]),
                            t=t,
                        )
                    )
        return out

    def option_quote(self, occ: str) -> Quote | None:
        from vantage.quant.bs import bs_price

        p = parse_occ(occ)
        if not p:
            return None
        root, exp, typ, k = p
        sym = self.ensure(root)
        S = sym.price
        T = max((exp - clock.today_et()).days, 0.5) / 365
        iv = float(self.vol_surface(sym, np.array([k / S]), np.array([T]))[0])
        mid = float(bs_price(S, k, T, 0.04, 0.0, iv, typ == "call"))
        half = max(0.01, mid * (0.015 + 0.02 * abs(math.log(k / S)) * 10)) / 2
        return Quote(occ, now_ms(), max(0.01, round(mid - half, 2)), round(mid + half, 2), 10, 10)

    def option_bars(self, occ: str, timeframe: str, start_ms: int, end_ms: int | None) -> list[Bar]:
        from vantage.quant.bs import bs_price

        p = parse_occ(occ)
        if not p:
            return []
        root, exp, typ, k = p
        sym = self.ensure(root)
        und = self.bars(root, timeframe, start_ms, end_ms)
        if not und:
            return []
        exp_ms = clock.to_ms(clock.et_dt(exp, clock.CLOSE))
        T = np.array([max((exp_ms - b.t) / (365 * DAY_MS), 1 / 365 / 24) for b in und])
        S = np.array([b.c for b in und])
        iv = self.vol_surface(sym, k / S, T)
        c = np.asarray(bs_price(S, k, T, 0.04, 0.0, iv, typ == "call"))
        out = []
        prev = float(c[0])
        for b, ci in zip(und, c, strict=True):
            ci = float(max(ci, 0.01))
            out.append(Bar(occ, b.t, round(prev, 2), round(max(prev, ci), 2), round(min(prev, ci), 2), round(ci, 2), b.v / 50))
            prev = ci
        return out

    # ------------------------------------------------------------------ synthetic news & flow
    def random_news(self) -> NewsItem:
        s = self.rng.choice(list(self.syms))
        sent, tpl = self.rng.choice(NEWS_TEMPLATES)
        sym = self.syms[s]
        sym.drift += sent * 2.0  # annualised drift shock that decays
        sym.price *= math.exp(sent * 0.004)
        return NewsItem(
            id=new_id("sim-"),
            t=now_ms(),
            headline=tpl.format(s=NAMES.get(s, s), firm=self.rng.choice(FIRMS)),
            summary="Synthetic SIM-mode headline.",
            source="sim",
            url="",
            symbols=[s],
        )

    def random_flow(self) -> list[tuple[Quote, Trade]]:
        s = self.rng.choice(list(self.syms))
        sym = self.syms[s]
        bullish = self.rng.random() < 0.5 + max(-0.3, min(0.3, sym.drift / 10))
        typ = "call" if (bullish ^ (self.rng.random() < 0.3)) else "put"
        exps = self.expirations(clock.today_et())[: self.rng.randint(1, 6)]
        e = exps[-1]
        inc = 1.0 if sym.price < 100 else 2.5 if sym.price < 300 else 5.0
        otm = self.rng.randint(0, 6) * inc
        k = round((sym.price + (otm if typ == "call" else -otm)) / inc) * inc
        occ = make_occ(s, e, typ, k)
        q = self.option_quote(occ)
        if q is None:
            return []
        aggressive_buy = (typ == "call") == bullish
        n = self.rng.choice([1, 1, 1, 3, 5])  # multi-leg prints = sweep
        size = max(1, int(self.rng.lognormvariate(3.5, 1.3)))
        out = []
        t = now_ms()
        for _ in range(n):
            px = q.ask if aggressive_buy else q.bid
            out.append((q, Trade(occ, t, px, size, self.rng.choice("ABCIMNQWXZ"), "")))
        return out


def _bar_kwargs(b: Bar) -> dict[str, Any]:
    return {"symbol": b.symbol, "t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}


def _aggregate(bars: list[Bar], step_ms: int) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        bt = (b.t // step_ms) * step_ms
        if out and out[-1].t == bt:
            o = out[-1]
            o.h = max(o.h, b.h)
            o.l = min(o.l, b.l)
            o.c = b.c
            o.v += b.v
        else:
            out.append(Bar(b.symbol, bt, b.o, b.h, b.l, b.c, b.v))
    return out


# ====================================================================== broker
class SimBroker:
    mode = "sim"
    stock_feed = "sim"
    options_feed = "sim"
    flow_realtime = True

    def __init__(self, bus: Bus, symbols: list[str], store: Any = None, initial_equity: float = 10_000.0):
        self.bus = bus
        self.market = SimMarket(bus, symbols)
        self.store = store
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.equity_points: list[tuple[int, float]] = []
        self.last_equity = initial_equity
        self.day_start = SimMarket._day_start(now_ms())
        self.daytrades: deque[int] = deque()
        self._opened_today: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.store:
            return
        st = self.store.kv_get("sim", "broker_state")
        if st:
            self.cash = st.get("cash", self.cash)
            self.initial_equity = st.get("initial_equity", self.initial_equity)
            self.positions = st.get("positions", {})
            self.equity_points = [tuple(p) for p in st.get("equity_points", [])][-20000:]  # type: ignore[misc]
            self.last_equity = st.get("last_equity", self.cash)

    def _save(self) -> None:
        if not self.store:
            return
        self.store.kv_set(
            "sim",
            "broker_state",
            {
                "cash": self.cash,
                "initial_equity": self.initial_equity,
                "positions": self.positions,
                "equity_points": self.equity_points[-20000:],
                "last_equity": self.last_equity,
            },
        )

    def reset(self, equity: float) -> None:
        self.initial_equity = equity
        self.cash = equity
        self.positions.clear()
        self.orders.clear()
        self.equity_points.clear()
        self.last_equity = equity
        self.daytrades.clear()
        self._save()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sim-loop")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._save()

    async def _loop(self) -> None:
        dt = 0.25
        n = 0
        next_news = 15.0
        next_flow = 2.0
        elapsed = 0.0
        while True:
            await asyncio.sleep(dt)
            elapsed += dt
            n += 1
            try:
                self.market.tick(dt)
                self._match_orders()
                for occ in list(self.market.opt_quote_subs):
                    q = self.market.option_quote(occ)
                    if q:
                        self.bus.publish(f"oquote.{occ}", q)
                if elapsed >= next_news:
                    self.bus.publish("news.raw", self.market.random_news())
                    next_news = elapsed + self.market.rng.uniform(20, 75)
                if elapsed >= next_flow:
                    for q, tr in self.market.random_flow():
                        self.bus.publish(f"oquote.{tr.symbol}", q)
                        self.bus.publish(f"otrade.{tr.symbol}", tr)
                    next_flow = elapsed + self.market.rng.uniform(1.5, 6)
                if n % 4 == 0:
                    t = now_ms()
                    ds = SimMarket._day_start(t)
                    eq = self._equity()
                    if ds != self.day_start:
                        self.day_start = ds
                        self.last_equity = eq
                        self._opened_today.clear()
                    self.bus.publish("equity", {"t": t, "v": round(eq, 2)})
                    if not self.equity_points or t - self.equity_points[-1][0] >= MIN_MS:
                        self.equity_points.append((t, round(eq, 2)))
                        if len(self.equity_points) % 5 == 0:
                            self._save()
            except Exception:  # noqa: BLE001
                log.exception("sim loop error")

    # ------------------------------------------------------------------ pricing helpers
    def _price(self, symbol: str) -> tuple[float, float]:
        if parse_occ(symbol):
            q = self.market.option_quote(symbol)
            return (q.bid, q.ask) if q else (0.0, 0.0)
        q = self.market.quote(symbol)
        return q.bid, q.ask

    def _mark(self, symbol: str) -> float:
        b, a = self._price(symbol)
        return (b + a) / 2

    def _equity(self) -> float:
        eq = self.cash
        for sym, p in self.positions.items():
            mult = 100 if parse_occ(sym) else 1
            eq += p["qty"] * self._mark(sym) * mult
        return eq

    # ------------------------------------------------------------------ trading API
    async def get_account(self) -> dict[str, Any]:
        eq = self._equity()
        now = now_ms()
        while self.daytrades and now - self.daytrades[0] > 7 * DAY_MS:
            self.daytrades.popleft()
        options_value = sum(
            p["qty"] * self._mark(s) * 100 for s, p in self.positions.items() if parse_occ(s)
        )
        return {
            "equity": eq,
            "last_equity": self.last_equity,
            "cash": self.cash,
            "buying_power": max(self.cash, 0.0),
            "options_buying_power": max(self.cash, 0.0),
            "daytrade_count": len(self.daytrades),
            "pattern_day_trader": False,
            "daytrading_buying_power": max(self.cash, 0.0) if eq >= 25_000 else 0.0,
            "day_pl": eq - self.last_equity,
            "day_pl_pct": (eq - self.last_equity) / self.last_equity if self.last_equity else 0.0,
            "status": "ACTIVE",
            "currency": "USD",
            "options_level": 2,
            "multiplier": 1.0,
            "shorting_enabled": False,
            "trading_blocked": False,
            "options_market_value": options_value,
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for sym, p in self.positions.items():
            occ = parse_occ(sym)
            mult = 100 if occ else 1
            px = self._mark(sym)
            mv = p["qty"] * px * mult
            cb = p["qty"] * p["avg"] * mult
            ref = p.get("day_ref") or p["avg"]
            pos: dict[str, Any] = {
                "symbol": sym,
                "asset_class": "option" if occ else "equity",
                "underlying": occ[0] if occ else sym,
                "qty": p["qty"],
                "side": "long",
                "avg_entry_price": p["avg"],
                "current_price": round(px, 4),
                "market_value": mv,
                "cost_basis": cb,
                "unrealized_pl": mv - cb,
                "unrealized_plpc": (mv - cb) / cb if cb else 0.0,
                "change_today_pct": px / ref - 1 if ref else 0.0,
                "lastday_price": ref,
                "strategy_id": None,
            }
            if occ:
                pos["option"] = {"type": occ[2], "strike": occ[3], "expiration": occ[1].isoformat(),
                                 "dte": (occ[1] - clock.today_et()).days}
            out.append(pos)
        return out

    async def get_orders(self, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        rows = sorted(self.orders.values(), key=lambda o: o["submitted_at"], reverse=True)
        if status == "open":
            rows = [o for o in rows if o["status"] in ("new", "accepted", "partially_filled")]
        elif status == "closed":
            rows = [o for o in rows if o["status"] not in ("new", "accepted", "partially_filled")]
        return [dict(o) for o in rows[:limit]]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        if order_id not in self.orders:
            raise BrokerError("order not found", 404)
        return dict(self.orders[order_id])

    async def submit_order(self, req: dict[str, Any]) -> dict[str, Any]:
        sym = req["symbol"].upper()
        occ = parse_occ(sym)
        if not occ:
            self.market.ensure(sym)
        side = req["side"]
        typ = req.get("type", "limit")
        qty = req.get("qty")
        notional = req.get("notional")
        if occ and (qty is None or float(qty) != int(float(qty))):
            raise BrokerError("options orders require whole-contract qty", 422)
        if typ == "limit" and not req.get("limit_price"):
            raise BrokerError("limit_price required", 422)
        bid, ask = self._price(sym)
        if notional is not None and qty is None:
            qty = float(notional) / (ask if side == "buy" else bid)
        qty = float(qty or 0)
        if qty <= 0:
            raise BrokerError("qty must be > 0", 422)
        if side == "sell":
            held = self.positions.get(sym, {}).get("qty", 0.0)
            if qty > held + 1e-9:
                raise BrokerError("insufficient position (shorting disabled in sim)", 403)
        else:
            mult = 100 if occ else 1
            px = float(req.get("limit_price") or ask)
            if qty * px * mult > self.cash + 1e-6:
                raise BrokerError("insufficient buying power", 403)
        oid = new_id("sim-")
        order = {
            "id": oid,
            "client_order_id": req.get("client_order_id") or new_id("vt-"),
            "symbol": sym,
            "asset_class": "option" if occ else "equity",
            "side": side,
            "qty": qty,
            "notional": notional,
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "type": typ,
            "limit_price": req.get("limit_price"),
            "stop_price": None,
            "status": "new",
            "time_in_force": req.get("time_in_force", "day"),
            "submitted_at": now_ms(),
            "filled_at": None,
            "strategy_id": None,
        }
        self.orders[oid] = order
        self._emit(order, "new")
        self._try_fill(order, aggressive=typ == "market")
        return dict(order)

    async def replace_order(self, order_id: str, limit_price: float | None = None, qty: float | None = None) -> dict[str, Any]:
        o = self.orders.get(order_id)
        if not o or o["status"] not in ("new", "accepted", "partially_filled"):
            raise BrokerError("order not open", 422)
        o["status"] = "replaced"
        self._emit(o, "replaced")
        new = dict(o)
        new["id"] = new_id("sim-")
        new["status"] = "new"
        if limit_price is not None:
            new["limit_price"] = limit_price
        if qty is not None:
            new["qty"] = qty
        new["submitted_at"] = now_ms()
        self.orders[new["id"]] = new
        self._emit(new, "new")
        self._try_fill(new, aggressive=False)
        return dict(new)

    async def cancel_order(self, order_id: str) -> None:
        o = self.orders.get(order_id)
        if o and o["status"] in ("new", "accepted", "partially_filled"):
            o["status"] = "canceled"
            self._emit(o, "canceled")

    async def close_position(self, symbol: str, pct: float = 1.0) -> dict[str, Any]:
        p = self.positions.get(symbol)
        if not p:
            raise BrokerError("position not found", 404)
        qty = p["qty"] * pct
        if parse_occ(symbol):
            qty = max(1, int(round(qty)))
        return await self.submit_order({"symbol": symbol, "side": "sell", "qty": qty, "type": "market"})

    # ------------------------------------------------------------------ fills
    def _match_orders(self) -> None:
        for o in list(self.orders.values()):
            if o["status"] in ("new", "accepted", "partially_filled"):
                self._try_fill(o, aggressive=o["type"] == "market")

    def _try_fill(self, o: dict[str, Any], aggressive: bool) -> None:
        bid, ask = self._price(o["symbol"])
        if bid <= 0 or ask <= 0:
            return
        lp = o.get("limit_price")
        px = None
        if o["side"] == "buy":
            if aggressive or (lp is not None and lp >= ask):
                px = ask if aggressive else min(lp, ask)
            elif lp is not None and lp >= (bid + ask) / 2 and self.market.rng.random() < 0.08:
                px = lp
        else:
            if aggressive or (lp is not None and lp <= bid):
                px = bid if aggressive else max(lp, bid)
            elif lp is not None and lp <= (bid + ask) / 2 and self.market.rng.random() < 0.08:
                px = lp
        if px is None:
            return
        self._fill(o, float(px))

    def _fill(self, o: dict[str, Any], px: float) -> None:
        sym = o["symbol"]
        mult = 100 if parse_occ(sym) else 1
        qty = o["qty"]
        now = now_ms()
        if o["side"] == "buy":
            cost = qty * px * mult
            if cost > self.cash + 1e-6:
                o["status"] = "rejected"
                self._emit(o, "rejected")
                return
            self.cash -= cost
            p = self.positions.setdefault(sym, {"qty": 0.0, "avg": 0.0, "day_ref": px})
            p["avg"] = (p["avg"] * p["qty"] + px * qty) / (p["qty"] + qty)
            p["qty"] += qty
            self._opened_today[sym] = now
        else:
            p = self.positions.get(sym)
            if not p:
                o["status"] = "rejected"
                self._emit(o, "rejected")
                return
            qty = min(qty, p["qty"])
            self.cash += qty * px * mult
            p["qty"] -= qty
            if p["qty"] <= 1e-9:
                del self.positions[sym]
            if sym in self._opened_today:
                self.daytrades.append(now)
        o["filled_qty"] = qty
        o["filled_avg_price"] = px
        o["status"] = "filled"
        o["filled_at"] = now
        self._emit(o, "fill", px, qty)
        self._save()

    def _emit(self, o: dict[str, Any], event: str, px: float | None = None, qty: float | None = None) -> None:
        d = dict(o)
        d.update(event=event, fill_price=px, fill_qty=qty, event_t=now_ms())
        self.bus.publish("order", d)

    # ------------------------------------------------------------------ history
    async def get_portfolio_history(self, rng: str) -> dict[str, Any]:
        now = now_ms()
        eq_now = self._equity()
        if rng == "1D":
            start = self.day_start
            pts = [(t, v) for t, v in self.equity_points if t >= start]
            synth = self._synthetic_history(start, now, 5 * MIN_MS, self.last_equity)
            first_real = pts[0][0] if pts else now
            pts = [p for p in synth if p[0] < first_real] + pts
            base = self.last_equity
            tf = "5Min"
        else:
            span = {"1W": 7, "1M": 30, "3M": 91, "1Y": 365, "ALL": 730}.get(rng)
            if rng == "YTD":
                start = int(datetime(datetime.now().year, 1, 1, tzinfo=clock.ET).timestamp() * 1000)
            else:
                start = now - (span or 30) * DAY_MS
            step = 15 * MIN_MS if rng == "1W" else 60 * MIN_MS if rng == "1M" else DAY_MS
            real = [(t, v) for t, v in self.equity_points if t >= start]
            first_real = real[0][0] if real else now
            synth = self._synthetic_history(start, first_real, step, real[0][1] if real else eq_now)
            pts = synth + _downsample(real, step)
            base = pts[0][1] if pts else eq_now
            tf = {"1W": "15Min", "1M": "1H"}.get(rng, "1D")
        pts.append((now, round(eq_now, 2)))
        return {"range": rng, "timeframe": tf, "base_value": base, "points": [{"t": t, "v": v} for t, v in pts]}

    def _synthetic_history(self, start: int, end: int, step: int, end_value: float) -> list[tuple[int, float]]:
        """Seeded random-walk equity before the sim account had real history (demo only)."""
        n = int((end - start) // step)
        if n <= 0:
            return []
        rs = np.random.default_rng(_seed("equity", start // DAY_MS, step))
        per_step_vol = 0.16 * math.sqrt(step / (365 * DAY_MS))
        steps = rs.normal(0.12 * step / (365 * DAY_MS), per_step_vol, n)
        path = np.exp(np.cumsum(steps[::-1]))[::-1]  # anchored at end
        vals = end_value / path
        return [(start + i * step, round(float(vals[i]), 2)) for i in range(n)]

    async def get_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        out = []
        d = start
        while d <= end:
            if clock.is_trading_day(d):
                out.append({"date": d.isoformat(), "open": "09:30", "close": clock.close_time(d).strftime("%H:%M")})
            d += timedelta(days=1)
        return out

    # ------------------------------------------------------------------ market data
    async def get_bars(self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None, limit: int = 10000) -> list[Bar]:
        return self.market.bars(symbol, timeframe, start_ms, end_ms)[-limit:]

    async def get_bars_multi(self, symbols: list[str], timeframe: str, start_ms: int, end_ms: int | None = None) -> dict[str, list[Bar]]:
        return {s: self.market.bars(s, timeframe, start_ms, end_ms) for s in symbols}

    async def get_snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for s in symbols:
            sym = self.market.ensure(s)
            q = self.market.quote(s)
            out[s] = {
                "symbol": s,
                "quote": q,
                "last": round(sym.price, 2),
                "t": q.t,
                "prev_close": sym.prev_close,
                "day": None,
                "minute": sym.minutes[-1] if sym.minutes else None,
                "volume": sym.day_volume,
            }
        return out

    async def get_option_expirations(self, underlying: str) -> list[date]:
        self.market.ensure(underlying)
        return self.market.expirations(clock.today_et())

    async def get_option_chain(self, underlying: str, exp_gte: date | None = None, exp_lte: date | None = None) -> list[OptionContract]:
        return await asyncio.to_thread(self.market.chain, underlying, exp_gte, exp_lte)

    async def get_option_bars(self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None) -> list[Bar]:
        return self.market.option_bars(symbol, timeframe, start_ms, end_ms)

    async def get_news(self, symbols: list[str] | None, limit: int = 50) -> list[NewsItem]:
        return []

    async def search_assets(self, q: str) -> list[dict[str, Any]]:
        assets = [{"symbol": s, "name": NAMES.get(s, s), "exchange": "SIM"} for s in sorted(set(BASE_PRICES) | set(self.market.syms))]
        res = rank_assets(assets, q)
        if not res and q.strip().isalpha() and len(q.strip()) <= 5:
            res = [{"symbol": q.strip().upper(), "name": "Synthetic instrument", "exchange": "SIM"}]
        return res

    async def get_most_actives(self, top: int = 20) -> list[str]:
        return sorted(self.market.syms, key=lambda s: -self.market.syms[s].day_volume)[:top]

    # ------------------------------------------------------------------ streaming
    async def stream_subscribe(self, quotes=(), bars=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        for s in {*quotes, *bars, *trades}:
            self.market.ensure(s)

    async def stream_unsubscribe(self, quotes=(), bars=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        return None

    async def options_subscribe(self, quotes=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        self.market.opt_quote_subs |= set(quotes)

    async def options_unsubscribe(self, quotes=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        self.market.opt_quote_subs -= set(quotes)

    def connection_state(self) -> dict[str, bool]:
        return {"stocks": True, "options": True, "news": True, "trade_updates": True}

    def stream_errors(self) -> dict[str, Any]:
        return {}


def _downsample(pts: list[tuple[int, float]], step: int) -> list[tuple[int, float]]:
    out: dict[int, tuple[int, float]] = {}
    for t, v in pts:
        out[t // step] = (t, v)
    return list(out.values())
