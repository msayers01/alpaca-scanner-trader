"""NewsService: ingest (stream + REST backfill), dedupe, score, persist, expose per-symbol sentiment.

Scoring is two-stage: an instant lexicon score (so nothing waits on the LLM), then an optional
LLM enrichment for relevant tickers. Enriched items are re-published with the same ``id``;
clients replace by id.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections import defaultdict, deque
from typing import Any

from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import NewsItem
from vantage.core.util import dumps, loads, now_ms
from vantage.llm import prompts
from vantage.llm.gateway import LLMGateway

log = logging.getLogger(__name__)

# Compact finance lexicon (inspired by Loughran & McDonald 2011), phrase -> weight
POSITIVE = {
    "beat": 1.0, "beats": 1.0, "tops": 0.8, "surge": 1.0, "surges": 1.0, "soar": 1.0, "soars": 1.0, "jumps": 0.8,
    "rally": 0.6, "rallies": 0.6, "upgrade": 1.0, "upgrades": 1.0, "raises guidance": 1.4, "raised guidance": 1.4,
    "guidance raised": 1.4, "lifts target": 0.8, "raises target": 0.8, "record": 0.5, "buyback": 0.8,
    "repurchase": 0.6, "approval": 0.9, "approved": 0.8, "wins": 0.6, "contract": 0.3, "outperform": 0.8,
    "strong demand": 0.8, "accelerat": 0.5, "profit": 0.3, "growth": 0.3, "dividend increase": 0.8,
    "acquire": 0.3, "partnership": 0.4, "breakthrough": 0.8, "exceeds": 0.8, "above estimates": 1.0,
}
NEGATIVE = {
    "miss": 1.0, "misses": 1.0, "plunge": 1.0, "plunges": 1.0, "tumble": 1.0, "tumbles": 1.0, "falls": 0.6,
    "slides": 0.6, "sinks": 0.8, "downgrade": 1.0, "downgrades": 1.0, "cuts guidance": 1.4, "cut guidance": 1.4,
    "lowers guidance": 1.4, "cuts outlook": 1.4, "cuts full-year": 1.2, "lawsuit": 0.7, "probe": 0.8,
    "investigation": 0.8, "recall": 0.8, "fraud": 1.2, "short seller": 0.9, "bankruptcy": 1.5, "layoffs": 0.5,
    "underperform": 0.8, "weak demand": 0.9, "below estimates": 1.0, "delay": 0.5, "halt": 0.7, "warning": 0.7,
    "departs": 0.5, "resigns": 0.6, "default": 1.0, "dilution": 0.8, "offering": 0.4, "sec charges": 1.2,
}
_WORD = re.compile(r"[a-z][a-z\-']+")


def lexicon_sentiment(text: str) -> tuple[float, float]:
    """Return (sentiment in [-1,1], strength in [0,1])."""
    t = text.lower()
    pos = sum(w for k, w in POSITIVE.items() if k in t)
    neg = sum(w for k, w in NEGATIVE.items() if k in t)
    if pos == 0 and neg == 0:
        return 0.0, 0.0
    s = (pos - neg) / (pos + neg + 1.0)
    strength = 1 - math.exp(-(pos + neg) / 1.5)
    return max(-1.0, min(1.0, s)), strength


class NewsService:
    def __init__(self, broker: Any, bus: Bus, store: Store, mode: str, llm: LLMGateway | None):
        self.broker = broker
        self.bus = bus
        self.store = store
        self.mode = mode
        self.llm = llm
        self.items: deque[NewsItem] = deque(maxlen=2000)
        self._seen: set[str] = set()
        self.by_symbol: dict[str, deque[tuple[int, float]]] = defaultdict(lambda: deque(maxlen=200))
        self.llm_symbols: set[str] = set()  # tickers worth spending LLM time on
        self._sub = bus.subscribe("news.raw", maxsize=0)
        self._q: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=200)
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self, symbols: list[str]) -> None:
        self.llm_symbols |= set(symbols)
        await self._load_persisted()
        self._tasks.append(asyncio.create_task(self._ingest(), name="news-ingest"))
        self._tasks.append(asyncio.create_task(self._enrich_loop(), name="news-llm"))
        try:
            backfill = await self.broker.get_news(symbols[:40], limit=50)
            for n in sorted(backfill, key=lambda x: x.t):
                self._accept(n, publish=False)
        except Exception as e:  # noqa: BLE001
            log.warning("news backfill failed: %s", e)

    async def stop(self) -> None:
        self._sub.close()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    async def _load_persisted(self) -> None:
        rows = await self.store.query(
            "SELECT data FROM news WHERE mode=? ORDER BY t DESC LIMIT 500", (self.mode,)
        )
        for r in reversed(rows):
            d = loads(r["data"])
            n = NewsItem(**d)
            if n.id not in self._seen:
                self._seen.add(n.id)
                self.items.append(n)
                self._index(n)

    async def _ingest(self) -> None:
        async for _, n in self._sub:
            self._accept(n, publish=True)

    def _accept(self, n: NewsItem, publish: bool) -> None:
        if n.id in self._seen:
            return
        self._seen.add(n.id)
        s, strength = lexicon_sentiment(f"{n.headline}. {n.summary}")
        n.sentiment = round(s, 3)
        n.relevance = round(strength, 3)
        self.items.append(n)
        self._index(n)
        self._persist(n)
        if publish:
            self.bus.publish("news", n)
        if self.llm and self.llm.enabled and set(n.symbols) & self.llm_symbols and len(n.symbols) <= 6:
            with contextlib.suppress(asyncio.QueueFull):
                self._q.put_nowait(n)

    def _index(self, n: NewsItem) -> None:
        if n.sentiment is None:
            return
        weight = n.sentiment * (n.relevance if n.relevance is not None else 0.5)
        per = {}
        if n.llm and isinstance(n.llm.get("per_symbol"), dict):
            per = n.llm["per_symbol"]
        for s in n.symbols:
            v = per.get(s, weight)
            self.by_symbol[s].append((n.t, float(v)))

    def _persist(self, n: NewsItem) -> None:
        self.store.write(
            "INSERT INTO news(id,mode,t,data) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (n.id, self.mode, n.t, dumps(n).decode()),
        )

    async def _enrich_loop(self) -> None:
        while True:
            n = await self._q.get()
            if now_ms() - n.t > 6 * 3600_000:
                continue
            assert self.llm is not None
            res = await self.llm.complete_json(
                "news",
                prompts.NEWS_SYSTEM,
                prompts.news_user(n.headline, n.summary, n.symbols),
                prompts.NEWS_SCHEMA,
                cache_ttl_s=3600,
                max_tokens=500,
            )
            if not res.ok or not res.data:
                continue
            items = [i for i in res.data.get("items", []) if i.get("symbol") in n.symbols]
            if not items:
                continue
            per = {
                i["symbol"]: float(i["direction"]) * float(i["magnitude"]) * float(i["relevance"])
                * (0.3 + 0.7 * float(i.get("novelty", 1)))
                for i in items
            }
            top = max(items, key=lambda i: i["relevance"])
            n.llm = {
                "direction": top["direction"],
                "magnitude": top["magnitude"],
                "horizon": top.get("horizon", "days"),
                "rationale": top.get("rationale", ""),
                "per_symbol": per,
                "latency_ms": res.latency_ms,
            }
            n.sentiment = round(float(top["direction"]) * float(top["magnitude"]), 3)
            n.relevance = round(float(top["relevance"]), 3)
            # replace lexicon entries for this item with LLM ones
            for s in n.symbols:
                dq = self.by_symbol[s]
                kept = [(t, v) for (t, v) in dq if t != n.t]
                dq.clear()
                dq.extend(kept)
                if s in per:
                    dq.append((n.t, per[s]))
            self._persist(n)
            self.bus.publish("news", n)

    # ------------------------------------------------------------------ queries
    def recent(self, symbol: str | None = None, limit: int = 100) -> list[NewsItem]:
        out = []
        for n in reversed(self.items):
            if symbol is None or symbol in n.symbols:
                out.append(n)
                if len(out) >= limit:
                    break
        return out

    def sentiment_score(self, symbol: str, half_life_h: float = 24.0, now: int | None = None) -> tuple[float, int]:
        """Exponentially decayed sum of item scores; returns (score, n_items_in_3_half_lives)."""
        now = now or now_ms()
        lam = math.log(2) / (half_life_h * 3600_000)
        tot = 0.0
        n = 0
        for t, v in self.by_symbol.get(symbol, ()):
            age = now - t
            if age < 0 or age > 3 * half_life_h * 3600_000:
                continue
            tot += v * math.exp(-lam * age)
            n += 1
        return tot, n
