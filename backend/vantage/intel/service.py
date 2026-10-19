"""IntelService: polls the intel sources, scores and indexes what they return, and answers the signal layer.

Outputs (all consumed read-only by signals/strategies/API):
* per-symbol social stats  -> ``social_signal`` (polarity x attention, with a crowding guard);
* statements by market movers -> ``statement_signal`` (decayed, symbol/sector/market-wide applicability);
* VIX regime + prediction-market tilt / event risk -> ``macro`` (market-wide state, risk-on score, event risk).

Everything degrades gracefully: a failing source only marks its own status; no source = no signal (models return None).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np

from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.util import dumps, loads, now_ms
from vantage.intel import text as tx
from vantage.intel.sim import SimIntelSource
from vantage.intel.sources import ALL_SOURCES, DEFAULT_CONFIG, UA, Source, SourceCtx
from vantage.intel.types import Batch, IntelItem, MentionSnap, PredictionMarket, SourceState
from vantage.llm import prompts
from vantage.quant import sentiment as qs

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
KEEP_MS = 72 * HOUR_MS
PM_MAX = 400

VIX_STRESS = {"panic": 1.0, "stressed": 0.7, "elevated": 0.4, "normal": 0.15, "calm": 0.05}


def validate_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Whitelist + sanity-check a config patch from the API. Raises ValueError on bad input."""
    out: dict[str, Any] = {}
    if "enabled" in patch:
        out["enabled"] = bool(patch["enabled"])
    if "sources" in patch:
        if not isinstance(patch["sources"], dict):
            raise ValueError("sources must be an object of id -> bool")
        out["sources"] = {str(k): bool(v) for k, v in patch["sources"].items()}
    for key, cap in (("subreddits", 12), ("x_handles", 10)):
        if key in patch:
            vals = patch[key]
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                raise ValueError(f"{key} must be a list of strings")
            clean = [v.strip().lstrip("@").removeprefix("r/") for v in vals if v.strip()]
            if any(not v.replace("_", "").isalnum() or len(v) > 40 for v in clean):
                raise ValueError(f"{key}: letters, digits and underscores only")
            out[key] = list(dict.fromkeys(clean))[:cap]
    if "movers" in patch:
        out["movers"] = _clean_list(patch["movers"], ("name", "weight", "x", "query", "enabled"), 12, need="name")
    if "rss" in patch:
        rss = _clean_list(patch["rss"], ("id", "label", "url", "mover", "weight", "enabled"), 20, need="url")
        for f in rss:
            _check_url(f["url"])
            f.setdefault("id", f.get("label", "feed").lower().replace(" ", "-")[:24])
        out["rss"] = rss
    return out


def _clean_list(v: Any, keys: tuple[str, ...], cap: int, need: str) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        raise ValueError("expected a list")
    out = []
    for row in v[:cap]:
        if not isinstance(row, dict) or not str(row.get(need, "")).strip():
            raise ValueError(f"every entry needs a '{need}'")
        r = {k: row[k] for k in keys if k in row}
        if "weight" in r:
            r["weight"] = max(0.0, min(1.0, float(r["weight"])))
        if "enabled" in r:
            r["enabled"] = bool(r["enabled"])
        out.append(r)
    return out


def _check_url(url: str) -> None:
    """Feeds are fetched by the backend, so refuse anything that could reach the local machine or LAN."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        raise ValueError("feed URLs must be http(s)")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("feed URLs must be public hosts")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a DNS name, not an IP literal
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        raise ValueError("feed URLs must be public hosts")


class IntelService:
    def __init__(self, bus: Bus, store: Store, mode: str, env: dict[str, str], symbols_fn: Callable[[], list[str]],
                 llm: Any | None = None, enabled: bool = True):
        self.bus = bus
        self.store = store
        self.mode = mode
        self.env = env
        self.symbols_fn = symbols_fn
        self.llm = llm
        saved = store.kv_get("global", "intel", {}) or {}
        self.cfg: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG) | {k: v for k, v in saved.items() if k in DEFAULT_CONFIG}
        self.cfg["enabled"] = bool(saved.get("enabled", enabled))
        srcs: list[Source] = [SimIntelSource()] if mode == "sim" else [cls() for cls in ALL_SOURCES]
        self.sources: dict[str, Source] = {s.id: s for s in srcs}
        self.states: dict[str, SourceState] = {}
        for s in srcs:
            ok = s.configured(env)
            self.states[s.id] = SourceState(s.id, s.label, s.kind, enabled=bool(self.cfg["sources"].get(s.id, ok)) and ok,
                                            configured=ok, needs=s.needs, note=s.note, interval_s=s.interval_s)
        self.items: deque[IntelItem] = deque(maxlen=3000)
        self._seen: set[str] = set()
        self.by_symbol: dict[str, deque[IntelItem]] = defaultdict(lambda: deque(maxlen=400))
        self.mentions: dict[str, dict[str, MentionSnap]] = defaultdict(dict)
        self.markets: dict[str, PredictionMarket] = {}
        self._pm_hist: dict[str, deque[tuple[int, float]]] = defaultdict(lambda: deque(maxlen=600))
        self.vix_raw: dict[str, Any] | None = None
        self.vix: dict[str, Any] | None = None
        self._macro: dict[str, Any] | None = None
        self._macro_t = 0.0
        self._recent_stmts: tuple[float, list[IntelItem]] = (0.0, [])
        self._scratch: dict[str, dict[str, Any]] = defaultdict(dict)
        self._fails: dict[str, int] = defaultdict(int)
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._q: asyncio.Queue[IntelItem] = asyncio.Queue(maxsize=100)
        self._publish_t = 0.0

    # ------------------------------------------------------------------ lifecycle
    @property
    def enabled(self) -> bool:
        return bool(self.cfg["enabled"])

    @property
    def universe(self) -> frozenset[str]:
        return frozenset(self.symbols_fn())

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=12.0, headers={"User-Agent": UA, "Accept": "*/*"}, follow_redirects=True)
        await self._load_persisted()
        for i, src in enumerate(self.sources.values()):
            self._tasks.append(asyncio.create_task(self._run_source(src, 1.0 + 0.7 * i), name=f"intel-{src.id}"))
        self._tasks.append(asyncio.create_task(self._enrich_loop(), name="intel-llm"))
        self._tasks.append(asyncio.create_task(self._prune_loop(), name="intel-prune"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
        if self._client:
            await self._client.aclose()

    async def _load_persisted(self) -> None:
        since = now_ms() - KEEP_MS
        rows = await self.store.query("SELECT data FROM intel_items WHERE mode=? AND t>=? ORDER BY t DESC LIMIT 2000",
                                      (self.mode, since))
        for r in reversed(rows):
            try:
                it = IntelItem(**loads(r["data"]))
            except (TypeError, ValueError):
                continue
            if it.id not in self._seen:
                self._seen.add(it.id)
                self.items.append(it)
                self.by_symbol_add(it)
        pm = await self.store.query("SELECT mid, t, p FROM intel_pm WHERE mode=? AND t>=? ORDER BY t", (self.mode, now_ms() - 3 * 86_400_000))
        for r in pm:
            self._pm_hist[r["mid"]].append((r["t"], r["p"]))

    # ------------------------------------------------------------------ polling
    async def _run_source(self, src: Source, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            st = self.states[src.id]
            if self.enabled and st.enabled and st.configured:
                await self._poll(src)
            fails = self._fails[src.id]
            await asyncio.sleep(src.interval_s * min(8, 2 ** min(fails, 3)))  # back off while a source keeps failing

    async def _prune_loop(self) -> None:
        while True:
            await self.store.awrite("DELETE FROM intel_items WHERE mode=? AND t<?", (self.mode, now_ms() - KEEP_MS))
            await self.store.awrite("DELETE FROM intel_pm WHERE mode=? AND t<?", (self.mode, now_ms() - 4 * 86_400_000))
            await asyncio.sleep(3600)

    async def _poll(self, src: Source) -> None:
        st = self.states[src.id]
        st.last_poll = now_ms()
        assert self._client is not None
        ctx = SourceCtx(self._client, self.universe, self.cfg, self.env, self._scratch[src.id])
        try:
            batch = await asyncio.wait_for(src.poll(ctx), 40)
            self._ingest(src, batch)
            st.last_ok, st.last_error = now_ms(), None
            self._fails[src.id] = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._fails[src.id] += 1
            st.last_error = _short_err(e)
            log.info("intel source %s failed: %s", src.id, st.last_error)

    async def refresh(self, source_id: str | None = None) -> dict[str, Any]:
        """Poll sources now (API 'refresh'); returns the updated source statuses."""
        todo = [self.sources[source_id]] if source_id else list(self.sources.values())
        for src in todo:
            if src.id in self.sources and self.states[src.id].configured:
                await self._poll(src)
        return {"sources": self.sources_wire()}

    def _ingest(self, src: Source, batch: Batch) -> None:
        st = self.states[src.id]
        n = 0
        for it in sorted(batch.items, key=lambda x: x.t):
            if self._accept(it):
                n += 1
        for m in batch.mentions:
            self.mentions[m.symbol][m.source] = m
        if batch.markets:
            self._set_markets(batch.markets)
        if batch.vix:
            self._set_vix(batch.vix)
        st.n_last = n + len(batch.mentions) + len(batch.markets) + (1 if batch.vix else 0)
        st.n_total += n
        self._publish_macro(force=bool(batch.markets or batch.vix))

    # ------------------------------------------------------------------ items
    def _accept(self, it: IntelItem, publish: bool = True) -> bool:
        if it.id in self._seen or now_ms() - it.t > KEEP_MS or not it.text.strip():
            return False
        self._seen.add(it.id)
        self._score(it)
        if it.kind == "social" and not it.symbols:
            return False
        if it.kind == "statement" and not (it.themes or it.symbols or (it.strength or 0) > 0 or it.weight >= 0.8):
            return False
        self.items.append(it)
        self.by_symbol_add(it)
        self.store.write("INSERT INTO intel_items(id,mode,t,kind,data) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET data=excluded.data", (it.id, self.mode, it.t, it.kind, dumps(it).decode()))
        if publish:
            self.bus.publish("intel", it)
        if it.kind == "statement":
            self._recent_stmts = (0.0, [])
            if self.llm and self.llm.enabled and it.weight >= 0.5:
                with contextlib.suppress(asyncio.QueueFull):
                    self._q.put_nowait(it)
        return True

    def _score(self, it: IntelItem) -> None:
        uni = self.universe
        if not it.symbols:
            it.symbols = tx.extract_symbols(it.text, uni, cashtags_any=it.source == "stocktwits")
        it.themes = it.themes or tx.themes_of(it.text)
        if it.kind == "social":
            if it.label:
                it.sentiment, it.strength = (1.0 if it.label == "bullish" else -1.0) * 0.85, 0.9
            else:
                it.sentiment, it.strength = (round(v, 3) for v in tx.social_sentiment(it.text))
        else:
            it.sentiment, it.strength = (round(v, 3) for v in tx.statement_impact(it.text))
            if it.source.startswith("news:"):  # headlines *about* a mover are a weaker proxy than the mover's own words
                it.strength = round((it.strength or 0.0) * (0.3 if "opinion" in it.text.lower()[:40] else 0.6), 3)

    def by_symbol_add(self, it: IntelItem) -> None:
        for s in it.symbols:
            self.by_symbol[s].append(it)

    async def _enrich_loop(self) -> None:
        """LLM re-scoring of statements (the lexicon is the offline fallback). Only produces signal inputs; it can never
        trade — strategies and the risk manager decide that."""
        while True:
            it = await self._q.get()
            try:
                await self._enrich(it)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one malformed reply must not end enrichment for the session
                log.exception("intel enrichment failed for %s", it.id)

    async def _enrich(self, it: IntelItem) -> None:
        if now_ms() - it.t > 6 * HOUR_MS or not (self.llm and self.llm.enabled):
            return
        res = await self.llm.complete_json("intel", prompts.INTEL_SYSTEM,
                                           prompts.intel_user(it.author, it.mover, it.text, sorted(self.universe)),
                                           prompts.INTEL_SCHEMA, cache_ttl_s=3600, max_tokens=500)
        if not res.ok or not res.data:
            return
        d = res.data
        if not d.get("market_relevant", True):
            it.sentiment, it.strength = 0.0, 0.0
        else:
            mag, nov = float(d["magnitude"]), float(d.get("novelty", 1.0))
            it.sentiment = round(max(-1.0, min(1.0, float(d["market_direction"]) * mag)), 3)
            it.strength = round(min(1.0, 0.4 + 0.6 * nov), 3)
        per = {a["symbol"]: float(a["direction"]) * float(a.get("relevance", 1.0)) * float(d["magnitude"])
               for a in d.get("affected", []) if a.get("symbol") in self.universe}
        it.llm = {"direction": d["market_direction"], "magnitude": d["magnitude"], "horizon": d.get("horizon", "days"),
                  "rationale": str(d.get("rationale", ""))[:400], "per_symbol": per, "latency_ms": res.latency_ms}
        for s in per:
            if s not in it.symbols:
                it.symbols.append(s)
                self.by_symbol[s].append(it)
        self._recent_stmts = (0.0, [])
        self.store.write("INSERT INTO intel_items(id,mode,t,kind,data) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET data=excluded.data", (it.id, self.mode, it.t, it.kind, dumps(it).decode()))
        self.bus.publish("intel", it)
        self._publish_macro(force=True)

    # ------------------------------------------------------------------ prediction markets & VIX
    def _set_markets(self, markets: list[PredictionMarket]) -> None:
        now = now_ms()
        self.markets = {m.id: m for m in markets[:PM_MAX]}
        for m in self.markets.values():
            h = self._pm_hist[m.id]
            if not h or now - h[-1][0] >= 30 * 60_000:
                h.append((now, m.p))
                self.store.write("INSERT OR REPLACE INTO intel_pm(mode,mid,t,p) VALUES(?,?,?,?)", (self.mode, m.id, now, m.p))
            # prefer our own 24h-ago observation when we have one; otherwise trust the venue's field
            old = next((p for t, p in h if now - t >= 22 * HOUR_MS), None)
            if old is not None:
                m.dp24 = m.p - old

    def _set_vix(self, raw: dict[str, Any]) -> None:
        self.vix_raw = raw
        self.vix = qs.vix_regime(float(raw["vix"]), raw.get("prev_close"), np.asarray(raw.get("hist") or [], dtype=float),
                                 raw.get("vix9d"), raw.get("vix3m"), raw.get("vvix"), raw.get("skew"))
        self.vix["t"] = raw.get("t", now_ms())

    # ------------------------------------------------------------------ social stats / signal
    def social_stats(self, symbol: str, now: int | None = None) -> dict[str, Any] | None:
        now = now or now_ms()
        items = [i for i in self.by_symbol.get(symbol, ()) if i.kind == "social" and 0 <= now - i.t <= 24 * HOUR_MS]
        snaps = self.mentions.get(symbol, {})
        if not items and not snaps:
            return None
        bull = bear = n_eff = 0.0
        labelled = 0
        sources: dict[str, int] = defaultdict(int)
        for i in items:
            w = qs.decay_weight(now - i.t, 6.0) * qs.engagement_weight(i.engagement) * (i.strength or 0.0)
            s = i.sentiment or 0.0
            bull += max(s, 0.0) * w
            bear += max(-s, 0.0) * w
            n_eff += qs.decay_weight(now - i.t, 6.0)
            labelled += i.label is not None
            sources[i.source] += 1
        pol = qs.shrunk_polarity(bull, bear, prior=3.0)
        c6 = sum(1 for i in items if now - i.t <= 6 * HOUR_MS)
        older = sum(1 for i in self.by_symbol.get(symbol, ()) if i.kind == "social" and 6 * HOUR_MS < now - i.t <= 72 * HOUR_MS)
        base6 = older / 11.0  # (72 - 6) h / 6 h windows
        parts = []
        all_t = [i.t for i in self.by_symbol.get(symbol, ()) if i.kind == "social"]
        # item-based attention needs a baseline: without >= 18 h of history every symbol would look "suddenly loud"
        if len(items) >= 5 and all_t and now - min(all_t) >= 18 * HOUR_MS:
            parts.append(qs.attention_ratio(c6, base6, k=3.0))
        mention = base_mention = None
        for s in snaps.values():
            mention, base_mention = s.mentions, s.mentions_prev
            parts.append(qs.attention_ratio(s.mentions, s.mentions_prev, k=5.0))
        attn = float(np.mean(parts)) if parts else 0.0
        z, conf, crowded = qs.social_score(pol, attn, n_eff + (3.0 if snaps else 0.0))
        return {"symbol": symbol, "n": len(items), "n_eff": round(n_eff, 2), "bull": round(bull, 2), "bear": round(bear, 2),
                "polarity": round(pol, 3), "attention": round(attn, 3), "mentions_24h": mention, "mentions_prev": base_mention,
                "rank": next((s.rank for s in snaps.values() if s.rank), None), "labelled": labelled, "crowded": crowded,
                "sources": dict(sources), "score_z": round(z, 3), "confidence": round(conf, 3)}

    def social_signal(self, symbol: str, now: int | None = None) -> tuple[float, float, dict[str, Any]] | None:
        st = self.social_stats(symbol, now)
        if not st or (st["n"] == 0 and st["mentions_24h"] is None):
            return None
        if st["n"] < 3 and st["mentions_24h"] is None:
            return None
        return st["score_z"], st["confidence"], {k: st[k] for k in ("n", "polarity", "attention", "mentions_24h", "crowded", "sources")}

    # ------------------------------------------------------------------ statements
    def _statements(self, now: int) -> list[IntelItem]:
        t, cache = self._recent_stmts
        if cache and time.monotonic() - t < 10:
            return cache
        out = [i for i in self.items if i.kind == "statement" and 0 <= now - i.t <= 24 * HOUR_MS]
        self._recent_stmts = (time.monotonic(), out)
        return out

    def statement_signal(self, symbol: str, beta: float = 1.0, now: int | None = None) -> tuple[float, float, dict[str, Any]] | None:
        """Decayed sum of mover statements applicable to ``symbol``: explicit mention (1.0), exposed theme (0.7), market-wide
        (0.5 x beta, direction from the statement's market tilt)."""
        now = now or now_ms()
        uni = self.universe
        beta = max(-2.0, min(2.0, beta))
        vals: list[tuple[float, float, float]] = []
        top: list[tuple[float, IntelItem]] = []
        for it in self._statements(now):
            v = it.sentiment or 0.0
            s = it.strength or 0.0
            if abs(v) < 0.02 or s <= 0:
                continue
            per = (it.llm or {}).get("per_symbol") or {}
            if symbol in per:
                app, val = 1.0, float(per[symbol])
            elif symbol in it.symbols:
                app, val = 1.0, v
            elif it.themes and symbol in tx.theme_symbols(it.themes, uni):
                app, val = 0.7, v
            elif it.mover and it.weight >= 0.5:
                app, val = 0.5 * beta, v
            else:
                continue
            w = it.weight * s * app
            vals.append((val, now - it.t, w))
            top.append((abs(val * w), it))
        if not vals:
            return None
        tilt, n_eff = qs.decayed_tilt(vals, half_life_h=3.0, scale=1.5)
        top.sort(key=lambda x: -x[0])
        meta = {"n": len(vals), "tilt": round(tilt, 3),
                "top": [{"mover": i.mover, "text": tx.short(i.text, 120), "sentiment": i.sentiment} for _, i in top[:2]]}
        return 2.5 * tilt, 1.0 - math.exp(-n_eff / 1.5), meta

    # ------------------------------------------------------------------ macro
    def macro(self, now: int | None = None) -> dict[str, Any]:
        """Market-wide state. Cached for ~2 s because every symbol's model reads it each refresh."""
        if self._macro and time.monotonic() - self._macro_t < 2.0:
            return self._macro
        now = now or now_ms()
        mk = [m for m in self.markets.values() if m.liquidity >= 1_000 or m.volume24h >= 1_000]
        ev = qs.event_risk([(m.p, m.dp24, m.weight, max(0.0, ((m.end_ms or now + 30 * 86_400_000) - now) / 86_400_000)) for m in mk])
        tilt = qs.macro_tilt([(m.polarity, m.dp24, m.weight * min(1.0, math.log1p(m.volume24h) / 12)) for m in mk if m.polarity])
        movers = [(i.sentiment or 0.0, now - i.t, i.weight * (i.strength or 0.0)) for i in self._statements(now) if i.mover]
        s_tilt, s_n = qs.decayed_tilt(movers, half_life_h=3.0, scale=1.5) if movers else (0.0, 0.0)
        v = self.vix
        parts = [(0.5, v["risk_on"])] if v else []
        parts += [(0.3, tilt)] if mk else []
        parts += [(0.2, s_tilt)] if movers else []
        risk_on = sum(w * x for w, x in parts) / sum(w for w, _ in parts) if parts else 0.0
        stress = VIX_STRESS.get(str(v["label"]), 0.15) if v else 0.0
        event = max(ev, 0.7 * stress) if (mk or v) else 0.0
        top_m = sorted(mk, key=lambda m: -(m.weight * abs(m.dp24) * 4 + qs.binary_uncertainty(m.p) * m.weight * 0.3))[:3]
        self._macro = {
            "t": now, "vix": v, "risk_on": round(risk_on, 3), "event_risk": round(event, 3),
            "prediction": {"tilt": round(tilt, 3), "event_risk": round(ev, 3), "n": len(mk),
                           "top": [{"question": m.question, "p": m.p, "dp24": m.dp24} for m in top_m]},
            "statements": {"tilt": round(s_tilt, 3), "n_eff": round(s_n, 2)},
        }
        self._macro_t = time.monotonic()
        return self._macro

    def _publish_macro(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._publish_t < 2.0:
            return
        self._publish_t = time.monotonic()
        self._macro_t = 0.0
        self.bus.publish("macro", self.macro())

    # ------------------------------------------------------------------ queries / wire
    def recent(self, kind: str | None = None, symbol: str | None = None, mover: str | None = None, limit: int = 100) -> list[IntelItem]:
        out: list[IntelItem] = []
        for it in reversed(self.items):
            if kind and it.kind != kind:
                continue
            if symbol and symbol not in it.symbols:
                continue
            if mover and (it.mover or "").lower() != mover.lower():
                continue
            out.append(it)
            if len(out) >= limit:
                break
        return out

    def leaderboard(self, limit: int = 30) -> list[dict[str, Any]]:
        syms = set(self.by_symbol) | set(self.mentions)
        rows = [r for s in syms if (r := self.social_stats(s))]
        uni = self.universe
        for r in rows:
            r["in_universe"] = r["symbol"] in uni
        rows.sort(key=lambda r: (-(r["mentions_24h"] or 0) - 4 * r["n"]))
        return rows[:limit]

    def markets_wire(self, limit: int = 60) -> list[dict[str, Any]]:
        ms = sorted(self.markets.values(), key=lambda m: -(m.weight * math.log1p(m.volume24h)))
        return [m.to_wire() for m in ms[:limit]]

    def sources_wire(self) -> list[dict[str, Any]]:
        return [s.to_wire() for s in self.states.values()]

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "macro": self.macro(), "markets": self.markets_wire(30),
            "statements": [i.to_wire() for i in self.recent("statement", limit=20)],
            "social": self.leaderboard(25), "sources": self.sources_wire(),
        }

    def status_bits(self) -> dict[str, Any]:
        v = self.vix
        ok = sum(1 for s in self.states.values() if s.enabled and s.last_ok and not s.last_error)
        on = sum(1 for s in self.states.values() if s.enabled)
        return {"enabled": self.enabled, "vix": round(v["vix"], 2) if v else None, "vix_chg": round(v["chg_1d"], 4) if v else None,
                "regime": v["label"] if v else None, "sources_ok": ok, "sources_on": on}

    def dossier(self, symbol: str) -> dict[str, Any]:
        """Compact evidence for the LLM gate / analyst."""
        m = self.macro()
        st = self.social_stats(symbol)
        v = m.get("vix") or {}
        return {
            "macro": {"risk_on": m["risk_on"], "event_risk": m["event_risk"], "vix": v.get("vix"), "vix_regime": v.get("label"),
                      "vix_term_structure": v.get("term"), "prediction_markets": m["prediction"]["top"]},
            "social": {k: st[k] for k in ("polarity", "attention", "n", "mentions_24h", "crowded")} if st else None,
            "statements": [{"age_h": round((now_ms() - i.t) / HOUR_MS, 1), "mover": i.mover, "text": tx.short(i.text, 160),
                            "sentiment": i.sentiment} for i in self._statements(now_ms())[-4:]],
        }

    # ------------------------------------------------------------------ config
    def update_config(self, patch: dict[str, Any]) -> None:
        clean = validate_config(patch)
        for k, v in clean.items():
            if k == "sources":
                self.cfg["sources"] = {**self.cfg["sources"], **v}
            else:
                self.cfg[k] = v
        for sid, st in self.states.items():
            st.enabled = bool(self.cfg["sources"].get(sid, st.configured)) and st.configured
        self.store.kv_set("global", "intel", {k: self.cfg[k] for k in DEFAULT_CONFIG})

    def config_wire(self) -> dict[str, Any]:
        return {k: self.cfg[k] for k in DEFAULT_CONFIG}


def _short_err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    if isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return "timed out"
    msg = str(e) or type(e).__name__
    return msg[:160]
