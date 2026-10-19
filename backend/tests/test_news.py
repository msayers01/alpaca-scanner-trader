"""NewsService: LLM enrichment must only replace its own lexicon entry and must survive malformed replies."""

from __future__ import annotations

from types import SimpleNamespace

from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import NewsItem
from vantage.core.util import now_ms
from vantage.data.news import NewsService
from vantage.llm.gateway import LLMResult


class FakeLLM:
    enabled = True

    def __init__(self, data):
        self.data = data

    async def complete_json(self, *a, **k):
        return LLMResult(True, self.data, "", 5)


def _svc(data):
    return NewsService(SimpleNamespace(), Bus(), Store(":memory:"), "sim", FakeLLM(data))


async def test_enrichment_replaces_only_its_own_entry_when_timestamps_collide():
    t = now_ms()
    svc = _svc({"items": [{"symbol": "AAPL", "direction": 1.0, "magnitude": 1.0, "relevance": 1.0, "novelty": 1.0}]})
    a = NewsItem("a", t, "Apple beats estimates", "", "x", "", ["AAPL"])
    b = NewsItem("b", t, "Apple surges on record sales", "", "x", "", ["AAPL"])  # same millisecond as `a`
    svc._accept(a, publish=False)
    svc._accept(b, publish=False)
    assert len(svc.by_symbol["AAPL"]) == 2
    await svc._enrich(a)
    entries = list(svc.by_symbol["AAPL"])
    assert len(entries) == 2 and {e[2] for e in entries} == {"a", "b"}  # b's lexicon entry survives
    assert a.llm is not None and a.llm["direction"] == 1.0


async def test_enrich_loop_survives_a_reply_that_breaks_the_consumer():
    import asyncio

    svc = _svc({"items": [{"symbol": "AAPL"}]})  # passes the symbol filter, then KeyError on 'direction'
    n = NewsItem("a", now_ms(), "Apple news", "", "x", "", ["AAPL"])
    svc.items.append(n)
    svc._q.put_nowait(n)
    task = asyncio.create_task(svc._enrich_loop())
    await asyncio.sleep(0.05)
    assert not task.done()  # the loop swallowed the error and is still running
    task.cancel()
