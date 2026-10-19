"""Process-wide state: settings, store, bus, and the active Container (one trading mode at a time)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vantage.config import Settings
from vantage.container import Container
from vantage.core.bus import Bus
from vantage.core.store import Store

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.data_dir / "vantage.db")
        self.bus = Bus()
        self.container: Container | None = None
        self._lock = asyncio.Lock()
        self.hub: Any = None
        self.mode_error: str | None = None

    def initial_mode(self) -> str:
        saved = self.store.kv_get("global", "mode")
        m = saved or self.settings.vantage_mode
        if m == "live" and not self.settings.live_allowed:
            m = "paper" if self.settings.paper_keys else "sim"
        if m == "paper" and not self.settings.paper_keys:
            m = "sim"
        return m

    async def switch_mode(self, mode: str) -> Container:
        async with self._lock:
            if self.container is not None and self.container.mode == mode and self.container.started:
                return self.container
            old = self.container
            if old is not None:
                await old.stop()
                self.container = None
            try:
                c = Container(self.settings, self.store, self.bus, mode)
                await c.start()
            except Exception as e:
                self.mode_error = str(e)
                log.exception("failed to start %s mode", mode)
                if mode != "sim":
                    c = Container(self.settings, self.store, self.bus, "sim")
                    await c.start()
                else:
                    raise
            else:
                self.mode_error = None
            self.container = c
            self.store.kv_set("global", "mode", c.mode)
            if self.hub is not None:
                await self.hub.resubscribe_all()
            return c

    async def shutdown(self) -> None:
        if self.container is not None:
            await self.container.stop()
            self.container = None
        self.store.close()
