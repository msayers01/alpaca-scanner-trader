from __future__ import annotations

from vantage.core.bus import Bus
from vantage.data.market import MarketDataService


class FakeBroker:
    stock_feed = "iex"

    def __init__(self):
        self.quotes: set[str] = set()
        self.bars: set[str] = set()
        self.trades: set[str] = set()

    async def stream_subscribe(self, quotes=(), bars=(), trades=()):
        self.quotes |= set(quotes)
        self.bars |= set(bars)
        self.trades |= set(trades)

    async def stream_unsubscribe(self, quotes=(), bars=(), trades=()):
        self.quotes -= set(quotes)
        self.bars -= set(bars)
        self.trades -= set(trades)


async def test_iex_quote_budget_prioritises_ui_symbols():
    b = FakeBroker()
    m = MarketDataService(b, Bus(), "paper")
    core = [f"S{i:02d}" for i in range(35)]
    await m.acquire(core)
    assert len(b.quotes) == 30 and b.bars == set(core) and not b.trades
    assert "S34" not in b.quotes  # beyond the budget -> bars + REST polling only
    await m.acquire(["ZZZ"], "ui")
    assert "ZZZ" in b.quotes and len(b.quotes) == 30
    assert "S29" not in b.quotes  # lowest-priority core symbol evicted
    await m.release(["ZZZ"], "ui")
    assert "ZZZ" not in b.quotes and "ZZZ" not in b.bars and "S29" in b.quotes


async def test_sip_streams_everything():
    b = FakeBroker()
    b.stock_feed = "sip"
    m = MarketDataService(b, Bus(), "paper")
    syms = [f"S{i:02d}" for i in range(40)]
    await m.acquire(syms)
    assert b.quotes == set(syms) == b.trades == b.bars
