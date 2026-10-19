"""Collectors for the intel layer. Each source polls ONE public endpoint and returns a ``Batch``.

Only documented / public interfaces are used — no HTML scraping and no logged-in sessions:
* Reddit via the official OAuth API (client-credentials app) with a public-JSON fallback;
* X via the official v2 recent-search API (needs a bearer token; there is no free read tier);
* Stocktwits public symbol streams (user-tagged bullish/bearish), ApeWisdom (aggregated Reddit mention counts);
* RSS/Atom feeds (Fed, SEC, White House, a Truth Social archive feed, Google News queries for named market movers);
* Polymarket (gamma) and Kalshi public market data; VIX family from Yahoo's chart endpoint with a CBOE CSV fallback.

Network errors are raised to the service, which records them in the source status; they never reach the trading loop.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import httpx
from defusedxml import ElementTree as ET

from vantage.core.util import iso_to_ms, now_ms
from vantage.intel import text as tx
from vantage.intel.types import Batch, IntelItem, MentionSnap, PredictionMarket

log = logging.getLogger(__name__)

UA = "Vantage/0.1 (personal trading workstation; research use)"


@dataclass
class SourceCtx:
    client: httpx.AsyncClient
    universe: frozenset[str]
    cfg: dict[str, Any]
    env: dict[str, str]
    scratch: dict[str, Any] = field(default_factory=dict)  # per-source mutable state (cursors, tokens, caches)


class Source:
    id: str = ""
    label: str = ""
    kind: str = "social"  # social | statement | prediction | volatility
    interval_s: float = 300.0
    needs: str | None = None  # env var names that must be set
    note: str = ""

    def configured(self, env: dict[str, str]) -> bool:
        return all(env.get(k) for k in (self.needs or "").replace(" ", "").split("/") if k)

    async def poll(self, ctx: SourceCtx) -> Batch:  # pragma: no cover - interface
        raise NotImplementedError


def _get_json(r: httpx.Response) -> Any:
    r.raise_for_status()
    return r.json()


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ============================================================================ social
class RedditSource(Source):
    id, label, kind, interval_s = "reddit", "Reddit (official API)", "social", 120.0
    needs = "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"
    note = ("Subreddit posts via Reddit's OAuth API. Without credentials the public JSON endpoint is tried, which Reddit "
            "frequently blocks (403/429) — create a free 'script' app at reddit.com/prefs/apps for reliable access.")

    def configured(self, env: dict[str, str]) -> bool:
        return True  # degrades to the public endpoint

    async def _token(self, ctx: SourceCtx) -> str | None:
        cid, sec = ctx.env.get("REDDIT_CLIENT_ID"), ctx.env.get("REDDIT_CLIENT_SECRET")
        if not (cid and sec):
            return None
        tok = ctx.scratch.get("token")
        if tok and tok[1] > now_ms() + 60_000:
            return tok[0]
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        r = await ctx.client.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                                  headers={"Authorization": f"Basic {basic}", "User-Agent": ctx.env.get("REDDIT_USER_AGENT") or UA})
        d = _get_json(r)
        ctx.scratch["token"] = (d["access_token"], now_ms() + int(d.get("expires_in", 3600)) * 1000)
        return str(d["access_token"])

    async def poll(self, ctx: SourceCtx) -> Batch:
        subs = [s for s in ctx.cfg.get("subreddits", []) if isinstance(s, str) and s.strip()][:12]
        tok = await self._token(ctx)
        ua = ctx.env.get("REDDIT_USER_AGENT") or UA
        base = "https://oauth.reddit.com" if tok else "https://www.reddit.com"
        headers = {"User-Agent": ua} | ({"Authorization": f"Bearer {tok}"} if tok else {})
        suffix = "" if tok else ".json"

        async def one(sub: str) -> list[IntelItem]:
            r = await ctx.client.get(f"{base}/r/{sub}/new{suffix}", params={"limit": 50, "raw_json": 1}, headers=headers)
            out = []
            for ch in _get_json(r).get("data", {}).get("children", []):
                d = ch.get("data", {})
                if not d.get("id"):
                    continue
                body = tx.clean_text(f"{d.get('title', '')}. {d.get('selftext', '')[:600]}")
                out.append(IntelItem(
                    id=f"reddit:{d['id']}", t=int(_f(d.get("created_utc")) * 1000), kind="social", source="reddit",
                    author=f"r/{sub}", text=tx.short(body, 400), url="https://www.reddit.com" + d.get("permalink", ""),
                    engagement=_f(d.get("score")) + _f(d.get("num_comments")) * 2,
                ))
            return out

        res = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
        items = [i for r in res if isinstance(r, list) for i in r]
        errs = [r for r in res if isinstance(r, Exception)]
        if errs and not items:
            raise errs[0]
        return Batch(items=items)


class ApeWisdomSource(Source):
    id, label, kind, interval_s = "apewisdom", "ApeWisdom (Reddit mention counts)", "social", 300.0
    note = "Aggregated ticker mention counts across Reddit finance subs (free, no key) — attention without scraping Reddit."

    async def poll(self, ctx: SourceCtx) -> Batch:
        snaps: list[MentionSnap] = []
        t = now_ms()
        for page in (1, 2, 3):
            d = _get_json(await ctx.client.get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"))
            for r in d.get("results", []):
                sym = str(r.get("ticker", "")).upper()
                if sym and (sym in ctx.universe or int(r.get("rank", 999)) <= 60):
                    snaps.append(MentionSnap(sym, t, "apewisdom", _f(r.get("mentions")), _f(r.get("mentions_24h_ago")),
                                             _f(r.get("upvotes")), int(r.get("rank", 0)) or None,
                                             int(r["rank_24h_ago"]) if r.get("rank_24h_ago") else None))
            if page >= int(d.get("pages", 1)):
                break
        return Batch(mentions=snaps)


class StocktwitsSource(Source):
    id, label, kind, interval_s = "stocktwits", "Stocktwits", "social", 90.0
    note = "Public symbol streams with the author's own bullish/bearish tag. Unauthenticated limit ≈200 req/h, so symbols are polled round-robin."
    BATCH = 4

    async def poll(self, ctx: SourceCtx) -> Batch:
        syms = sorted(ctx.universe)
        if not syms:
            return Batch()
        i = ctx.scratch.get("i", 0) % len(syms)
        ctx.scratch["i"] = i + self.BATCH
        pick = [syms[(i + k) % len(syms)] for k in range(min(self.BATCH, len(syms)))]
        items: list[IntelItem] = []
        errors: list[Exception] = []
        for sym in pick:
            try:
                d = _get_json(await ctx.client.get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                continue
            for m in d.get("messages", []):
                label = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                lab = label.lower() if isinstance(label, str) else None
                items.append(IntelItem(
                    id=f"stocktwits:{m['id']}", t=iso_to_ms(m.get("created_at")) or now_ms(), kind="social",
                    source="stocktwits", author=(m.get("user") or {}).get("username", ""),
                    text=tx.short(tx.clean_text(m.get("body", "")), 400), url=f"https://stocktwits.com/message/{m['id']}",
                    symbols=[sym], engagement=_f((m.get("likes") or {}).get("total")) + _f((m.get("user") or {}).get("followers")) / 500,
                    label=lab if lab in ("bullish", "bearish") else None,
                ))
        if errors and not items:
            raise errors[0]
        return Batch(items=items)


class XSource(Source):
    id, label, kind, interval_s = "x", "X (official API v2)", "social", 900.0
    needs = "X_BEARER_TOKEN"
    note = ("Recent search via the official X API v2: posts by the configured handles (as statements) and cashtag chatter "
            "for the watched symbols. X has no free read tier and logged-out scraping is blocked and against its terms, "
            "so this stays off until X_BEARER_TOKEN is set.")

    async def poll(self, ctx: SourceCtx) -> Batch:
        tok = ctx.env["X_BEARER_TOKEN"]
        headers = {"Authorization": f"Bearer {tok}", "User-Agent": UA}
        handles = [h.lstrip("@") for h in ctx.cfg.get("x_handles", []) if isinstance(h, str) and h.strip()][:10]
        movers = {m.get("x", "").lstrip("@").lower(): m for m in ctx.cfg.get("movers", []) if m.get("x")}
        items: list[IntelItem] = []

        async def search(q: str, key: str) -> list[dict[str, Any]]:
            params = {"query": q, "max_results": 50, "tweet.fields": "created_at,public_metrics,author_id",
                      "expansions": "author_id", "user.fields": "username"}
            if ctx.scratch.get(key):
                params["since_id"] = ctx.scratch[key]
            d = _get_json(await ctx.client.get("https://api.x.com/2/tweets/search/recent", params=params, headers=headers))
            users = {u["id"]: u["username"] for u in (d.get("includes") or {}).get("users", [])}
            data = d.get("data", [])
            if data:
                ctx.scratch[key] = max(data, key=lambda x: int(x["id"]))["id"]
            for r in data:
                r["_user"] = users.get(r.get("author_id"), "")
            return data

        if handles:
            q = "(" + " OR ".join(f"from:{h}" for h in handles) + ") -is:retweet -is:reply"
            for r in await search(q, "since_handles"):
                m = movers.get(r["_user"].lower())
                pm = r.get("public_metrics") or {}
                items.append(IntelItem(
                    id=f"x:{r['id']}", t=iso_to_ms(r.get("created_at")) or now_ms(), kind="statement", source="x",
                    author=f"@{r['_user']}", text=tx.short(tx.clean_text(r.get("text", "")), 500),
                    url=f"https://x.com/{r['_user']}/status/{r['id']}", mover=(m or {}).get("name") or r["_user"],
                    weight=float((m or {}).get("weight", 0.6)),
                    engagement=_f(pm.get("like_count")) + _f(pm.get("retweet_count")) * 2))
        tags = [s for s in sorted(ctx.universe) if 1 <= len(s) <= 5][:25]
        if tags:
            q = "(" + " OR ".join(f"${s}" for s in tags) + ") -is:retweet lang:en"
            for r in await search(q, "since_tags"):
                pm = r.get("public_metrics") or {}
                items.append(IntelItem(
                    id=f"x:{r['id']}", t=iso_to_ms(r.get("created_at")) or now_ms(), kind="social", source="x",
                    author=f"@{r['_user']}", text=tx.short(tx.clean_text(r.get("text", "")), 400),
                    url=f"https://x.com/{r['_user']}/status/{r['id']}",
                    engagement=_f(pm.get("like_count")) + _f(pm.get("retweet_count")) * 2))
        return Batch(items=items)


# ============================================================================ statements (RSS / Atom)
def parse_feed(xml: str) -> list[dict[str, Any]]:
    """RSS 2.0 / Atom -> list of {id,title,text,url,t,author}. Uses defusedxml (no entity expansion)."""
    root = ET.fromstring(xml.lstrip("﻿"))
    ns = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    out: list[dict[str, Any]] = []

    def txt(el: Any, path: str) -> str:
        n = el.find(path, ns)
        return (n.text or "").strip() if n is not None and n.text else ""

    for it in root.iter("item"):
        when = txt(it, "pubDate")
        try:
            t = int(parsedate_to_datetime(when).timestamp() * 1000) if when else now_ms()
        except (TypeError, ValueError):
            t = now_ms()
        link = txt(it, "link")
        out.append({"id": txt(it, "guid") or link or txt(it, "title"), "title": tx.clean_text(txt(it, "title")),
                    "text": tx.clean_text(txt(it, "description")), "url": link, "t": t,
                    "author": txt(it, "dc:creator")})
    for en in root.iter("{http://www.w3.org/2005/Atom}entry"):
        when = txt(en, "a:updated") or txt(en, "a:published")
        link_el = en.find("a:link", ns)
        out.append({"id": txt(en, "a:id"), "title": tx.clean_text(txt(en, "a:title")),
                    "text": tx.clean_text(txt(en, "a:content") or txt(en, "a:summary")),
                    "url": link_el.get("href", "") if link_el is not None else "",
                    "t": iso_to_ms(when) or now_ms(), "author": txt(en, "a:author/a:name")})
    return out


class TruthSource(Source):
    id, label, kind, interval_s = "truth", "Truth Social archive (Trump)", "statement", 60.0
    note = ("Donald Trump's Truth Social posts via the public trumpstruth.org RSS archive (Truth Social itself blocks "
            "automated clients). Posts are attributed to the 'Trump' mover.")
    URL = "https://www.trumpstruth.org/feed"

    async def poll(self, ctx: SourceCtx) -> Batch:
        r = await ctx.client.get(ctx.cfg.get("truth_url") or self.URL)
        r.raise_for_status()
        mover = next((m for m in ctx.cfg.get("movers", []) if m.get("name", "").lower() == "trump"), {})
        items = []
        for e in parse_feed(r.text)[:40]:
            body = e["text"] or e["title"]
            if len(body) < 15 or "removed" in e["id"]:
                continue
            items.append(IntelItem(id=f"truth:{e['id']}", t=e["t"], kind="statement", source="truth", author="Donald J. Trump",
                                   text=tx.short(body, 600), url=e["url"], mover="Trump", weight=float(mover.get("weight", 1.0))))
        return Batch(items=items)


class RssSource(Source):
    id, label, kind, interval_s = "rss", "Official RSS (Fed, SEC, White House…)", "statement", 180.0
    note = "Configurable list of official feeds. Each feed can be attributed to a mover and given a weight."

    async def poll(self, ctx: SourceCtx) -> Batch:
        feeds = [f for f in ctx.cfg.get("rss", []) if f.get("url") and f.get("enabled", True)][:20]

        async def one(f: dict[str, Any]) -> list[IntelItem]:
            r = await ctx.client.get(f["url"])
            r.raise_for_status()
            out = []
            for e in parse_feed(r.text)[:25]:
                body = f"{e['title']}. {e['text']}" if e["text"] and e["text"] != e["title"] else e["title"]
                out.append(IntelItem(id=f"rss:{f['id']}:{e['id']}", t=e["t"], kind="statement", source=f"rss:{f['id']}",
                                     author=f.get("label", f["id"]), text=tx.short(body, 600), url=e["url"],
                                     mover=f.get("mover") or f.get("label"), weight=float(f.get("weight", 0.7))))
            return out

        res = await asyncio.gather(*(one(f) for f in feeds), return_exceptions=True)
        items = [i for r in res if isinstance(r, list) for i in r]
        errs = [r for r in res if isinstance(r, Exception)]
        if errs and not items:
            raise errs[0]
        return Batch(items=items)


class GoogleNewsSource(Source):
    id, label, kind, interval_s = "gnews", "Mover headlines (Google News RSS)", "statement", 300.0
    note = "One Google News RSS query per configured mover (e.g. Powell, Bessent, Musk) — headlines quoting what they said."

    async def poll(self, ctx: SourceCtx) -> Batch:
        movers = [m for m in ctx.cfg.get("movers", []) if m.get("query") and m.get("enabled", True)][:10]

        async def one(m: dict[str, Any]) -> list[IntelItem]:
            url = f"https://news.google.com/rss/search?q={quote_plus(m['query'] + ' when:1d')}&hl=en-US&gl=US&ceid=US:en"
            r = await ctx.client.get(url)
            r.raise_for_status()
            return [IntelItem(id=f"gnews:{m['name']}:{e['id']}", t=e["t"], kind="statement", source=f"news:{m['name'].lower()}",
                              author=e["title"].rsplit(" - ", 1)[-1] if " - " in e["title"] else "Google News",
                              text=tx.short(e["title"].rsplit(" - ", 1)[0], 400), url=e["url"], mover=m["name"],
                              weight=float(m.get("weight", 0.6)) * 0.7)  # headlines about a mover < the mover's own words
                    for e in parse_feed(r.text)[:15]]

        res = await asyncio.gather(*(one(m) for m in movers), return_exceptions=True)
        items = [i for r in res if isinstance(r, list) for i in r]
        errs = [r for r in res if isinstance(r, Exception)]
        if errs and not items:
            raise errs[0]
        return Batch(items=items)


# ============================================================================ prediction markets
class PolymarketSource(Source):
    id, label, kind, interval_s = "polymarket", "Polymarket", "prediction", 180.0
    note = ("Top-volume active markets plus targeted searches (recession, Fed, tariffs, shutdown, inflation, S&P 500, "
            "ceasefire) via the public gamma API (no key), filtered to market-relevant questions.")
    QUERIES = ("US recession", "Fed rate cut", "tariffs", "government shutdown", "inflation CPI", "S&P 500", "ceasefire")

    @staticmethod
    def _parse(m: dict[str, Any], t: int, slug: str | None) -> PredictionMarket | None:
        q = str(m.get("question", ""))
        if " say " in q.lower() or "press conference" in q.lower():  # word-count 'mention' markets carry no macro signal
            return None
        cls = tx.classify_market(q)
        if cls is None or m.get("closed") or m.get("active") is False:
            return None
        try:
            outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
            prices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
            if [str(o).lower() for o in outcomes[:2]] != ["yes", "no"]:
                return None
            p = float(prices[0])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        vol, liq = _f(m.get("volume24hr")), _f(m.get("liquidityNum") or m.get("liquidity"))
        end = iso_to_ms(m.get("endDate"))
        if (vol < 2_000 and liq < 10_000) or (end and end - t > 400 * 86_400_000):
            return None
        themes, pol, w = cls
        return PredictionMarket(
            id=f"polymarket:{m.get('id')}", source="polymarket", question=q,
            url=f"https://polymarket.com/event/{slug}" if slug else f"https://polymarket.com/market/{m.get('slug', '')}",
            p=p, dp24=_f(m.get("oneDayPriceChange")), volume24h=vol, liquidity=liq, end_ms=end, themes=themes,
            polarity=pol, weight=w, t=t)

    async def poll(self, ctx: SourceCtx) -> Batch:
        t = now_ms()
        found: dict[str, PredictionMarket] = {}
        for off in (0, 100, 200, 300):
            r = await ctx.client.get("https://gamma-api.polymarket.com/markets", params={
                "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100, "offset": off})
            page = _get_json(r)
            for m in page:
                ev = m.get("events")
                slug = ev[0].get("slug") if isinstance(ev, list) and ev and isinstance(ev[0], dict) else None
                if pm := self._parse(m, t, slug):
                    found[pm.id] = pm
            if len(page) < 100:
                break

        async def search(q: str) -> list[PredictionMarket]:
            d = _get_json(await ctx.client.get("https://gamma-api.polymarket.com/public-search", params={
                "q": q, "events_status": "active", "limit_per_type": 6}))
            return [pm for e in d.get("events", []) for m in e.get("markets", []) if (pm := self._parse(m, t, e.get("slug")))]

        for res in await asyncio.gather(*(search(q) for q in self.QUERIES), return_exceptions=True):
            if isinstance(res, list):
                for pm in res:
                    found.setdefault(pm.id, pm)
        out = sorted(found.values(), key=lambda x: -(x.weight * math.log1p(x.volume24h)))
        return Batch(markets=out[:60])


class KalshiSource(Source):
    id, label, kind, interval_s = "kalshi", "Kalshi", "prediction", 300.0
    note = "Open events with nested markets (public API, no key), filtered to economics / financials / policy questions."
    CATS = {"Economics", "Financials", "Politics", "Companies", "World"}

    async def poll(self, ctx: SourceCtx) -> Batch:
        t = now_ms()
        out: list[PredictionMarket] = []
        cursor = ""
        for _ in range(4):
            params: dict[str, Any] = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            d = _get_json(await ctx.client.get("https://api.elections.kalshi.com/trade-api/v2/events", params=params))
            for ev in d.get("events", []):
                tk = str(ev.get("event_ticker", ""))
                if tk.startswith("KXMVE") or ev.get("category") not in self.CATS:
                    continue
                title = str(ev.get("title", ""))
                for m in ev.get("markets", []):
                    sub = str(m.get("yes_sub_title") or "")
                    q = f"{title} — {sub}" if sub and sub.lower() not in title.lower() else title
                    cls = tx.classify_market(q)
                    end = iso_to_ms(m.get("close_time"))
                    if cls is None or (end and end - t > 400 * 86_400_000):
                        continue
                    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                    last = _f(m.get("last_price_dollars"))
                    p = (bid + ask) / 2 if bid > 0 and ask > 0 else last
                    prev = _f(m.get("previous_price_dollars"), p)
                    vol = _f(m.get("volume_24h_fp"))
                    if vol < 100 and _f(m.get("open_interest_fp")) < 1000:
                        continue
                    themes, pol, w = cls
                    out.append(PredictionMarket(
                        id=f"kalshi:{m.get('ticker')}", source="kalshi", question=tx.short(q, 200),
                        url=f"https://kalshi.com/markets/{str(ev.get('series_ticker') or tk).lower()}",
                        p=p, dp24=p - prev if prev else 0.0, volume24h=vol, liquidity=_f(m.get("open_interest_fp")),
                        end_ms=end, themes=themes, polarity=pol, weight=w, t=t))
            cursor = d.get("cursor") or ""
            if not cursor:
                break
        out.sort(key=lambda x: -(x.weight * math.log1p(x.volume24h)))
        return Batch(markets=out[:40])


# ============================================================================ volatility
class VixSource(Source):
    id, label, kind, interval_s = "vix", "VIX family (Yahoo / CBOE)", "volatility", 60.0
    note = "VIX, VIX9D, VIX3M, VVIX and SKEW from Yahoo's chart endpoint; daily VIX history falls back to CBOE's CSV."
    HIST_TTL_MS = 6 * 3_600_000

    @staticmethod
    async def _chart(ctx: SourceCtx, sym: str, rng: str) -> dict[str, Any]:
        r = await ctx.client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(sym)}",
                                 params={"interval": "1d", "range": rng})
        res = _get_json(r)["chart"]["result"][0]
        closes = res["indicators"]["quote"][0].get("close") or []
        return {"price": _f(res["meta"].get("regularMarketPrice")), "closes": [c for c in closes if c is not None]}

    @staticmethod
    async def _cboe(ctx: SourceCtx) -> list[float]:
        r = await ctx.client.get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        return [float(x["CLOSE"]) for x in rows[-300:] if x.get("CLOSE")]

    async def poll(self, ctx: SourceCtx) -> Batch:
        hist_c = ctx.scratch.get("hist")
        spot = None
        try:
            if not hist_c or now_ms() - hist_c[0] > self.HIST_TTL_MS:
                d = await self._chart(ctx, "^VIX", "1y")
                ctx.scratch["hist"] = hist_c = (now_ms(), d["closes"])
                spot = d["price"]
            else:
                spot = (await self._chart(ctx, "^VIX", "5d"))["price"]
        except Exception as e:  # noqa: BLE001
            log.info("yahoo VIX failed (%s); trying CBOE csv", e)
            closes = await self._cboe(ctx)
            ctx.scratch["hist"] = hist_c = (now_ms(), closes)
            spot = closes[-1]
        closes = list(hist_c[1])
        if not spot or spot <= 0:
            raise ValueError("no VIX quote")
        if closes and abs(closes[-1] - spot) < 1e-6:  # today's (possibly partial) bar is already in the history
            closes = closes[:-1]
        aux = await asyncio.gather(*(self._chart(ctx, s, "5d") for s in ("^VIX9D", "^VIX3M", "^VVIX", "^SKEW")),
                                   return_exceptions=True)
        v9, v3, vv, sk = (a["price"] if isinstance(a, dict) and a["price"] > 0 else None for a in aux)
        return Batch(vix={"vix": spot, "prev_close": closes[-1] if closes else None, "vix9d": v9, "vix3m": v3,
                          "vvix": vv, "skew": sk, "hist": closes[-260:], "t": now_ms()})


ALL_SOURCES: list[type[Source]] = [
    TruthSource, RssSource, GoogleNewsSource, XSource, RedditSource, ApeWisdomSource, StocktwitsSource,
    PolymarketSource, KalshiSource, VixSource,
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "sources": {},  # id -> bool overrides (default: on if configured)
    "subreddits": ["wallstreetbets", "stocks", "investing", "options", "StockMarket", "thetagang"],
    "x_handles": ["realDonaldTrump", "federalreserve", "elonmusk", "SecScottBessent"],
    "movers": [
        {"name": "Trump", "weight": 1.0, "x": "realDonaldTrump", "enabled": True,
         "query": "\"President Trump\" (tariffs OR economy OR stocks OR Fed OR China)"},
        {"name": "Fed", "weight": 0.9, "x": "federalreserve", "enabled": True,
         "query": "(\"Jerome Powell\" OR \"Fed Chair\" OR FOMC) (said OR says OR rates)"},
        {"name": "Bessent", "weight": 0.7, "x": "SecScottBessent", "enabled": True,
         "query": "\"Treasury Secretary\" Bessent (said OR says)"},
        {"name": "Musk", "weight": 0.6, "x": "elonmusk", "enabled": True,
         "query": "\"Elon Musk\" (Tesla OR crypto OR xAI OR DOGE) (said OR says OR posted)"},
    ],
    "rss": [
        {"id": "fed", "label": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml",
         "mover": "Fed", "weight": 0.9, "enabled": True},
        {"id": "whitehouse", "label": "White House", "url": "https://www.whitehouse.gov/presidential-actions/feed/",
         "mover": "Trump", "weight": 0.9, "enabled": True},
        {"id": "sec", "label": "SEC", "url": "https://www.sec.gov/news/pressreleases.rss", "mover": "SEC", "weight": 0.5,
         "enabled": True},
    ],
}

