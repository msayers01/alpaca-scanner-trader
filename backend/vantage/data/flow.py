"""FlowService: options order-flow analytics and stock order-flow imbalance (OFI).

Stream mode (OPRA / SIM): every option print is classified by the quote rule against the latest
NBBO (ask-side = buyer-initiated), sweeps are detected (same contract, several prints within
``SWEEP_WINDOW_MS`` across exchanges), and an unusualness score is computed from premium,
size/open-interest, moneyness and tenor. Snapshot mode (free ``indicative`` feed): every minute
the cached chains are diffed for volume spikes vs open interest.

Per underlying, bullish premium (calls bought at ask, puts sold at bid) and bearish premium are
accumulated with exponential decay; ``flow_score()`` feeds the ``options_flow`` signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import FlowEvent, Quote, Trade, parse_occ
from vantage.core.util import now_ms

log = logging.getLogger(__name__)

SWEEP_WINDOW_MS = 250
FLOW_HALF_LIFE_MS = 6 * 3600_000


@dataclass
class _Agg:
    bull: float = 0.0
    bear: float = 0.0
    t: int = 0
    sweeps_bull: int = 0
    sweeps_bear: int = 0
    events: deque[FlowEvent] = field(default_factory=lambda: deque(maxlen=200))

    def decay(self, now: int) -> None:
        if self.t:
            f = math.exp(-math.log(2) * (now - self.t) / FLOW_HALF_LIFE_MS)
            self.bull *= f
            self.bear *= f
        self.t = now


@dataclass
class _OFI:
    prev: Quote | None = None
    window: deque[tuple[int, float, float]] = field(default_factory=lambda: deque(maxlen=5000))  # t, e_n, depth


class FlowService:
    def __init__(self, bus: Bus, store: Store, mode: str, options: Any, min_premium: float | None = None):
        self.bus = bus
        self.store = store
        self.mode = mode
        self.options = options
        self.min_premium = min_premium if min_premium is not None else (5_000 if mode == "sim" else 25_000)
        self.events: deque[FlowEvent] = deque(maxlen=3000)
        self.agg: dict[str, _Agg] = defaultdict(_Agg)
        self.ofi: dict[str, _OFI] = defaultdict(_OFI)
        self._pending: dict[str, list[Trade]] = {}
        self._offs: list[Any] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._last_vol: dict[str, float] = {}
        self.universe: set[str] = set()

    async def start(self, universe: list[str], realtime: bool, broker: Any = None) -> None:
        self.universe = set(universe)
        self._offs.append(self.bus.on("otrade.*", self._on_otrade))
        self._offs.append(self.bus.on("quote.*", self._on_quote))
        self._tasks.append(asyncio.create_task(self._sweep_flusher(), name="flow-sweeps"))
        if not realtime:
            self._tasks.append(asyncio.create_task(self._snapshot_loop(), name="flow-snapshots"))
        elif broker is not None and self.mode != "sim":
            self._tasks.append(asyncio.create_task(self._contract_subscriptions(broker), name="flow-subs"))

    async def _contract_subscriptions(self, broker: Any, max_trades: int = 3000, max_quotes: int = 1000) -> None:
        """OPRA: stream prints (and NBBO for the nearest-to-money subset) for liquid near-dated contracts of the
        universe, refreshed as the scanner refreshes chains. Wildcards are not allowed for option quotes."""
        trades: set[str] = set()
        quotes: set[str] = set()
        while True:
            await asyncio.sleep(60 if not trades else 300)
            try:
                today = clock.today_et()
                ranked: list[tuple[float, str]] = []
                for c in list(self.options.contracts.values()):
                    if c.underlying not in self.universe or (c.expiration - today).days > 60:
                        continue
                    m = abs(abs(c.delta) - 0.5) if c.delta is not None else 1.0  # nearest-to-money first
                    ranked.append((m - min(c.volume, 1e5) * 1e-7, c.symbol))
                ranked.sort()
                want_t = {s for _, s in ranked[:max_trades]}
                want_q = {s for _, s in ranked[:max_quotes]}
                add_t, add_q = sorted(want_t - trades), sorted(want_q - quotes)
                rm_t, rm_q = sorted(trades - want_t), sorted(quotes - want_q)
                if rm_t or rm_q:
                    await broker.options_unsubscribe(quotes=rm_q, trades=rm_t)
                if add_t or add_q:
                    await broker.options_subscribe(quotes=add_q, trades=add_t)
                trades, quotes = want_t, want_q
            except Exception:  # noqa: BLE001
                log.exception("option flow subscription refresh failed")

    async def stop(self) -> None:
        for off in self._offs:
            off()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    # ------------------------------------------------------------------ stock OFI (hot path)
    def _on_quote(self, topic: str, q: Quote) -> None:
        st = self.ofi[q.symbol]
        p = st.prev
        st.prev = q
        if p is None or q.bid <= 0 or q.ask <= 0:
            return
        e = 0.0
        if q.bid >= p.bid:
            e += q.bid_size
        if q.bid <= p.bid:
            e -= p.bid_size
        if q.ask <= p.ask:
            e -= q.ask_size
        if q.ask >= p.ask:
            e += p.ask_size
        st.window.append((q.t, e, (q.bid_size + q.ask_size) / 2))

    def ofi_score(self, symbol: str, window_ms: int = 300_000) -> tuple[float, int]:
        """Normalised OFI over the window: sum(e_n) / (mean depth * sqrt(n)). Returns (z-like, n)."""
        st = self.ofi.get(symbol)
        if not st or not st.window:
            return 0.0, 0
        cutoff = now_ms() - window_ms
        es = [(e, d) for t, e, d in st.window if t >= cutoff]
        if len(es) < 10:
            return 0.0, len(es)
        tot = sum(e for e, _ in es)
        depth = sum(d for _, d in es) / len(es) or 1.0
        return tot / (depth * math.sqrt(len(es))), len(es)

    # ------------------------------------------------------------------ options prints
    def _on_otrade(self, topic: str, tr: Trade) -> None:
        occ = parse_occ(tr.symbol)
        if not occ:
            return
        if self.universe and occ[0] not in self.universe and self.mode != "sim":
            return
        self._pending.setdefault(tr.symbol, []).append(tr)

    async def _sweep_flusher(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_WINDOW_MS / 1000)
            try:
                self._flush(now_ms())
            except Exception:  # noqa: BLE001
                log.exception("flow flush failed")

    def _flush(self, now: int) -> None:
        ready = [s for s, trs in self._pending.items() if now - trs[0].t >= SWEEP_WINDOW_MS or now - trs[-1].t > 50]
        for sym in ready:
            trs = self._pending.pop(sym)
            ev = self._classify(sym, trs)
            if ev is not None:
                self._emit(ev)

    def _quote_for(self, sym: str) -> Quote | None:
        if not self.options:
            return None
        q = self.options.quotes.get(sym)
        if q:
            return q
        c = self.options.contracts.get(sym)  # fall back to the latest chain snapshot
        return Quote(sym, c.t, c.bid, c.ask) if c else None

    def _contract_oi(self, sym: str) -> float:
        c = self.options.contracts.get(sym) if self.options else None
        return c.open_interest if c else 0.0

    def _classify(self, sym: str, trs: list[Trade]) -> FlowEvent | None:
        occ = parse_occ(sym)
        if not occ:
            return None
        root, exp, typ, strike = occ
        size = sum(t.size for t in trs)
        vwap = sum(t.price * t.size for t in trs) / size if size else trs[-1].price
        premium = vwap * size * 100
        exchanges = {t.exchange for t in trs if t.exchange}
        is_sweep = len(trs) >= 2 and (len(exchanges) >= 2 or len(trs) >= 3)
        q = self._quote_for(sym)
        side = "mid"
        if q and q.bid > 0 and q.ask > 0:
            mid = (q.bid + q.ask) / 2
            if vwap >= q.ask - 1e-9 or vwap > mid + 0.25 * (q.ask - mid):
                side = "ask"
            elif vwap <= q.bid + 1e-9 or vwap < mid - 0.25 * (mid - q.bid):
                side = "bid"
        if side == "ask":
            direction = 1 if typ == "call" else -1
        elif side == "bid":
            direction = -1 if typ == "call" else 1
        else:
            direction = 0
        score = unusual_score(premium, size, self._contract_oi(sym), (exp - clock.today_et()).days, is_sweep, side)
        if premium < self.min_premium and not (is_sweep and premium >= self.min_premium / 3) and score < 0.7:
            return None
        return FlowEvent(
            t=trs[0].t,
            contract=sym,
            underlying=root,
            type=typ,
            strike=strike,
            expiration=exp.isoformat(),
            price=round(vwap, 4),
            size=size,
            premium=round(premium, 2),
            side=side,  # type: ignore[arg-type]
            is_sweep=is_sweep,
            unusual_score=round(score, 3),
            direction=direction,
            source="stream",
        )

    def _emit(self, ev: FlowEvent) -> None:
        self.events.append(ev)
        a = self.agg[ev.underlying]
        a.decay(ev.t)
        w = ev.premium * (1.5 if ev.is_sweep else 1.0) * (0.5 + ev.unusual_score)
        if ev.direction > 0:
            a.bull += w
            a.sweeps_bull += int(ev.is_sweep)
        elif ev.direction < 0:
            a.bear += w
            a.sweeps_bear += int(ev.is_sweep)
        a.events.append(ev)
        self.bus.publish("flow", ev)

    # ------------------------------------------------------------------ snapshot fallback
    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._snapshot_scan()
            except Exception:  # noqa: BLE001
                log.exception("flow snapshot scan failed")

    async def _snapshot_scan(self) -> None:
        if not self.options:
            return
        today = clock.today_et()
        for (u, _a, _b), (_t, contracts) in list(self.options._chains.items()):
            for c in contracts:
                prev = self._last_vol.get(c.symbol)
                self._last_vol[c.symbol] = c.volume
                if prev is None:
                    continue
                dv = c.volume - prev
                if dv <= 0:
                    continue
                prem = dv * c.mid * 100
                if prem < self.min_premium:
                    continue
                side = "mid"
                if c.last and c.bid and c.ask:
                    side = "ask" if c.last >= c.ask - 0.005 else "bid" if c.last <= c.bid + 0.005 else "mid"
                direction = (1 if c.type == "call" else -1) * (1 if side == "ask" else -1 if side == "bid" else 0)
                self._emit(
                    FlowEvent(
                        t=now_ms(), contract=c.symbol, underlying=u, type=c.type, strike=c.strike,
                        expiration=c.expiration.isoformat(), price=round(c.last or c.mid, 4), size=dv,
                        premium=round(prem, 2), side=side, is_sweep=False,  # type: ignore[arg-type]
                        unusual_score=round(unusual_score(prem, dv, c.open_interest, (c.expiration - today).days, False, side), 3),
                        direction=direction, source="snapshot",
                    )
                )

    # ------------------------------------------------------------------ queries
    def recent(self, underlying: str | None = None, limit: int = 200) -> list[FlowEvent]:
        out = []
        for e in reversed(self.events):
            if underlying is None or e.underlying == underlying:
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def flow_score(self, underlying: str) -> dict[str, Any]:
        a = self.agg.get(underlying)
        if not a:
            return {"score": 0.0, "bull": 0.0, "bear": 0.0, "n": 0, "sweeps_bull": 0, "sweeps_bear": 0}
        a.decay(now_ms())
        tot = a.bull + a.bear
        imb = (a.bull - a.bear) / tot if tot > 0 else 0.0
        # confidence grows with absolute premium (log-scaled around $250k)
        conf = 1 - math.exp(-tot / 250_000)
        return {"score": imb, "confidence": conf, "bull": a.bull, "bear": a.bear, "n": len(a.events),
                "sweeps_bull": a.sweeps_bull, "sweeps_bear": a.sweeps_bear}


def unusual_score(premium: float, size: float, oi: float, dte: int, is_sweep: bool, side: str) -> float:
    """Heuristic 0..1 unusualness: big premium, size large vs OI (opening), short tenor, sweep, aggressive."""
    s_prem = 1 - math.exp(-premium / 100_000)
    s_oi = min(1.0, size / oi) if oi > 0 else 0.5
    s_dte = 1.0 if dte <= 7 else 0.7 if dte <= 30 else 0.4
    s = 0.45 * s_prem + 0.25 * s_oi + 0.1 * s_dte + 0.1 * float(is_sweep) + 0.1 * float(side != "mid")
    return max(0.0, min(1.0, s))


def _days(d: date) -> int:
    return (d - clock.today_et()).days
