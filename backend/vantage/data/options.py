"""OptionsService: cached chains/expirations, ATM IV term structure, daily IV history."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np

from vantage.broker.base import Broker
from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import OptionContract, Quote

log = logging.getLogger(__name__)


class OptionsService:
    def __init__(self, broker: Broker, bus: Bus, store: Store, mode: str, risk_free: float = 0.04):
        self.broker = broker
        self.bus = bus
        self.store = store
        self.mode = mode
        self.r = risk_free
        self._chains: dict[tuple[str, date | None, date | None], tuple[float, list[OptionContract]]] = {}
        self._exps: dict[str, tuple[float, list[date]]] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.quotes: dict[str, Quote] = {}
        self.contracts: dict[str, OptionContract] = {}  # latest snapshot per OCC symbol
        self._off = bus.on("oquote.*", self._on_quote)
        self.chain_ttl = 20.0 if mode == "sim" else 45.0

    def stop(self) -> None:
        self._off()

    def _on_quote(self, topic: str, q: Quote) -> None:
        self.quotes[q.symbol] = q

    async def expirations(self, underlying: str) -> list[date]:
        u = underlying.upper()
        hit = self._exps.get(u)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
        exps = await self.broker.get_option_expirations(u)
        self._exps[u] = (time.monotonic(), exps)
        return exps

    async def chain(
        self, underlying: str, exp_gte: date | None = None, exp_lte: date | None = None, max_age: float | None = None
    ) -> list[OptionContract]:
        u = underlying.upper()
        key = (u, exp_gte, exp_lte)
        ttl = self.chain_ttl if max_age is None else max_age
        async with self._locks[u]:
            hit = self._chains.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            contracts = await self.broker.get_option_chain(u, exp_gte, exp_lte)
            self._chains[key] = (time.monotonic(), contracts)
            for ct in contracts:
                self.contracts[ct.symbol] = ct
            return contracts

    async def chain_for_expiration(self, underlying: str, expiration: date, spot: float) -> dict[str, Any]:
        contracts = await self.chain(underlying, expiration, expiration)
        calls = sorted((c for c in contracts if c.type == "call"), key=lambda c: c.strike)
        puts = sorted((c for c in contracts if c.type == "put"), key=lambda c: c.strike)
        dte = (expiration - clock.today_et()).days
        atm = atm_iv(contracts, spot)
        em = spot * atm * math.sqrt(max(dte, 0.5) / 365) if atm else None
        return {
            "underlying": underlying.upper(),
            "spot": spot,
            "expiration": expiration.isoformat(),
            "dte": dte,
            "calls": [c.to_wire() for c in calls],
            "puts": [c.to_wire() for c in puts],
            "atm_iv": atm,
            "expected_move": em,
        }

    def record_iv(self, underlying: str, iv: float | None, rv: float | None) -> None:
        if iv is None or not math.isfinite(iv):
            return
        self.store.write(
            "INSERT INTO iv_history(mode,symbol,d,atm_iv,rv) VALUES(?,?,?,?,?) "
            "ON CONFLICT(mode,symbol,d) DO UPDATE SET atm_iv=excluded.atm_iv, rv=excluded.rv",
            (self.mode, underlying, clock.today_et().isoformat(), iv, rv),
        )

    async def iv_history(self, underlying: str, days: int = 252) -> list[float]:
        rows = await self.store.query(
            "SELECT atm_iv FROM iv_history WHERE mode=? AND symbol=? ORDER BY d DESC LIMIT ?",
            (self.mode, underlying, days),
        )
        return [r["atm_iv"] for r in reversed(rows) if r["atm_iv"] is not None]


def atm_iv(contracts: list[OptionContract], spot: float, target_dte: int | None = None) -> float | None:
    """Average call/put IV at the strike nearest spot, for the expiration nearest target_dte (or nearest)."""
    if not contracts or spot <= 0:
        return None
    today = clock.today_et()
    exps = sorted({c.expiration for c in contracts if (c.expiration - today).days >= 1})
    if not exps:
        return None
    if target_dte is None:
        exp = exps[0]
    else:
        exp = min(exps, key=lambda e: abs((e - today).days - target_dte))
    sub = [c for c in contracts if c.expiration == exp and c.iv and c.iv > 0]
    if not sub:
        return None
    k = min({c.strike for c in sub}, key=lambda s: abs(s - spot))
    ivs = [c.iv for c in sub if c.strike == k and c.iv]
    return float(np.mean(ivs)) if ivs else None


def term_structure(contracts: list[OptionContract], spot: float) -> list[tuple[int, float]]:
    today = clock.today_et()
    out = []
    for e in sorted({c.expiration for c in contracts}):
        dte = (e - today).days
        if dte < 1:
            continue
        iv = atm_iv([c for c in contracts if c.expiration == e], spot)
        if iv:
            out.append((dte, iv))
    return out
