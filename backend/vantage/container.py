"""Service container: builds and tears down the whole service graph for one trading mode."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from vantage import __version__
from vantage.broker.alpaca import AlpacaBroker
from vantage.broker.sim import SimBroker
from vantage.config import Settings
from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.util import now_ms
from vantage.data.calendar import CalendarService
from vantage.data.flow import FlowService
from vantage.data.market import MarketDataService
from vantage.data.news import NewsService
from vantage.data.options import OptionsService
from vantage.execution.engine import ExecutionEngine
from vantage.intel.service import IntelService
from vantage.llm.gateway import LLMGateway
from vantage.scanner.options_scanner import OptionsScanner
from vantage.signals.engine import SignalEngine

log = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "TSLA"]


class Container:
    def __init__(self, settings: Settings, store: Store, bus: Bus, mode: str):
        self.settings = settings
        self.store = store
        self.bus = bus
        self.mode = mode
        g = store.kv_get("global", "settings", {}) or {}
        self.stock_feed: str = g.get("stock_feed", settings.alpaca_stock_feed)
        self.options_feed: str = g.get("options_feed", settings.alpaca_options_feed)
        self._universe: list[str] = g.get("universe") or list(settings.universe)
        self._watchlist: list[str] = g.get("watchlist") or list(DEFAULT_WATCHLIST)
        self.ws_flush_ms: int = int(g.get("ws_flush_ms", settings.ws_flush_ms))
        llm_cfg = store.kv_get("global", "llm", {}) or {}
        self.llm = LLMGateway(
            llm_cfg.get("base_url", settings.llm_base_url), llm_cfg.get("model", settings.llm_model), store, mode,
            enabled=llm_cfg.get("enabled", settings.llm_enabled), timeout_s=settings.llm_timeout_s,
            max_concurrency=settings.llm_max_concurrency, temperature=llm_cfg.get("temperature", 0.1),
            bus=bus,
        )
        if mode == "sim":
            self.broker: Any = SimBroker(bus, self.all_symbols(), store, float(store.kv_get("sim", "initial_equity", 10_000.0)))
        else:
            key, secret = settings.keys_for(mode)
            if not (key and secret):
                raise RuntimeError(f"no API keys configured for {mode} mode")
            if mode == "live" and not settings.live_allowed:
                raise PermissionError("live trading not allowed (set ALLOW_LIVE_TRADING=true and live keys in .env)")
            self.broker = AlpacaBroker(mode, key, secret, bus, self.stock_feed, self.options_feed)
        self.market = MarketDataService(self.broker, bus, mode)
        self.options = OptionsService(self.broker, bus, store, mode, settings.risk_free_rate)
        self.news = NewsService(self.broker, bus, store, mode, self.llm)
        self.flow = FlowService(bus, store, mode, self.options)
        self.calendar = CalendarService(store, mode, settings.finnhub_api_key)
        self.intel = IntelService(bus, store, mode, settings.intel_env, self.signal_symbols, self.llm,
                                  enabled=settings.intel_enabled)
        self.signals = SignalEngine(self.market, self.options, self.news, self.flow, self.calendar, store, bus, mode,
                                    self.signal_symbols, intel=self.intel)
        self.scanner = OptionsScanner(self.market, self.options, self.signals, self.calendar, self.flow, bus,
                                      self.universe, self.watchlist, settings.risk_free_rate, mode)
        self.execution = ExecutionEngine(self.broker, bus, store, mode, self.quote_for)
        from vantage.autotrader.engine import AutoTrader

        self.autotrader = AutoTrader(self)
        self._tasks: list[asyncio.Task[None]] = []
        self.account: dict[str, Any] | None = None
        self.positions: list[dict[str, Any]] = []
        self.started = False
        self.last_error: str | None = None

    # ------------------------------------------------------------------ symbol sets
    def universe(self) -> list[str]:
        return list(self._universe)

    def watchlist(self) -> list[str]:
        return list(self._watchlist)

    def all_symbols(self) -> list[str]:
        return list(dict.fromkeys(self._universe + self._watchlist))

    def signal_symbols(self) -> list[str]:
        held = [p["underlying"] for p in self.positions]
        return list(dict.fromkeys(self._universe + self._watchlist + held))

    async def set_lists(self, universe: list[str] | None = None, watchlist: list[str] | None = None) -> None:
        old = set(self.all_symbols())
        if universe is not None:
            self._universe = [s.upper().strip() for s in universe if s.strip()][:200]
        if watchlist is not None:
            self._watchlist = [s.upper().strip() for s in watchlist if s.strip()][:100]
        new = set(self.all_symbols())
        if new - old:
            await self.market.acquire(sorted(new - old))
            self.news.llm_symbols |= new
        if old - new:
            await self.market.release(sorted(old - new))
        self.flow.universe = set(self._universe)
        self._persist_settings()

    def _persist_settings(self) -> None:
        self.store.kv_set("global", "settings", {
            "stock_feed": self.stock_feed, "options_feed": self.options_feed, "universe": self._universe,
            "watchlist": self._watchlist, "ws_flush_ms": self.ws_flush_ms,
        })

    # ------------------------------------------------------------------ helpers
    def market_state(self) -> clock.MarketState:
        ms = clock.market_state()
        if self.mode == "sim":  # the sim market never closes; its "day" rolls over at 04:00 ET
            from vantage.broker.sim import SimMarket

            day_end = SimMarket._day_start(ms.now) + 86_400_000
            ms.is_open = True
            ms.session = "regular"
            ms.next_close = day_end
            ms.next_open = day_end
            ms.minutes_since_open = ms.minutes_since_open or 120.0
            ms.minutes_to_close = ms.minutes_to_close or 240.0
        return ms

    def quote_for(self, symbol: str) -> tuple[float, float] | None:
        if len(symbol) > 15 and symbol[-9] in "CP":
            q = self.options.quotes.get(symbol)
            if q and q.bid > 0 and q.ask > 0 and now_ms() - q.t < 120_000:
                return q.bid, q.ask
            if self.mode == "sim":
                sq = self.broker.market.option_quote(symbol)
                return (sq.bid, sq.ask) if sq else None
            c = self.options.contracts.get(symbol)
            return (c.bid, c.ask) if c and c.bid > 0 else None
        q = self.market.quotes.get(symbol)
        if q and q.bid > 0 and q.ask > 0:
            return q.bid, q.ask
        if self.mode == "sim":
            sq = self.broker.market.quote(symbol)
            return sq.bid, sq.ask
        return None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.broker.start()
        syms = self.all_symbols()
        await self.market.start(syms)
        await self.news.start(syms)
        await self.flow.start(self._universe, realtime=bool(getattr(self.broker, "flow_realtime", False)),
                              broker=self.broker)
        await self.calendar.start(syms)
        await self.intel.start()
        await self.execution.start()
        await self.signals.start()
        await self.scanner.start()
        await self.autotrader.start()
        self._tasks.append(asyncio.create_task(self._account_loop(), name="account-poller"))
        self._tasks.append(asyncio.create_task(self.llm.warmup(), name="llm-warmup"))
        self.started = True

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
        for svc in (self.autotrader, self.scanner, self.signals, self.execution, self.intel, self.calendar, self.flow,
                    self.news, self.market):
            try:
                await svc.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping %s failed", type(svc).__name__)
        self.options.stop()
        await self.broker.stop()
        await self.llm.close()
        self.started = False

    async def _account_loop(self) -> None:
        interval = 1.0 if self.mode == "sim" else 3.0
        order_sub = self.bus.subscribe("order", maxsize=0)
        try:
            while True:
                try:
                    acct = await self.broker.get_account()
                    acct["tier"] = self.autotrader.tier
                    self.account = acct
                    pos = await self.broker.get_positions()
                    metas = await self.store.query("SELECT symbol, strategy_id, data FROM positions_meta WHERE mode=?",
                                                   (self.mode,))
                    mm = {r["symbol"]: r for r in metas}
                    for p in pos:
                        m = mm.get(p["symbol"])
                        if m:
                            p["strategy_id"] = m["strategy_id"]
                            from vantage.core.util import loads

                            d = loads(m["data"] or "{}")
                            ep = p["avg_entry_price"]
                            p["exit"] = {
                                "stop_price": ep * (1 - d["stop_pct"]) if d.get("stop_pct") else None,
                                "target_price": ep * (1 + d["target_pct"]) if d.get("target_pct") else None,
                                "trail_pct": d.get("trail_pct"),
                            }
                        if p.get("option"):
                            c = self.options.contracts.get(p["symbol"])
                            if c:
                                p["option"]["delta"] = c.delta
                                p["option"]["iv"] = c.iv
                    self.positions = pos
                    self.bus.publish("account", acct)
                    self.bus.publish("positions", pos)
                    if self.mode != "sim":
                        self.bus.publish("equity", {"t": now_ms(), "v": round(acct["equity"], 2)})
                    self.last_error = None
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    log.warning("account poll failed: %s", e)
                # wake early on order events so the UI reflects fills immediately
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(order_sub.get(), interval)
                    await asyncio.sleep(0.15)
                    while not order_sub.queue.empty():
                        order_sub.queue.get_nowait()
        finally:
            order_sub.close()

    def status(self) -> dict[str, Any]:
        ms = self.market_state()
        conn = self.broker.connection_state()
        return {
            "mode": self.mode,
            "live_allowed": self.settings.live_allowed,
            "paper_keys": self.settings.paper_keys,
            "live_keys": self.settings.live_keys,
            "market": ms.to_wire(),
            "feeds": {
                "stocks": self.stock_feed if self.mode != "sim" else "sim",
                "options": self.options_feed if self.mode != "sim" else "sim",
                "news": conn.get("news", False),
                "flow_realtime": bool(getattr(self.broker, "flow_realtime", False)),
                "connected": conn,
                "errors": self.broker.stream_errors() if hasattr(self.broker, "stream_errors") else {},
            },
            "autotrader": {
                "armed": self.autotrader.armed,
                "running": self.autotrader.running,
                "last_cycle": self.autotrader.last_cycle,
                "killed": self.autotrader.risk.killed,
                "kill_reason": self.autotrader.risk.kill_reason,
            },
            "intel": self.intel.status_bits(),
            "llm": {"enabled": self.llm.enabled, "available": self.llm.available, "model": self.llm.model,
                    "base_url": self.llm.base_url},
            "version": __version__,
            "error": self.last_error,
        }
