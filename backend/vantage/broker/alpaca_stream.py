"""Async Alpaca WebSocket clients: stocks (JSON), options (msgpack), news (JSON), trade_updates.

Each client keeps its desired subscription set, reconnects with exponential backoff and replays
subscriptions after reconnecting. Messages are decoded into domain types and published on the Bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import msgpack
import orjson
import websockets

from vantage.broker.alpaca_rest import news_from_alpaca, normalize_order
from vantage.core.bus import Bus
from vantage.core.types import Bar, Quote, Trade
from vantage.core.util import fnum, iso_to_ms

log = logging.getLogger(__name__)

STOCK_WS = "wss://stream.data.alpaca.markets/v2/{feed}"
OPTION_WS = "wss://stream.data.alpaca.markets/v1beta1/{feed}"
NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"
TRADE_WS = {"paper": "wss://paper-api.alpaca.markets/stream", "live": "wss://api.alpaca.markets/stream"}


def _ts(v: Any) -> int:
    if isinstance(v, str):
        return iso_to_ms(v) or 0
    if isinstance(v, datetime):
        return int(v.timestamp() * 1000)
    if isinstance(v, msgpack.Timestamp):
        return v.to_unix_nano() // 1_000_000
    return int(v or 0)


class _BaseStream:
    name = "stream"
    binary = False

    def __init__(self, url: str, key: str, secret: str, bus: Bus):
        self.url = url
        self.key = key
        self.secret = secret
        self.bus = bus
        self.connected = False
        self.subs: dict[str, set[str]] = {}
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name=f"ws-{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self.connected = False

    def _encode(self, msg: dict[str, Any]) -> bytes | str:
        return msgpack.packb(msg) if self.binary else orjson.dumps(msg).decode()

    def _decode(self, raw: bytes | str) -> Any:
        if self.binary and isinstance(raw, bytes):
            return msgpack.unpackb(raw, timestamp=3)
        return orjson.loads(raw)

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._ws is not None and self.connected:
            await self._ws.send(self._encode(msg))

    def _has_subs(self) -> bool:
        return any(self.subs.values())

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._has_subs() and self.name in ("stocks", "options"):
                await asyncio.sleep(0.5)
                continue
            try:
                headers = {"Content-Type": "application/msgpack"} if self.binary else None
                async with websockets.connect(
                    self.url, additional_headers=headers, max_size=2**24, ping_interval=20, open_timeout=10
                ) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    log.info("%s stream connected", self.name)
                    await self._resubscribe()
                    async for raw in ws:
                        msg = self._decode(raw)
                        if isinstance(msg, list):
                            for m in msg:
                                self._dispatch(m)
                        else:
                            self._dispatch(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.warning("%s stream error: %s", self.name, e)
            finally:
                self.connected = False
                self._ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff + random.random() * 0.5)
            backoff = min(backoff * 2, 30.0)

    async def _handshake(self, ws: Any) -> None:
        # market-data style: {"T":"success","msg":"connected"} then auth
        first = self._decode(await ws.recv())
        log.debug("%s hello %s", self.name, first)
        await ws.send(self._encode({"action": "auth", "key": self.key, "secret": self.secret}))
        resp = self._decode(await asyncio.wait_for(ws.recv(), 10))
        items = resp if isinstance(resp, list) else [resp]
        for it in items:
            if it.get("T") == "error":
                raise RuntimeError(f"auth failed: {it.get('code')} {it.get('msg')}")

    async def _resubscribe(self) -> None:
        payload = {k: sorted(v) for k, v in self.subs.items() if v}
        if payload:
            await self._send({"action": "subscribe", **payload})

    async def subscribe(self, **channels: Iterable[str]) -> None:
        delta: dict[str, list[str]] = {}
        for ch, syms in channels.items():
            new = {s for s in syms if s} - self.subs.setdefault(ch, set())
            if new:
                self.subs[ch] |= new
                delta[ch] = sorted(new)
        if delta:
            await self._send({"action": "subscribe", **delta})

    async def unsubscribe(self, **channels: Iterable[str]) -> None:
        delta: dict[str, list[str]] = {}
        for ch, syms in channels.items():
            cur = self.subs.setdefault(ch, set())
            gone = {s for s in syms if s} & cur
            if gone:
                cur -= gone
                delta[ch] = sorted(gone)
        if delta:
            await self._send({"action": "unsubscribe", **delta})

    def _dispatch(self, m: dict[str, Any]) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class StockStream(_BaseStream):
    name = "stocks"

    def __init__(self, feed: str, key: str, secret: str, bus: Bus):
        super().__init__(STOCK_WS.format(feed=feed), key, secret, bus)

    def _dispatch(self, m: dict[str, Any]) -> None:
        typ = m.get("T")
        sym = m.get("S", "")
        if typ == "q":
            self.bus.publish(
                f"quote.{sym}",
                Quote(sym, _ts(m.get("t")), fnum(m.get("bp")), fnum(m.get("ap")), fnum(m.get("bs")), fnum(m.get("as"))),
            )
        elif typ == "t":
            self.bus.publish(
                f"trade.{sym}",
                Trade(sym, _ts(m.get("t")), fnum(m.get("p")), fnum(m.get("s")), str(m.get("x", "")), "".join(m.get("c") or [])),
            )
        elif typ in ("b", "u"):
            self.bus.publish(
                f"bar.{sym}",
                Bar(sym, _ts(m.get("t")), fnum(m.get("o")), fnum(m.get("h")), fnum(m.get("l")), fnum(m.get("c")),
                    fnum(m.get("v")), fnum(m.get("vw")), int(m.get("n") or 0)),
            )
        elif typ == "error":
            self.last_error = f"{m.get('code')} {m.get('msg')}"
            log.warning("stocks stream error message: %s", self.last_error)


class OptionStream(_BaseStream):
    name = "options"
    binary = True

    def __init__(self, feed: str, key: str, secret: str, bus: Bus):
        super().__init__(OPTION_WS.format(feed=feed), key, secret, bus)

    def _dispatch(self, m: dict[str, Any]) -> None:
        typ = m.get("T")
        sym = m.get("S", "")
        if typ == "q":
            self.bus.publish(
                f"oquote.{sym}",
                Quote(sym, _ts(m.get("t")), fnum(m.get("bp")), fnum(m.get("ap")), fnum(m.get("bs")), fnum(m.get("as"))),
            )
        elif typ == "t":
            self.bus.publish(
                f"otrade.{sym}",
                Trade(sym, _ts(m.get("t")), fnum(m.get("p")), fnum(m.get("s")), str(m.get("x", "")), str(m.get("c", ""))),
            )
        elif typ == "error":
            self.last_error = f"{m.get('code')} {m.get('msg')}"
            log.warning("options stream error message: %s", self.last_error)


class NewsStream(_BaseStream):
    name = "news"

    def __init__(self, key: str, secret: str, bus: Bus):
        super().__init__(NEWS_WS, key, secret, bus)
        self.subs = {"news": {"*"}}

    def _dispatch(self, m: dict[str, Any]) -> None:
        if m.get("T") == "n":
            self.bus.publish("news.raw", news_from_alpaca(m))


class TradeUpdatesStream(_BaseStream):
    """Order lifecycle events. Lossless consumers should subscribe to ``order`` with maxsize=0."""

    name = "trade_updates"

    def __init__(self, mode: str, key: str, secret: str, bus: Bus):
        super().__init__(TRADE_WS[mode], key, secret, bus)

    async def _handshake(self, ws: Any) -> None:
        await ws.send(orjson.dumps({"action": "auth", "key": self.key, "secret": self.secret}).decode())
        resp = self._decode(await asyncio.wait_for(ws.recv(), 10))
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        if data.get("status") not in ("authorized", None):
            raise RuntimeError(f"trade stream auth failed: {resp}")
        await ws.send(orjson.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}).decode())

    async def _resubscribe(self) -> None:
        return None

    def _decode(self, raw: bytes | str) -> Any:
        return orjson.loads(raw)

    def _dispatch(self, m: dict[str, Any]) -> None:
        if m.get("stream") != "trade_updates":
            return
        d = m.get("data") or {}
        order = normalize_order(d.get("order") or {})
        order["event"] = d.get("event")
        order["fill_price"] = fnum(d.get("price")) or None
        order["fill_qty"] = fnum(d.get("qty")) or None
        order["event_t"] = _ts(d.get("timestamp"))
        self.bus.publish("order", order)
