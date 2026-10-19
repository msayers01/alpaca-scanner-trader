"""MarketDataService: latest quotes/trades, bar caches, ref-counted stream subscriptions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from vantage.broker.base import Broker, BrokerError
from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.types import Bar, Quote, Trade
from vantage.core.util import now_ms

log = logging.getLogger(__name__)

DAY_MS = 86_400_000

RANGE_SPEC: dict[str, tuple[str, int]] = {
    # range -> (timeframe, lookback days)
    "1D": ("1Min", 0),
    "1W": ("15Min", 7),
    "1M": ("1Hour", 31),
    "3M": ("1Day", 92),
    "YTD": ("1Day", 0),
    "1Y": ("1Day", 366),
    "5Y": ("1Week", 5 * 366),
}


class MarketDataService:
    def __init__(self, broker: Broker, bus: Bus, mode: str):
        self.broker = broker
        self.bus = bus
        self.mode = mode
        self.quotes: dict[str, Quote] = {}
        self.last: dict[str, float] = {}
        self.prev_close: dict[str, float] = {}
        self.day_volume: dict[str, float] = defaultdict(float)
        self.minute_bars: dict[str, list[Bar]] = defaultdict(list)  # today's 1-min bars
        self._daily: dict[str, tuple[float, list[Bar]]] = {}  # symbol -> (fetched_at, bars)
        self._refs: dict[str, int] = defaultdict(int)  # core subscriptions (universe/watchlist)
        self._ui_refs: dict[str, int] = defaultdict(int)  # browser subscriptions (highest priority)
        self._ui_last: dict[str, int] = {}
        self._order: dict[str, int] = {}
        self._quoted: set[str] = set()
        self._poller: asyncio.Task[None] | None = None
        self._offs: list[Any] = []
        self._lock = asyncio.Lock()
        self._daily_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, symbols: list[str]) -> None:
        self._offs.append(self.bus.on("quote.*", self._on_quote))
        self._offs.append(self.bus.on("trade.*", self._on_trade))
        self._offs.append(self.bus.on("bar.*", self._on_bar))
        await self.acquire(symbols)
        try:
            await self.refresh_snapshots(symbols)
        except BrokerError as e:
            log.warning("initial snapshots failed: %s", e)
        if self.mode != "sim":
            self._poller = asyncio.create_task(self._poll_unquoted(), name="snapshot-poller")

    async def stop(self) -> None:
        for off in self._offs:
            off()
        self._offs.clear()
        if self._poller:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller

    # ------------------------------------------------------------------ bus handlers (sync, hot path)
    def _on_quote(self, topic: str, q: Quote) -> None:
        self.quotes[q.symbol] = q

    def _on_trade(self, topic: str, tr: Trade) -> None:
        self.last[tr.symbol] = tr.price
        self.day_volume[tr.symbol] += tr.size

    def _on_bar(self, topic: str, b: Bar) -> None:
        bars = self.minute_bars[b.symbol]
        if bars and bars[-1].t == b.t:
            bars[-1] = b
        elif not bars or b.t > bars[-1].t:
            bars.append(b)
            if len(bars) > 2000:
                del bars[:-1500]
        self.last[b.symbol] = b.c if b.symbol not in self._quoted else self.last.get(b.symbol, b.c)

    # ------------------------------------------------------------------ subscriptions
    # The free IEX feed caps live quotes+trades at ~30 symbols per connection while minute bars are
    # effectively unlimited. Every acquired symbol streams bars; the quote budget goes to UI-viewed
    # symbols first, then core symbols in acquisition order; the rest are refreshed by REST polling.
    @property
    def quote_limit(self) -> int | None:
        return 30 if getattr(self.broker, "stock_feed", "") == "iex" else None

    @property
    def stream_trades(self) -> bool:
        return self.quote_limit is None

    async def acquire(self, symbols: list[str], priority: str = "core") -> None:
        refs = self._ui_refs if priority == "ui" else self._refs
        new_bars = []
        async with self._lock:
            for s in symbols:
                s = s.upper()
                if not self._ui_refs.get(s) and not self._refs.get(s):
                    new_bars.append(s)
                refs[s] += 1
                self._order.setdefault(s, len(self._order))
        try:
            if new_bars:
                await self.broker.stream_subscribe(bars=new_bars, trades=new_bars if self.stream_trades else ())
            await self._rebalance_quotes()
        except Exception as e:  # noqa: BLE001
            log.warning("stream subscribe failed: %s", e)

    async def release(self, symbols: list[str], priority: str = "core") -> None:
        refs = self._ui_refs if priority == "ui" else self._refs
        gone = []
        async with self._lock:
            for s in symbols:
                s = s.upper()
                if refs.get(s, 0) > 0:
                    refs[s] -= 1
                    if not self._ui_refs.get(s) and not self._refs.get(s):
                        gone.append(s)
        try:
            if gone:
                await self.broker.stream_unsubscribe(bars=gone, trades=gone if self.stream_trades else ())
            await self._rebalance_quotes()
        except Exception as e:  # noqa: BLE001
            log.warning("stream unsubscribe failed: %s", e)

    def _wanted_quotes(self) -> list[str]:
        active = [s for s in self._order if self._ui_refs.get(s) or self._refs.get(s)]
        active.sort(key=lambda s: (0 if self._ui_refs.get(s) else 1, -self._ui_last.get(s, 0), self._order[s]))
        lim = self.quote_limit
        return active if lim is None else active[:lim]

    async def _rebalance_quotes(self) -> None:
        for s in self._ui_refs:
            if self._ui_refs[s]:
                self._ui_last.setdefault(s, now_ms())
        want = set(self._wanted_quotes())
        add, drop = sorted(want - self._quoted), sorted(self._quoted - want)
        if drop:
            await self.broker.stream_unsubscribe(quotes=drop)
        if add:
            await self.broker.stream_subscribe(quotes=add)
        self._quoted = want

    def live_quoted(self, symbol: str) -> bool:
        return symbol in self._quoted

    async def _poll_unquoted(self) -> None:
        """REST snapshots every 10 s for acquired symbols that have no live quote slot."""
        while True:
            await asyncio.sleep(10)
            syms = [s for s in self._order if (self._ui_refs.get(s) or self._refs.get(s)) and s not in self._quoted]
            if not syms:
                continue
            try:
                snaps = await self.broker.get_snapshots(syms[:100])
            except Exception as e:  # noqa: BLE001
                log.debug("snapshot poll failed: %s", e)
                continue
            for s, sn in snaps.items():
                q = sn.get("quote")
                if sn.get("last"):
                    self.last[s] = sn["last"]
                if sn.get("prev_close"):
                    self.prev_close[s] = sn["prev_close"]
                if isinstance(q, Quote) and q.bid > 0 and q.ask > 0:
                    # sizes zeroed so polled quotes contribute nothing to order-flow imbalance
                    self.bus.publish(f"quote.{s}", Quote(s, q.t or now_ms(), q.bid, q.ask, 0.0, 0.0))

    # ------------------------------------------------------------------ snapshots / quotes
    async def refresh_snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        snaps = await self.broker.get_snapshots([s.upper() for s in symbols])
        for s, sn in snaps.items():
            q = sn.get("quote")
            if isinstance(q, Quote) and (q.bid > 0 or q.ask > 0):
                self.quotes.setdefault(s, q)
            if sn.get("last"):
                self.last[s] = sn["last"]
            if sn.get("prev_close"):
                self.prev_close[s] = sn["prev_close"]
            if sn.get("volume"):
                self.day_volume[s] = max(self.day_volume[s], sn["volume"])
        return snaps

    def price(self, symbol: str) -> float | None:
        q = self.quotes.get(symbol)
        if q and q.bid > 0 and q.ask > 0 and q.spread_pct < 0.02:
            return q.mid
        return self.last.get(symbol) or (q.mid if q else None)

    async def ensure_price(self, symbol: str) -> float | None:
        p = self.price(symbol)
        if p is None:
            try:
                await self.refresh_snapshots([symbol])
            except BrokerError:
                return None
            p = self.price(symbol)
        return p

    async def quote_snap(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        if symbol not in self.prev_close or self.price(symbol) is None:
            await self.refresh_snapshots([symbol])
        q = self.quotes.get(symbol)
        last = self.price(symbol) or 0.0
        pc = self.prev_close.get(symbol) or last
        return {
            "symbol": symbol,
            "t": q.t if q else now_ms(),
            "bid": q.bid if q else 0.0,
            "ask": q.ask if q else 0.0,
            "last": last,
            "prev_close": pc,
            "change": last - pc,
            "change_pct": (last / pc - 1) if pc else 0.0,
            "volume": self.day_volume.get(symbol, 0.0),
        }

    def wire_quote(self, q: Quote) -> dict[str, Any]:
        last = self.price(q.symbol) or q.mid
        pc = self.prev_close.get(q.symbol)
        return {
            "symbol": q.symbol,
            "t": q.t,
            "bid": q.bid,
            "ask": q.ask,
            "last": round(last, 4),
            "change_pct": (last / pc - 1) if pc else 0.0,
        }

    # ------------------------------------------------------------------ bars
    async def daily_bars(self, symbol: str, days: int = 500, max_age_s: float = 3600) -> list[Bar]:
        """Daily bars (cached). Includes today's partial bar when the broker provides it."""
        symbol = symbol.upper()
        async with self._daily_locks[symbol]:
            hit = self._daily.get(symbol)
            if hit and time.monotonic() - hit[0] < max_age_s and len(hit[1]) >= min(days, 30):
                return hit[1][-days:]
            start = now_ms() - int(days * 1.5 + 10) * DAY_MS
            bars = await self.broker.get_bars(symbol, "1Day", start)
            self._daily[symbol] = (time.monotonic(), bars)
            if len(bars) >= 2 and symbol not in self.prev_close:
                today = clock.today_et()
                last_d = datetime.fromtimestamp(bars[-1].t / 1000, tz=clock.ET).date()
                self.prev_close[symbol] = bars[-2].c if last_d == today else bars[-1].c
            return bars[-days:]

    async def daily_bars_multi(self, symbols: list[str], days: int = 500) -> dict[str, list[Bar]]:
        need = [s for s in symbols if not (s in self._daily and time.monotonic() - self._daily[s][0] < 3600)]
        if need:
            start = now_ms() - int(days * 1.5 + 10) * DAY_MS
            for i in range(0, len(need), 50):
                chunk = need[i : i + 50]
                try:
                    res = await self.broker.get_bars_multi(chunk, "1Day", start)
                except BrokerError as e:
                    log.warning("daily bars multi failed: %s", e)
                    continue
                for s, bars in res.items():
                    self._daily[s] = (time.monotonic(), bars)
        return {s: self._daily.get(s, (0, []))[1][-days:] for s in symbols}

    async def intraday_bars(self, symbol: str, minutes: int = 390) -> list[Bar]:
        symbol = symbol.upper()
        bars = self.minute_bars.get(symbol)
        if not bars or len(bars) < min(minutes, 30):
            start = now_ms() - max(minutes, 60) * 60_000 * 3
            try:
                fetched = await self.broker.get_bars(symbol, "1Min", start)
            except BrokerError:
                fetched = []
            merged = {b.t: b for b in fetched}
            for b in self.minute_bars.get(symbol, []):
                merged[b.t] = b
            self.minute_bars[symbol] = sorted(merged.values(), key=lambda b: b.t)
        return self.minute_bars[symbol][-minutes:]

    async def bars_for_range(self, symbol: str, rng: str) -> dict[str, Any]:
        symbol = symbol.upper()
        if rng not in RANGE_SPEC:
            raise ValueError(f"bad range {rng}")
        tf, days = RANGE_SPEC[rng]
        now = now_ms()
        today = clock.today_et()
        if rng == "1D":
            if self.mode == "sim":
                start = _sim_day_start(now)
            else:
                d = clock.last_session_date()
                start = clock.to_ms(clock.et_dt(d, clock.PRE_OPEN))
        elif rng == "YTD":
            start = clock.to_ms(clock.et_dt(date(today.year, 1, 1), clock.PRE_OPEN))
        else:
            start = now - days * DAY_MS
        bars = await self.broker.get_bars(symbol, tf, start)
        if rng == "1D" and self.mode != "sim":
            # keep the live bar cache in sync for the 1D chart
            merged = {b.t: b for b in bars}
            for b in self.minute_bars.get(symbol, []):
                if b.t >= start:
                    merged[b.t] = b
            bars = sorted(merged.values(), key=lambda b: b.t)
        if symbol not in self.prev_close:
            with contextlib.suppress(BrokerError):
                await self.refresh_snapshots([symbol])
        pc = self.prev_close.get(symbol)
        return {
            "symbol": symbol,
            "range": rng,
            "timeframe": tf,
            "prev_close": pc if rng == "1D" else (bars[0].o if bars else None),
            "bars": [b.to_wire() for b in bars],
        }

    # ------------------------------------------------------------------ helpers for signals
    @staticmethod
    def ohlcv(bars: list[Bar]) -> dict[str, np.ndarray]:
        return {
            "t": np.array([b.t for b in bars], dtype=np.int64),
            "o": np.array([b.o for b in bars], dtype=float),
            "h": np.array([b.h for b in bars], dtype=float),
            "l": np.array([b.l for b in bars], dtype=float),
            "c": np.array([b.c for b in bars], dtype=float),
            "v": np.array([b.v for b in bars], dtype=float),
        }


def _sim_day_start(t_ms: int) -> int:
    from vantage.broker.sim import SimMarket

    return SimMarket._day_start(t_ms)


def days_ago(n: int) -> date:
    return clock.today_et() - timedelta(days=n)
