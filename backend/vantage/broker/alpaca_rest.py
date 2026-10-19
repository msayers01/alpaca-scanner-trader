"""Async Alpaca REST client (trading + market data). The ONLY module that knows Alpaca REST URLs.

Uses one pooled keep-alive httpx client per host so order submission pays no TCP/TLS setup.
A token-bucket limiter keeps us under the account's request quota (default 190/min trading,
data API shares a separate bucket).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from vantage.broker.base import BrokerError
from vantage.core.types import Bar, NewsItem, OptionContract, Quote, parse_occ
from vantage.core.util import clean_float, fnum, iso_to_ms, new_id

log = logging.getLogger(__name__)

TRADING_URL = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
DATA_URL = "https://data.alpaca.markets"

TIMEFRAMES = {"1Min", "5Min", "15Min", "30Min", "1Hour", "1Day", "1Week"}


class RateLimiter:
    def __init__(self, per_minute: int):
        self.capacity = per_minute
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def fmt_price(px: float) -> str:
    """Limit-price string: whole cents for prices >= $1 and for any price that is a whole number of cents (options), up to
    4 decimals for sub-dollar stocks (Alpaca accepts sub-penny increments below $1)."""
    if px >= 1.0:
        return f"{px:.2f}"
    s = f"{px:.4f}".rstrip("0")
    return s if len(s.split(".")[1]) >= 2 else f"{px:.2f}"


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _bar(symbol: str, b: dict[str, Any]) -> Bar:
    return Bar(
        symbol=symbol,
        t=iso_to_ms(b["t"]) or 0,
        o=fnum(b.get("o")),
        h=fnum(b.get("h")),
        l=fnum(b.get("l")),
        c=fnum(b.get("c")),
        v=fnum(b.get("v")),
        vw=fnum(b.get("vw")),
        n=int(b.get("n") or 0),
    )


def normalize_order(o: dict[str, Any]) -> dict[str, Any]:
    ac = o.get("asset_class") or "us_equity"
    return {
        "id": o.get("id"),
        "client_order_id": o.get("client_order_id"),
        "symbol": o.get("symbol"),
        "asset_class": "option" if ac == "us_option" else "equity",
        "side": o.get("side"),
        "qty": clean_float(o.get("qty")),
        "notional": clean_float(o.get("notional")),
        "filled_qty": fnum(o.get("filled_qty")),
        "filled_avg_price": clean_float(o.get("filled_avg_price")),
        "type": o.get("type") or o.get("order_type"),
        "limit_price": clean_float(o.get("limit_price")),
        "stop_price": clean_float(o.get("stop_price")),
        "status": o.get("status"),
        "time_in_force": o.get("time_in_force"),
        "submitted_at": iso_to_ms(o.get("submitted_at") or o.get("created_at")) or 0,
        "filled_at": iso_to_ms(o.get("filled_at")),
        "strategy_id": None,
    }


def normalize_position(p: dict[str, Any]) -> dict[str, Any]:
    ac = "option" if p.get("asset_class") == "us_option" else "equity"
    sym = p.get("symbol", "")
    occ = parse_occ(sym) if ac == "option" else None
    qty = fnum(p.get("qty"))
    out: dict[str, Any] = {
        "symbol": sym,
        "asset_class": ac,
        "underlying": occ[0] if occ else sym,
        "qty": abs(qty),
        "side": p.get("side") or ("long" if qty >= 0 else "short"),
        "avg_entry_price": fnum(p.get("avg_entry_price")),
        "current_price": fnum(p.get("current_price")),
        "market_value": fnum(p.get("market_value")),
        "cost_basis": fnum(p.get("cost_basis")),
        "unrealized_pl": fnum(p.get("unrealized_pl")),
        "unrealized_plpc": fnum(p.get("unrealized_plpc")),
        "change_today_pct": fnum(p.get("change_today")),
        "lastday_price": fnum(p.get("lastday_price")),
        "strategy_id": None,
    }
    if occ:
        out["option"] = {
            "type": occ[2],
            "strike": occ[3],
            "expiration": occ[1].isoformat(),
            "dte": (occ[1] - date.today()).days,
        }
    return out


def normalize_account(a: dict[str, Any]) -> dict[str, Any]:
    equity = fnum(a.get("equity"))
    last = fnum(a.get("last_equity")) or equity
    return {
        "equity": equity,
        "last_equity": last,
        "cash": fnum(a.get("cash")),
        "buying_power": fnum(a.get("buying_power")),
        "options_buying_power": fnum(a.get("options_buying_power"), fnum(a.get("buying_power"))),
        "daytrade_count": int(fnum(a.get("daytrade_count"))),
        "pattern_day_trader": bool(a.get("pattern_day_trader")),
        "daytrading_buying_power": fnum(a.get("daytrading_buying_power")),
        "day_pl": equity - last,
        "day_pl_pct": (equity - last) / last if last else 0.0,
        "status": a.get("status", ""),
        "currency": a.get("currency", "USD"),
        "options_level": int(fnum(a.get("options_trading_level") or a.get("options_approved_level"))),
        "multiplier": fnum(a.get("multiplier"), 1.0),
        "shorting_enabled": bool(a.get("shorting_enabled")),
        "trading_blocked": bool(a.get("trading_blocked")),
    }


class AlpacaREST:
    def __init__(self, mode: str, key: str, secret: str, stock_feed: str = "iex", options_feed: str = "indicative"):
        if mode not in TRADING_URL:
            raise ValueError(mode)
        self.mode = mode
        self.stock_feed = stock_feed
        self.options_feed = options_feed
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=120)
        timeout = httpx.Timeout(10.0, connect=5.0)
        self._trade = httpx.AsyncClient(base_url=TRADING_URL[mode], headers=headers, limits=limits, timeout=timeout)
        self._data = httpx.AsyncClient(base_url=DATA_URL, headers=headers, limits=limits, timeout=timeout)
        self._trade_rl = RateLimiter(190)
        self._data_rl = RateLimiter(190 if stock_feed == "iex" else 1000)
        self._assets: list[dict[str, Any]] | None = None
        self._assets_t = 0.0

    async def close(self) -> None:
        await self._trade.aclose()
        await self._data.aclose()

    async def _req(self, client: httpx.AsyncClient, rl: RateLimiter, method: str, path: str, **kw: Any) -> Any:
        params = kw.pop("params", None)
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            await rl.acquire()
            try:
                r = await client.request(method, path, params=params, **kw)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise BrokerError(f"network error: {e}") from e
                await asyncio.sleep(0.25 * 2**attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise BrokerError(f"{method} {path}: HTTP {r.status_code}", r.status_code)
                await asyncio.sleep(float(r.headers.get("retry-after", 0.5 * 2**attempt)))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                    msg = body.get("message") or str(body)
                    code = str(body.get("code", ""))
                except ValueError:
                    msg, code = r.text, ""
                raise BrokerError(msg, r.status_code, code)
            if r.status_code == 204 or not r.content:
                return None
            return r.json()
        raise BrokerError("unreachable")  # pragma: no cover

    def _t(self, method: str, path: str, **kw: Any) -> Any:
        return self._req(self._trade, self._trade_rl, method, path, **kw)

    def _d(self, method: str, path: str, **kw: Any) -> Any:
        return self._req(self._data, self._data_rl, method, path, **kw)

    # ================================================================ trading
    async def get_account(self) -> dict[str, Any]:
        return normalize_account(await self._t("GET", "/v2/account"))

    async def get_positions(self) -> list[dict[str, Any]]:
        return [normalize_position(p) for p in await self._t("GET", "/v2/positions")]

    async def get_orders(self, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._t(
            "GET", "/v2/orders", params={"status": status, "limit": min(limit, 500), "direction": "desc"}
        )
        return [normalize_order(o) for o in rows]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return normalize_order(await self._t("GET", f"/v2/orders/{order_id}"))

    async def submit_order(self, req: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": req["symbol"],
            "side": req["side"],
            "type": req.get("type", "limit"),
            "time_in_force": req.get("time_in_force", "day"),
            "client_order_id": req.get("client_order_id") or new_id("vt-"),
        }
        if req.get("notional") is not None:
            body["notional"] = f"{float(req['notional']):.2f}"
        else:
            q = float(req["qty"])
            body["qty"] = str(int(q)) if q == int(q) else f"{q:.6f}"
        if req.get("limit_price") is not None:
            body["limit_price"] = fmt_price(float(req["limit_price"]))
        if req.get("stop_price") is not None:
            body["stop_price"] = f"{float(req['stop_price']):.2f}"
        if req.get("extended_hours"):
            body["extended_hours"] = True
        if req.get("order_class"):
            body["order_class"] = req["order_class"]
            if req.get("stop_loss"):
                body["stop_loss"] = {"stop_price": f"{float(req['stop_loss']):.2f}"}
            if req.get("take_profit"):
                body["take_profit"] = {"limit_price": f"{float(req['take_profit']):.2f}"}
        try:
            return normalize_order(await self._t("POST", "/v2/orders", json=body))
        except BrokerError as e:
            # _req retries transport errors / 5xx. If an earlier attempt actually reached Alpaca, the retry is refused as a
            # duplicate client_order_id — but the order is live. Adopt it instead of reporting a failure, otherwise it would
            # fill untracked (no strategy attribution, no software stop).
            if e.status in (None, 422, 500, 502, 503, 504) or "client_order_id" in str(e).lower():
                found = await self._order_by_client_id(body["client_order_id"])
                if found is not None:
                    log.warning("order %s was accepted despite error (%s); adopting it", body["client_order_id"], e)
                    return found
            raise

    async def _order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            return normalize_order(await self._t("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}))
        except BrokerError:
            return None

    async def replace_order(
        self, order_id: str, limit_price: float | None = None, qty: float | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if limit_price is not None:
            body["limit_price"] = fmt_price(limit_price)
        if qty is not None:
            body["qty"] = str(int(qty)) if qty == int(qty) else f"{qty:.6f}"
        return normalize_order(await self._t("PATCH", f"/v2/orders/{order_id}", json=body))

    async def cancel_order(self, order_id: str) -> None:
        await self._t("DELETE", f"/v2/orders/{order_id}")

    async def close_position(self, symbol: str, pct: float = 1.0) -> dict[str, Any]:
        params = None if pct >= 0.999 else {"percentage": f"{pct * 100:.4f}"}
        return normalize_order(await self._t("DELETE", f"/v2/positions/{symbol}", params=params))

    async def get_portfolio_history(self, rng: str) -> dict[str, Any]:
        today = date.today()
        cfg: dict[str, Any]
        if rng == "1D":
            cfg = {"period": "1D", "timeframe": "5Min", "intraday_reporting": "extended_hours", "pnl_reset": "per_day"}
        elif rng == "1W":
            cfg = {"period": "1W", "timeframe": "15Min", "intraday_reporting": "market_hours", "pnl_reset": "no_reset"}
        elif rng == "1M":
            cfg = {"period": "30D", "timeframe": "1H", "intraday_reporting": "market_hours", "pnl_reset": "no_reset"}
        elif rng == "3M":
            cfg = {"period": "3M", "timeframe": "1D"}
        elif rng == "YTD":
            cfg = {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat(), "timeframe": "1D"}
        elif rng == "1Y":
            cfg = {"period": "1A", "timeframe": "1D"}
        else:
            cfg = {"period": "5A", "timeframe": "1D"}
        raw = await self._t("GET", "/v2/account/portfolio/history", params=cfg)
        ts = raw.get("timestamp") or []
        eq = raw.get("equity") or []
        points = [
            {"t": int(t) * 1000, "v": float(v)} for t, v in zip(ts, eq, strict=False) if v is not None and v > 0
        ]
        base = raw.get("base_value")
        if rng != "1D" or not base:
            base = points[0]["v"] if points else 0.0
        return {"range": rng, "timeframe": raw.get("timeframe", cfg["timeframe"]), "base_value": float(base), "points": points}

    async def get_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        return await self._t("GET", "/v2/calendar", params={"start": start.isoformat(), "end": end.isoformat()})

    async def get_clock(self) -> dict[str, Any]:
        return await self._t("GET", "/v2/clock")

    async def get_fills(self, after: date | None = None) -> list[dict[str, Any]]:
        params = {"activity_types": "FILL", "direction": "desc", "page_size": 100}
        if after:
            params["after"] = after.isoformat()
        return await self._t("GET", "/v2/account/activities", params=params)

    async def search_assets(self, q: str) -> list[dict[str, Any]]:
        if self._assets is None or time.monotonic() - self._assets_t > 6 * 3600:
            rows = await self._t("GET", "/v2/assets", params={"status": "active", "asset_class": "us_equity"})
            self._assets = [
                {"symbol": a["symbol"], "name": a.get("name", ""), "exchange": a.get("exchange", "")}
                for a in rows
                if a.get("tradable")
            ]
            self._assets_t = time.monotonic()
        return rank_assets(self._assets, q)

    # ================================================================ market data — stocks
    async def get_bars(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None, limit: int = 10000
    ) -> list[Bar]:
        res = await self.get_bars_multi([symbol], timeframe, start_ms, end_ms, limit)
        return res.get(symbol, [])

    async def get_bars_multi(
        self, symbols: list[str], timeframe: str, start_ms: int, end_ms: int | None = None, limit: int = 10000
    ) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        token = None
        pages = 0
        while True:
            raw = await self._d(
                "GET",
                "/v2/stocks/bars",
                params={
                    "symbols": ",".join(symbols),
                    "timeframe": timeframe,
                    "start": _iso(start_ms),
                    "end": _iso(end_ms) if end_ms else None,
                    "limit": 10000,
                    "adjustment": "all",
                    "feed": self.stock_feed,
                    "sort": "asc",
                    "page_token": token,
                },
            )
            for sym, bars in (raw.get("bars") or {}).items():
                out.setdefault(sym, []).extend(_bar(sym, b) for b in bars)
            token = raw.get("next_page_token")
            pages += 1
            if not token or pages > 50 or all(len(v) >= limit for v in out.values()):
                break
        return out

    async def get_snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i : i + 100]
            raw = await self._d("GET", "/v2/stocks/snapshots", params={"symbols": ",".join(chunk), "feed": self.stock_feed})
            for sym, s in (raw or {}).items():
                if not s:
                    continue
                q = s.get("latestQuote") or {}
                tr = s.get("latestTrade") or {}
                day = s.get("dailyBar")
                prev = s.get("prevDailyBar")
                last = fnum(tr.get("p")) or fnum((day or {}).get("c"))
                out[sym] = {
                    "symbol": sym,
                    "quote": Quote(sym, iso_to_ms(q.get("t")) or 0, fnum(q.get("bp")), fnum(q.get("ap")),
                                   fnum(q.get("bs")), fnum(q.get("as"))),
                    "last": last,
                    "t": iso_to_ms(tr.get("t")) or 0,
                    "prev_close": fnum((prev or {}).get("c")),
                    "day": _bar(sym, day) if day else None,
                    "minute": _bar(sym, s["minuteBar"]) if s.get("minuteBar") else None,
                    "volume": fnum((day or {}).get("v")),
                }
        return out

    async def get_most_actives(self, top: int = 20) -> list[str]:
        raw = await self._d("GET", "/v1beta1/screener/stocks/most-actives", params={"by": "volume", "top": top})
        return [r["symbol"] for r in raw.get("most_actives", [])]

    async def get_movers(self, top: int = 20) -> dict[str, list[dict[str, Any]]]:
        return await self._d("GET", "/v1beta1/screener/stocks/movers", params={"top": top})

    # ================================================================ market data — options
    async def get_option_contracts(
        self, underlying: str, exp_gte: date | None = None, exp_lte: date | None = None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        for _ in range(20):
            raw = await self._t(
                "GET",
                "/v2/options/contracts",
                params={
                    "underlying_symbols": underlying,
                    "status": "active",
                    "expiration_date_gte": (exp_gte or date.today()).isoformat(),
                    "expiration_date_lte": exp_lte.isoformat() if exp_lte else None,
                    "limit": 10000,
                    "page_token": token,
                },
            )
            out.extend(raw.get("option_contracts") or [])
            token = raw.get("next_page_token")
            if not token:
                break
        return out

    async def get_option_expirations(self, underlying: str) -> list[date]:
        rows = await self.get_option_contracts(underlying, date.today(), date.today() + timedelta(days=400))
        exps = sorted({date.fromisoformat(r["expiration_date"]) for r in rows if r.get("expiration_date")})
        return exps

    async def get_option_chain(
        self, underlying: str, exp_gte: date | None = None, exp_lte: date | None = None
    ) -> list[OptionContract]:
        """Snapshots (quotes, greeks, IV) joined with contract metadata (open interest)."""
        contracts_task = asyncio.create_task(self.get_option_contracts(underlying, exp_gte, exp_lte))
        snaps: dict[str, Any] = {}
        token = None
        for _ in range(30):
            raw = await self._d(
                "GET",
                f"/v1beta1/options/snapshots/{underlying}",
                params={
                    "feed": self.options_feed,
                    "limit": 1000,
                    "expiration_date_gte": exp_gte.isoformat() if exp_gte else None,
                    "expiration_date_lte": exp_lte.isoformat() if exp_lte else None,
                    "page_token": token,
                },
            )
            snaps.update(raw.get("snapshots") or {})
            token = raw.get("next_page_token")
            if not token:
                break
        try:
            meta = {c["symbol"]: c for c in await contracts_task}
        except BrokerError as e:
            log.warning("contracts lookup failed for %s: %s", underlying, e)
            meta = {}
        out: list[OptionContract] = []
        for sym, s in snaps.items():
            occ = parse_occ(sym)
            if not occ:
                continue
            q = s.get("latestQuote") or {}
            tr = s.get("latestTrade") or {}
            g = s.get("greeks") or {}
            day = s.get("dailyBar") or {}
            m = meta.get(sym, {})
            out.append(
                OptionContract(
                    symbol=sym,
                    underlying=underlying,
                    type=occ[2],
                    strike=occ[3],
                    expiration=occ[1],
                    bid=fnum(q.get("bp")),
                    ask=fnum(q.get("ap")),
                    last=fnum(tr.get("p")) or fnum(m.get("close_price")),
                    volume=fnum(day.get("v")),
                    open_interest=fnum(m.get("open_interest")),
                    iv=clean_float(s.get("impliedVolatility")),
                    delta=clean_float(g.get("delta")),
                    gamma=clean_float(g.get("gamma")),
                    theta=clean_float(g.get("theta")),
                    vega=clean_float(g.get("vega")),
                    t=iso_to_ms(q.get("t")) or 0,
                )
            )
        return out

    async def get_option_bars(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None
    ) -> list[Bar]:
        out: list[Bar] = []
        token = None
        for _ in range(20):
            raw = await self._d(
                "GET",
                "/v1beta1/options/bars",
                params={
                    "symbols": symbol,
                    "timeframe": timeframe,
                    "start": _iso(start_ms),
                    "end": _iso(end_ms) if end_ms else None,
                    "limit": 10000,
                    "sort": "asc",
                    "page_token": token,
                },
            )
            out.extend(_bar(symbol, b) for b in (raw.get("bars") or {}).get(symbol, []))
            token = raw.get("next_page_token")
            if not token:
                break
        return out

    # ================================================================ news
    async def get_news(self, symbols: list[str] | None, limit: int = 50) -> list[NewsItem]:
        raw = await self._d(
            "GET",
            "/v1beta1/news",
            params={
                "symbols": ",".join(symbols) if symbols else None,
                "limit": min(limit, 50),
                "sort": "desc",
                "include_content": "false",
            },
        )
        return [news_from_alpaca(n) for n in raw.get("news", [])]


def news_from_alpaca(n: dict[str, Any]) -> NewsItem:
    return NewsItem(
        id=str(n.get("id")),
        t=iso_to_ms(n.get("created_at") or n.get("updated_at")) or 0,
        headline=n.get("headline", ""),
        summary=n.get("summary", "") or "",
        source=n.get("source", "") or n.get("author", ""),
        url=n.get("url", "") or "",
        symbols=list(n.get("symbols") or []),
    )


def rank_assets(assets: list[dict[str, Any]], q: str) -> list[dict[str, Any]]:
    q = q.strip().upper()
    if not q:
        return []
    exact, prefix, name = [], [], []
    for a in assets:
        s = a["symbol"]
        if s == q:
            exact.append(a)
        elif s.startswith(q):
            prefix.append(a)
        elif q in a.get("name", "").upper():
            name.append(a)
    prefix.sort(key=lambda a: len(a["symbol"]))
    return (exact + prefix + name)[:20]
