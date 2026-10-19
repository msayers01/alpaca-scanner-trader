"""CalendarService: earnings, FOMC, options expirations, market holidays/early closes, user events.

Sources (in order): Finnhub earnings calendar (if FINNHUB_API_KEY), Nasdaq public earnings
calendar, built-in FOMC schedule, rule-based OPEX/VIX expiry/holidays, user-entered events.
SIM mode synthesises quarterly earnings dates so event logic can be exercised offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import date, timedelta
from typing import Any

import httpx

from vantage.core import clock
from vantage.core.store import Store
from vantage.core.util import dumps, loads, new_id

log = logging.getLogger(__name__)

# Scheduled FOMC meetings (decision on the 2nd day, 14:00 ET). Source: federalreserve.gov calendar.
FOMC: list[tuple[date, bool]] = [  # (decision day, has Summary of Economic Projections)
    (date(2026, 1, 28), False), (date(2026, 3, 18), True), (date(2026, 4, 29), False),
    (date(2026, 6, 17), True), (date(2026, 7, 29), False), (date(2026, 9, 16), True),
    (date(2026, 10, 28), False), (date(2026, 12, 9), True),
    (date(2027, 1, 27), False), (date(2027, 3, 17), True), (date(2027, 4, 28), False), (date(2027, 6, 9), True),
]

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Vantage/0.1", "Accept": "application/json"}


def _ev(d: date, kind: str, title: str, importance: int, time_: str | None = None, symbol: str | None = None,
        source: str = "builtin", meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": f"{kind}:{symbol or ''}:{d.isoformat()}:{title[:20]}",
        "date": d.isoformat(),
        "time": time_,
        "kind": kind,
        "title": title,
        "symbol": symbol,
        "importance": importance,
        "meta": meta or {},
        "source": source,
    }


def builtin_events(start: date, end: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d, sep in FOMC:
        if start <= d <= end:
            out.append(_ev(d, "fomc", "FOMC rate decision" + (" + SEP / dot plot" if sep else ""), 3, "14:00",
                           meta={"press_conference": "14:30"}))
            out.append(_ev(d - timedelta(days=1), "fomc", "FOMC meeting begins", 1))
    for y in range(start.year, end.year + 1):
        for d, name in clock.nyse_holidays(y).items():
            if start <= d <= end:
                out.append(_ev(d, "holiday", f"Market closed — {name}", 2))
        for d, name in clock.nyse_early_closes(y).items():
            if start <= d <= end:
                out.append(_ev(d, "early_close", name, 2, "13:00"))
    # monthly OPEX (3rd Friday; Thursday if holiday), quad witching in quarter months, VIX expiry
    m = date(start.year, start.month, 1)
    while m <= end:
        tf = clock.third_friday(m.year, m.month)
        if not clock.is_trading_day(tf):
            tf -= timedelta(days=1)
        if start <= tf <= end:
            quad = m.month in (3, 6, 9, 12)
            out.append(_ev(tf, "opex", "Quad witching (index/stock options & futures expire)" if quad
                           else "Monthly options expiration", 3 if quad else 2))
        nm = date(m.year + (m.month // 12), m.month % 12 + 1, 1)
        vix = clock.third_friday(nm.year, nm.month) - timedelta(days=30)
        if not clock.is_trading_day(vix):
            vix = clock.prev_trading_day(vix)
        if start <= vix <= end:
            out.append(_ev(vix, "opex", "VIX options/futures expiration", 1, "09:30"))
        # Employment report is usually the first Friday; flagged as an estimate
        ff = date(m.year, m.month, 1)
        ff += timedelta(days=(4 - ff.weekday()) % 7)
        if start <= ff <= end and clock.is_trading_day(ff):
            out.append(_ev(ff, "macro", "Jobs report (est. — verify BLS schedule)", 2, "08:30", meta={"estimated": True}))
        m = nm
    return out


class CalendarService:
    def __init__(self, store: Store, mode: str, finnhub_key: str = ""):
        self.store = store
        self.mode = mode
        self.finnhub_key = finnhub_key
        self._client = httpx.AsyncClient(timeout=8.0, headers=UA)
        self._earn: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # date -> rows
        self.next_earnings: dict[str, date] = {}
        self._task: asyncio.Task[None] | None = None
        self.universe: list[str] = []

    async def start(self, universe: list[str]) -> None:
        self.universe = universe
        self._task = asyncio.create_task(self._refresh_loop(), name="calendar")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._client.aclose()

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh_earnings(clock.today_et(), clock.today_et() + timedelta(days=45))
            except Exception:  # noqa: BLE001
                log.exception("earnings refresh failed")
            await asyncio.sleep(6 * 3600)

    # ------------------------------------------------------------------ earnings
    async def refresh_earnings(self, start: date, end: date) -> None:
        if self.mode == "sim":
            self._sim_earnings(start, end)
            return
        rows: list[dict[str, Any]] = []
        if self.finnhub_key:
            rows = await self._finnhub(start, end)
        if not rows:
            d = start
            while d <= end:
                if d.weekday() < 5:
                    rows.extend(await self._nasdaq_day(d))
                    await asyncio.sleep(0.2)
                d += timedelta(days=1)
        for r in rows:
            sym = r["symbol"]
            dd = date.fromisoformat(r["date"])
            if dd >= clock.today_et() and (sym not in self.next_earnings or dd < self.next_earnings[sym]):
                self.next_earnings[sym] = dd

    async def _nasdaq_day(self, d: date) -> list[dict[str, Any]]:
        key = d.isoformat()
        hit = self._earn.get(key)
        if hit and time.monotonic() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            r = await self._client.get(NASDAQ_URL, params={"date": key})
            r.raise_for_status()
            data = (r.json().get("data") or {}).get("rows") or []
        except (httpx.HTTPError, ValueError) as e:
            log.info("nasdaq earnings %s failed: %s", key, e)
            data = []
        out = []
        for row in data:
            sym = (row.get("symbol") or "").upper()
            if not sym:
                continue
            tcode = row.get("time", "")
            when = "pre" if "pre" in tcode else "post" if "after" in tcode else None
            mcap = _parse_money(row.get("marketCap"))
            out.append({"symbol": sym, "date": key, "when": when, "name": row.get("name", ""),
                        "eps_est": row.get("epsForecast"), "market_cap": mcap})
        self._earn[key] = (time.monotonic(), out)
        return out

    async def _finnhub(self, start: date, end: date) -> list[dict[str, Any]]:
        try:
            r = await self._client.get(FINNHUB_URL, params={"from": start.isoformat(), "to": end.isoformat(),
                                                            "token": self.finnhub_key})
            r.raise_for_status()
            rows = r.json().get("earningsCalendar") or []
        except (httpx.HTTPError, ValueError) as e:
            log.info("finnhub earnings failed: %s", e)
            return []
        out = []
        for row in rows:
            hour = row.get("hour") or ""
            out.append({"symbol": row.get("symbol", "").upper(), "date": row.get("date"),
                        "when": "pre" if hour == "bmo" else "post" if hour == "amc" else None,
                        "name": "", "eps_est": row.get("epsEstimate"), "market_cap": None,
                        "revenue_est": row.get("revenueEstimate")})
            self._earn.setdefault(row.get("date"), (time.monotonic(), []))[1].append(out[-1])
        return out

    def _sim_earnings(self, start: date, end: date) -> None:
        for i, sym in enumerate(self.universe):
            if sym in ("SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "SMH", "XLK", "XLV", "TLT", "GLD"):
                continue
            # quarterly cadence with a per-symbol offset
            base = date(start.year, 1, 20) + timedelta(days=(sum(map(ord, sym)) * 7) % 40)
            for q in range(0, 9):
                d = base + timedelta(days=91 * q)
                while not clock.is_trading_day(d):
                    d += timedelta(days=1)
                if d >= clock.today_et():
                    self.next_earnings.setdefault(sym, d)
                    if start <= d <= end:
                        row = {"symbol": sym, "date": d.isoformat(), "when": "post" if i % 2 else "pre",
                               "name": sym, "eps_est": None, "market_cap": None}
                        rows = self._earn.setdefault(d.isoformat(), (time.monotonic(), []))[1]
                        if row not in rows:
                            rows.append(row)

    def days_to_earnings(self, symbol: str) -> int | None:
        d = self.next_earnings.get(symbol)
        return (d - clock.today_et()).days if d else None

    # ------------------------------------------------------------------ query API
    async def events(self, start: date, end: date, focus: set[str] | None = None) -> list[dict[str, Any]]:
        out = builtin_events(start, end)
        focus = focus or set(self.universe)
        for key, (_t, rows) in list(self._earn.items()):
            d = date.fromisoformat(key)
            if not (start <= d <= end):
                continue
            for r in rows:
                big = (r.get("market_cap") or 0) >= 50e9
                if r["symbol"] in focus or big:
                    title = f"{r['symbol']} earnings" + (" (before open)" if r["when"] == "pre" else
                                                         " (after close)" if r["when"] == "post" else "")
                    out.append(_ev(d, "earnings", title, 3 if r["symbol"] in focus else 2,
                                   "08:00" if r["when"] == "pre" else "16:05" if r["when"] == "post" else None,
                                   r["symbol"], "finnhub" if self.finnhub_key else ("sim" if self.mode == "sim" else "nasdaq"),
                                   {"eps_est": r.get("eps_est"), "market_cap": r.get("market_cap")}))
        rows = await self.store.query("SELECT data FROM events WHERE d BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))
        out.extend(loads(r["data"]) for r in rows)
        out.sort(key=lambda e: (e["date"], e["time"] or "99:99", -e["importance"]))
        return out

    async def add_user_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        d = date.fromisoformat(ev["date"])
        e = {
            "id": new_id("user-"),
            "date": d.isoformat(),
            "time": ev.get("time"),
            "kind": "user",
            "title": str(ev.get("title", ""))[:200],
            "symbol": (ev.get("symbol") or None),
            "importance": int(ev.get("importance", 2)),
            "meta": {"user_kind": ev.get("kind", "user")},
            "source": "user",
        }
        await self.store.awrite("INSERT INTO events(id,d,data) VALUES(?,?,?)", (e["id"], e["date"], dumps(e).decode()))
        return e

    async def delete_user_event(self, eid: str) -> None:
        await self.store.awrite("DELETE FROM events WHERE id=?", (eid,))

    def macro_blackout(self, now_et_date: date, minutes_to_event: float | None = None) -> bool:
        return any(d == now_et_date for d, _ in FOMC)


def _parse_money(s: Any) -> float | None:
    if not s or not isinstance(s, str):
        return None
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None
