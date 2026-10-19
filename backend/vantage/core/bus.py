"""In-process async pub/sub.

Topics are dotted strings (``quote.AAPL``). Subscriptions take a pattern that is either an exact
topic, a prefix ending in ``.*`` (``quote.*``) or ``*``. Market-data subscriptions use bounded
queues that drop the oldest item when full, so slow consumers never stall producers. Pass
``maxsize=0`` for lossless (order/account) consumers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

log = logging.getLogger(__name__)


class Subscription:
    __slots__ = ("pattern", "queue", "dropped", "_bus", "_closed")

    def __init__(self, bus: Bus, pattern: str, maxsize: int):
        self.pattern = pattern
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._bus = bus
        self._closed = False

    def matches(self, topic: str) -> bool:
        p = self.pattern
        if p == "*" or p == topic:
            return True
        return p.endswith(".*") and topic.startswith(p[:-1])

    def put(self, topic: str, payload: Any) -> None:
        q = self.queue
        if q.maxsize and q.full():
            try:
                q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
        q.put_nowait((topic, payload))

    async def get(self) -> tuple[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._remove(self)

    def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[tuple[str, Any]]:
        try:
            while not self._closed:
                yield await self.queue.get()
        finally:
            self.close()


class Bus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._callbacks: list[tuple[str, Callable[[str, Any], None]]] = []
        self.published = 0

    def subscribe(self, pattern: str, maxsize: int = 10_000) -> Subscription:
        sub = Subscription(self, pattern, maxsize)
        self._subs.append(sub)
        return sub

    def on(self, pattern: str, cb: Callable[[str, Any], None]) -> Callable[[], None]:
        """Synchronous callback subscription (called inline in publish; keep it cheap)."""
        entry = (pattern, cb)
        self._callbacks.append(entry)

        def off() -> None:
            if entry in self._callbacks:
                self._callbacks.remove(entry)

        return off

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, topic: str, payload: Any) -> None:
        self.published += 1
        for sub in self._subs:
            if sub.matches(topic):
                sub.put(topic, payload)
        for pattern, cb in self._callbacks:
            if pattern == "*" or pattern == topic or (pattern.endswith(".*") and topic.startswith(pattern[:-1])):
                try:
                    cb(topic, payload)
                except Exception:  # pragma: no cover - defensive
                    log.exception("bus callback failed for %s", topic)
