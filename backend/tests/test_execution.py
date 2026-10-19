"""ExecutionEngine: timed-out orders must always reach a terminal state (a stuck 'working' order blocks the instrument)."""

from __future__ import annotations

from typing import Any

from vantage.broker.base import BrokerError
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.util import now_ms
from vantage.execution.engine import ExecutionEngine, WorkingOrder


class FakeBroker:
    def __init__(self, final: dict[str, Any] | Exception):
        self.final = final
        self.cancels = 0

    async def cancel_order(self, oid: str) -> None:
        self.cancels += 1
        raise BrokerError("order not cancelable", 422)

    async def get_order(self, oid: str) -> dict[str, Any]:
        if isinstance(self.final, Exception):
            raise self.final
        return self.final


def _engine(broker):
    return ExecutionEngine(broker, Bus(), Store(":memory:"), "sim", lambda s: (1.0, 1.1))


def _w(eng, **kw):
    w = WorkingOrder("vt-1", "AAPL", "buy", 1, None, False, "entry", "s", 0.25, 0.8, 90, broker_id="b1", status="working", **kw)
    eng.working[w.client_id] = w
    return w


async def test_lost_cancel_event_adopts_broker_state():
    eng = _engine(FakeBroker({"status": "canceled", "filled_qty": "0"}))
    w = _w(eng)
    await eng._cancel_and_reconcile(w, wait_s=0.01)
    assert w.status == "canceled" and w.done.is_set()
    assert eng.pending_symbols() == set() and not eng.is_working("AAPL")


async def test_cancel_that_raced_a_fill_records_the_fill():
    eng = _engine(FakeBroker({"status": "filled", "filled_qty": "1", "filled_avg_price": "1.05"}))
    fills = []

    async def cb(o, w):
        fills.append((w.symbol, w.filled_qty, w.avg_price))

    eng.on_fill(cb)
    w = _w(eng)
    await eng._cancel_and_reconcile(w, wait_s=0.01)
    assert w.status == "filled" and fills == [("AAPL", 1.0, "1.05")]


async def test_unreachable_broker_still_releases_the_instrument():
    eng = _engine(FakeBroker(BrokerError("down", 503)))
    w = _w(eng)
    await eng._cancel_and_reconcile(w, attempts=2, wait_s=0.01)
    assert w.status == "expired" and w.meta["reconcile_failed"] and w.done.is_set()
    assert not eng.is_working("AAPL")


async def test_partial_fill_before_cancel_is_reported():
    eng = _engine(FakeBroker({"status": "canceled", "filled_qty": "2", "filled_avg_price": "1.02"}))
    seen = []

    async def cb(o, w):
        seen.append(w.filled_qty)

    eng.on_fill(cb)
    w = _w(eng)
    await eng._cancel_and_reconcile(w, wait_s=0.01)
    assert seen == [2.0]


def test_prune_forgets_old_finished_orders_only():
    eng = _engine(FakeBroker({}))
    old = now_ms() - 3_600_000
    for i in range(210):
        w = WorkingOrder(f"vt-{i}", "X", "buy", 1, None, False, "entry", None, 0.2, 0.8, 90, created=old, status="filled", broker_id=f"b{i}")
        eng.working[w.client_id] = w
    live = WorkingOrder("vt-live", "Y", "buy", 1, None, False, "entry", None, 0.2, 0.8, 90, created=old, status="working")
    eng.working["vt-live"] = live
    eng._prune()
    assert list(eng.working) == ["vt-live"]


async def test_reconcile_loop_adopts_a_fill_whose_event_was_lost():
    import asyncio

    eng = _engine(FakeBroker({"status": "filled", "filled_qty": "1", "filled_avg_price": "1.04"}))
    fills = []

    async def cb(o, w):
        fills.append(w.symbol)

    eng.on_fill(cb)
    w = _w(eng)
    w.created = now_ms() - 60_000  # looks stale
    task = asyncio.create_task(eng._reconcile_loop(every_s=0.01, stale_s=1))
    await asyncio.wait_for(w.done.wait(), 2)
    task.cancel()
    assert w.status == "filled" and fills == ["AAPL"]
