"""WebSocket hub: channel subscriptions, per-key conflation, batched flushes (docs/API.md)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from vantage.core.bus import Bus
from vantage.core.util import dumps, loads, now_ms, sanitize

log = logging.getLogger(__name__)

CONFLATED_PREFIXES = ("quote:", "bar:", "oquote:")
CONFLATED = {"account", "positions", "equity", "status", "scanner"}
FEEDS = {"orders", "news", "flow", "signals", "decisions"}


class Client:
    __slots__ = ("ws", "channels", "latest", "feed", "event")

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.channels: set[str] = set()
        self.latest: dict[str, Any] = {}
        self.feed: list[tuple[str, Any]] = []
        self.event = asyncio.Event()


class Hub:
    def __init__(self, bus: Bus, container_fn: Callable[[], Any]):
        self.bus = bus
        self.container_fn = container_fn
        self.clients: set[Client] = set()
        self._offs: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self.sent_frames = 0

    @property
    def flush_s(self) -> float:
        c = self.container_fn()
        return max(0.016, (c.ws_flush_ms if c else 50) / 1000)

    def start(self) -> None:
        on = self.bus.on
        self._offs += [
            on("quote.*", lambda t, q: self._push(f"quote:{q.symbol}", q, conflate=True)),
            on("bar.*", lambda t, b: self._push(f"bar:{b.symbol}", b, conflate=True)),
            on("oquote.*", lambda t, q: self._push(f"oquote:{q.symbol}", q, conflate=True)),
            on("account", lambda t, d: self._push("account", d, conflate=True)),
            on("positions", lambda t, d: self._push("positions", d, conflate=True)),
            on("equity", lambda t, d: self._push("equity", d, conflate=True)),
            on("scanner", lambda t, d: self._push("scanner", d, conflate=True)),
            on("order", lambda t, d: self._push("orders", d)),
            on("news", lambda t, d: self._push("news", d)),
            on("flow", lambda t, d: self._push("flow", d)),
            on("signal", lambda t, d: self._push("signals", d)),
            on("decision", lambda t, d: self._push("decisions", d)),
        ]
        self._task = asyncio.create_task(self._flusher(), name="ws-flush")
        self._status_task = asyncio.create_task(self._status_loop(), name="ws-status")

    async def stop(self) -> None:
        for off in self._offs:
            off()
        for t in (self._task, self._status_task):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    # ------------------------------------------------------------------ fan-out (sync, hot path)
    def _push(self, ch: str, payload: Any, conflate: bool = False) -> None:
        if not self.clients:
            return
        for cl in self.clients:
            if ch in cl.channels:
                if conflate:
                    cl.latest[ch] = payload
                else:
                    cl.feed.append((ch, payload))
                    if len(cl.feed) > 2000:
                        del cl.feed[:1000]
                cl.event.set()

    def _wire(self, ch: str, payload: Any) -> Any:
        c = self.container_fn()
        if ch.startswith("quote:") and c is not None:
            return c.market.wire_quote(payload)
        if ch.startswith("oquote:"):
            return {"symbol": payload.symbol, "t": payload.t, "bid": payload.bid, "ask": payload.ask,
                    "mid": round(payload.mid, 4)}
        if ch.startswith("bar:"):
            return payload.to_wire()
        if hasattr(payload, "to_wire"):
            return payload.to_wire()
        return payload

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(self.flush_s)
            for cl in list(self.clients):
                if not cl.event.is_set():
                    continue
                cl.event.clear()
                batch = [{"ch": ch, "d": self._wire(ch, p)} for ch, p in cl.feed]
                cl.feed.clear()
                for ch, p in cl.latest.items():
                    batch.append({"ch": ch, "d": self._wire(ch, p)})
                cl.latest.clear()
                if not batch:
                    continue
                try:
                    await cl.ws.send_bytes(dumps(sanitize({"b": batch, "ts": now_ms()})))
                    self.sent_frames += 1
                except Exception:  # noqa: BLE001
                    self.clients.discard(cl)

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            c = self.container_fn()
            if c is not None and self.clients:
                with contextlib.suppress(Exception):
                    self._push("status", c.status(), conflate=True)

    # ------------------------------------------------------------------ subscriptions
    async def _acquire(self, chans: list[str]) -> None:
        c = self.container_fn()
        if c is None:
            return
        stocks = [ch.split(":", 1)[1] for ch in chans if ch.startswith(("quote:", "bar:"))]
        opts = [ch.split(":", 1)[1] for ch in chans if ch.startswith("oquote:")]
        if stocks:
            await c.market.acquire(stocks, "ui")
        if opts:
            with contextlib.suppress(Exception):
                await c.broker.options_subscribe(quotes=opts)

    async def _release(self, chans: list[str]) -> None:
        c = self.container_fn()
        if c is None:
            return
        stocks = [ch.split(":", 1)[1] for ch in chans if ch.startswith(("quote:", "bar:"))]
        opts = [ch.split(":", 1)[1] for ch in chans if ch.startswith("oquote:")]
        if stocks:
            await c.market.release(stocks, "ui")
        if opts:
            still = {ch for cl in self.clients for ch in cl.channels}
            gone = [o for o in opts if f"oquote:{o}" not in still]
            if gone:
                with contextlib.suppress(Exception):
                    await c.broker.options_unsubscribe(quotes=gone)

    async def resubscribe_all(self) -> None:
        """After a mode switch: re-acquire every client's market-data channels in the new container."""
        chans = [ch for cl in self.clients for ch in cl.channels]
        await self._acquire(chans)

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        cl = Client(ws)
        cl.channels.add("status")
        self.clients.add(cl)
        c = self.container_fn()
        if c is not None:
            cl.latest["status"] = c.status()
            cl.event.set()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                op = msg.get("op")
                chans = [str(x) for x in msg.get("ch", []) if isinstance(x, str)][:500]
                if op == "sub":
                    new = [ch for ch in chans if ch not in cl.channels and _valid(ch)]
                    cl.channels |= set(new)
                    await self._acquire(new)
                    self._prime(cl, new)
                elif op == "unsub":
                    gone = [ch for ch in chans if ch in cl.channels]
                    cl.channels -= set(gone)
                    await self._release(gone)
                elif op == "ping":
                    await ws.send_bytes(dumps({"pong": msg.get("id"), "ts": now_ms()}))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.debug("ws client error", exc_info=True)
        finally:
            self.clients.discard(cl)
            await self._release(list(cl.channels))

    def _prime(self, cl: Client, chans: list[str]) -> None:
        """Send current values immediately so new subscribers don't wait for the next tick."""
        c = self.container_fn()
        if c is None:
            return
        for ch in chans:
            if ch == "account" and c.account:
                cl.latest[ch] = c.account
            elif ch == "positions":
                cl.latest[ch] = c.positions
            elif ch == "scanner" and c.scanner.last.get("t"):
                cl.latest[ch] = c.scanner.last
            elif ch.startswith("quote:"):
                q = c.market.quotes.get(ch[6:])
                if q:
                    cl.latest[ch] = q
        if chans:
            cl.event.set()


def _valid(ch: str) -> bool:
    if ch in CONFLATED or ch in FEEDS:
        return True
    if ch.startswith(CONFLATED_PREFIXES):
        sym = ch.split(":", 1)[1]
        return 0 < len(sym) <= 32 and sym.replace(".", "").replace("/", "").isalnum()
    return False
