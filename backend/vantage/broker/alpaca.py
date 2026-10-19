"""AlpacaBroker = AlpacaREST + streams, implementing the Broker protocol for paper/live."""

from __future__ import annotations

import logging
from typing import Any

from vantage.broker.alpaca_rest import AlpacaREST
from vantage.broker.alpaca_stream import NewsStream, OptionStream, StockStream, TradeUpdatesStream
from vantage.core.bus import Bus

log = logging.getLogger(__name__)


class AlpacaBroker(AlpacaREST):
    def __init__(self, mode: str, key: str, secret: str, bus: Bus, stock_feed: str, options_feed: str):
        super().__init__(mode, key, secret, stock_feed, options_feed)
        self.bus = bus
        self.stocks = StockStream(stock_feed, key, secret, bus)
        self.options = OptionStream(options_feed, key, secret, bus)
        self.news = NewsStream(key, secret, bus)
        self.trades = TradeUpdatesStream(mode, key, secret, bus)
        self.flow_realtime = options_feed == "opra"

    async def start(self) -> None:
        for s in (self.stocks, self.options, self.news, self.trades):
            s.start()

    async def stop(self) -> None:
        for s in (self.stocks, self.options, self.news, self.trades):
            await s.stop()
        await self.close()

    async def stream_subscribe(self, quotes=(), bars=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        await self.stocks.subscribe(quotes=quotes, bars=bars, updatedBars=bars, trades=trades)

    async def stream_unsubscribe(self, quotes=(), bars=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        await self.stocks.unsubscribe(quotes=quotes, bars=bars, updatedBars=bars, trades=trades)

    async def options_subscribe(self, quotes=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        await self.options.subscribe(quotes=quotes, trades=trades)

    async def options_unsubscribe(self, quotes=(), trades=()) -> None:  # type: ignore[no-untyped-def]
        await self.options.unsubscribe(quotes=quotes, trades=trades)

    def connection_state(self) -> dict[str, bool]:
        return {
            "stocks": self.stocks.connected,
            "options": self.options.connected,
            "news": self.news.connected,
            "trade_updates": self.trades.connected,
        }

    def stream_errors(self) -> dict[str, Any]:
        return {s.name: s.last_error for s in (self.stocks, self.options, self.news, self.trades) if s.last_error}
