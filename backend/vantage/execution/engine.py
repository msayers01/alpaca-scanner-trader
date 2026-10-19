"""ExecutionEngine: works limit orders from mid toward the far side on a ladder, then cancels.

Entry orders: start at mid + a0·half-spread, every ``reprice_s`` add 0.15·half-spread up to
``max_aggression``; cancel after ``timeout_s``. Urgent exits: equities go market, options go to
the bid (never a market order on an option). All orders get a ``vt-`` client id and are recorded
in ``orders_meta`` so fills can be attributed to strategies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vantage.broker.base import Broker, BrokerError
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.util import dumps, new_id, now_ms

log = logging.getLogger(__name__)

QuoteFn = Callable[[str], tuple[float, float] | None]
FillCb = Callable[[dict[str, Any], "WorkingOrder"], Awaitable[None]]


def tick_round(px: float, side: str, is_option: bool) -> float:
    tick = 0.01 if (not is_option or px < 3.0) else 0.05
    if not is_option and px < 1.0:
        tick = 0.0001
    n = px / tick
    n = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    return round(max(n * tick, tick), 4)


@dataclass
class WorkingOrder:
    client_id: str
    symbol: str
    side: str
    qty: float | None
    notional: float | None
    is_option: bool
    purpose: str  # "entry" | "exit" | "manual"
    strategy_id: str | None
    aggression: float
    max_aggression: float
    timeout_s: float
    created: int = field(default_factory=now_ms)
    broker_id: str | None = None
    limit: float | None = None
    status: str = "pending"
    filled_qty: float = 0.0
    avg_price: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class ExecutionEngine:
    def __init__(self, broker: Broker, bus: Bus, store: Store, mode: str, quote_fn: QuoteFn, reprice_s: float = 5.0):
        self.broker = broker
        self.bus = bus
        self.store = store
        self.mode = mode
        self.quote_fn = quote_fn
        self.reprice_s = reprice_s
        self.working: dict[str, WorkingOrder] = {}  # client_id -> order
        self._by_broker: dict[str, str] = {}
        self._fill_cbs: list[FillCb] = []
        self._sub = bus.subscribe("order", maxsize=0)
        self._tasks: list[asyncio.Task[None]] = []

    def on_fill(self, cb: FillCb) -> None:
        self._fill_cbs.append(cb)

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._events(), name="exec-events"))

    async def stop(self) -> None:
        self._sub.close()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    def pending_symbols(self) -> set[str]:
        return {w.symbol for w in self.working.values() if w.status in ("pending", "working") and w.purpose == "entry"}

    def is_working(self, symbol: str) -> bool:
        return any(w.symbol == symbol and w.status in ("pending", "working") for w in self.working.values())

    # ------------------------------------------------------------------ submission
    async def submit(
        self,
        symbol: str,
        side: str,
        qty: float | None,
        is_option: bool,
        purpose: str,
        strategy_id: str | None,
        aggression: float = 0.25,
        max_aggression: float = 0.8,
        timeout_s: float = 90,
        urgent: bool = False,
        notional: float | None = None,
        meta: dict[str, Any] | None = None,
        ref_price: float | None = None,
    ) -> WorkingOrder:
        w = WorkingOrder(new_id("vt-"), symbol, side, qty, notional, is_option, purpose, strategy_id,
                         1.0 if urgent else aggression, max(aggression, max_aggression), timeout_s, meta=meta or {})
        self.working[w.client_id] = w
        self.store.write(
            "INSERT INTO orders_meta(client_order_id,mode,t,broker_id,symbol,strategy_id,purpose,data) VALUES(?,?,?,?,?,?,?,?)",
            (w.client_id, self.mode, w.created, None, symbol, strategy_id, purpose, dumps(w.meta).decode()),
        )
        req: dict[str, Any] = {"symbol": symbol, "side": side, "client_order_id": w.client_id, "time_in_force": "day",
                               "asset_class": "option" if is_option else "equity"}
        if qty is not None:
            req["qty"] = qty
        else:
            req["notional"] = notional
        if urgent and not is_option:
            req["type"] = "market"
        else:
            px = self._limit_price(symbol, side, w.aggression, is_option, ref_price)
            if px is None:
                w.status = "rejected"
                w.meta["error"] = "no quote available"
                w.done.set()
                raise BrokerError(f"no quote for {symbol}")
            req["type"] = "limit"
            req["limit_price"] = px
            w.limit = px
            if notional is not None and qty is None:
                # fractional limit orders must be qty-based
                req.pop("notional")
                req["qty"] = round(notional / px, 6)
                w.qty = req["qty"]
        try:
            o = await self.broker.submit_order(req)
        except BrokerError as e:
            w.status = "rejected"
            w.meta["error"] = str(e)
            w.done.set()
            raise
        w.broker_id = o.get("id")
        if w.broker_id:
            self._by_broker[w.broker_id] = w.client_id
            self.store.write("UPDATE orders_meta SET broker_id=? WHERE client_order_id=?", (w.broker_id, w.client_id))
        if o.get("status") == "filled":
            await self._handle_fill(w, o)
        else:
            w.status = "working"
            if req["type"] == "limit":
                self._tasks.append(asyncio.create_task(self._work(w), name=f"work-{symbol}"))
        return w

    def _limit_price(self, symbol: str, side: str, a: float, is_option: bool, ref: float | None) -> float | None:
        q = self.quote_fn(symbol)
        if q is None or q[0] <= 0 or q[1] <= 0:
            if ref:
                return tick_round(ref, side, is_option)
            return None
        bid, ask = q
        mid = (bid + ask) / 2
        half = (ask - bid) / 2
        px = mid + a * half if side == "buy" else mid - a * half
        return tick_round(px, side, is_option)

    async def _work(self, w: WorkingOrder) -> None:
        try:
            while w.status == "working":
                await asyncio.sleep(self.reprice_s)
                if w.status != "working":
                    return
                age = (now_ms() - w.created) / 1000
                if age >= w.timeout_s:
                    with contextlib.suppress(BrokerError):
                        await self.broker.cancel_order(w.broker_id or "")
                    w.meta["timeout"] = True
                    return
                if w.aggression >= w.max_aggression:
                    continue
                w.aggression = min(w.max_aggression, w.aggression + 0.15)
                px = self._limit_price(w.symbol, w.side, w.aggression, w.is_option, w.limit)
                if px is None or px == w.limit:
                    continue
                try:
                    o = await self.broker.replace_order(w.broker_id or "", limit_price=px)
                except BrokerError as e:
                    log.info("replace failed for %s: %s", w.symbol, e)
                    continue
                if o.get("id") and o["id"] != w.broker_id:
                    self._by_broker[o["id"]] = w.client_id
                    w.broker_id = o["id"]
                w.limit = px
        except asyncio.CancelledError:
            pass

    async def cancel_all(self, purpose: str | None = None) -> None:
        for w in list(self.working.values()):
            if w.status == "working" and (purpose is None or w.purpose == purpose):
                with contextlib.suppress(BrokerError):
                    await self.broker.cancel_order(w.broker_id or "")

    # ------------------------------------------------------------------ events
    async def _events(self) -> None:
        async for _, o in self._sub:
            try:
                cid = o.get("client_order_id")
                w = self.working.get(cid) if cid else None
                if w is None and o.get("id") in self._by_broker:
                    w = self.working.get(self._by_broker[o["id"]])
                if w is None:
                    continue
                ev = o.get("event")
                if o.get("id") and o["id"] != w.broker_id and ev in ("new", "pending_new", "replaced"):
                    self._by_broker[o["id"]] = w.client_id
                if ev in ("fill",):
                    await self._handle_fill(w, o)
                elif ev == "partial_fill":
                    w.filled_qty = float(o.get("filled_qty") or 0)
                    w.avg_price = o.get("filled_avg_price")
                elif ev in ("canceled", "expired", "rejected", "done_for_day"):
                    if o.get("id") == w.broker_id:
                        w.status = ev
                        if w.filled_qty > 0:
                            await self._notify(w, o)
                        w.done.set()
            except Exception:  # noqa: BLE001
                log.exception("order event handling failed")

    async def _handle_fill(self, w: WorkingOrder, o: dict[str, Any]) -> None:
        if w.status == "filled":
            return
        w.status = "filled"
        w.filled_qty = float(o.get("filled_qty") or w.qty or 0)
        w.avg_price = o.get("filled_avg_price") or o.get("fill_price") or w.limit
        await self._notify(w, o)
        w.done.set()

    async def _notify(self, w: WorkingOrder, o: dict[str, Any]) -> None:
        for cb in self._fill_cbs:
            try:
                await cb(o, w)
            except Exception:  # noqa: BLE001
                log.exception("fill callback failed")
