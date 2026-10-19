"""Synthetic intel for SIM mode (offline demos and tests). Independent of the sim price process on purpose:
nothing here may be read as evidence about strategy profitability (see CLAUDE.md)."""

from __future__ import annotations

import math
import random
import zlib

from vantage.core.util import new_id, now_ms
from vantage.intel import text as tx
from vantage.intel.sources import Source, SourceCtx
from vantage.intel.types import Batch, IntelItem, MentionSnap, PredictionMarket

SOCIAL = [
    ("$%s calls printing, this is going to the moon 🚀", 0.9), ("$%s breakout confirmed, loading up", 0.7),
    ("$%s looks overvalued, buying puts", -0.7), ("$%s about to dump, bearish 🐻", -0.9),
    ("Anyone else watching $%s? Earnings soon", 0.0), ("$%s squeeze setting up, short interest high", 0.6),
    ("$%s rug pull incoming, selling everything", -0.8), ("$%s holding support, accumulate on dips", 0.5),
]
STATEMENTS = [
    ("Trump", "Trump", 1.0, "We have reached a trade deal with China — tariffs will be lowered on both sides."),
    ("Trump", "Trump", 1.0, "Starting August 1st, a 25% tariff on all imported semiconductors and electronics."),
    ("Powell", "Fed", 0.9, "The Committee sees room for rate cuts if inflation continues to ease; policy is not on a preset course."),
    ("Powell", "Fed", 0.9, "Inflation remains elevated and we are prepared to raise rates further if needed."),
    ("Musk", "Musk", 0.6, "Tesla will reveal the next-generation platform next quarter. Bitcoin is the future of money."),
    ("Bessent", "Bessent", 0.7, "A ceasefire framework is close and sanctions relief is on the table."),
    ("Trump", "Trump", 1.0, "Great rally in Pennsylvania tonight, thank you to all the wonderful people!"),
]
MARKETS = [
    ("Will the US enter a recession in 2026?", -1, 1.0, 0.22), ("Will the Fed cut rates at the next FOMC meeting?", 1, 0.8, 0.35),
    ("Will new tariffs on China take effect before January?", -1, 0.9, 0.55), ("Will the government shut down on October 1?", -1, 0.7, 0.18),
    ("Will the S&P 500 close above 6,500 this month?", 1, 0.7, 0.48), ("Will CPI inflation exceed 3% in the next print?", -1, 0.6, 0.4),
    ("Will a ceasefire be announced before November?", 1, 0.8, 0.3), ("Will Bitcoin reach $150,000 this year?", 1, 0.3, 0.12),
]


class SimIntelSource(Source):
    id, label, kind, interval_s = "sim", "Synthetic intel (SIM mode)", "social", 10.0
    note = "Randomly generated posts, statements, prediction markets and VIX so the whole pipeline works offline."

    def __init__(self) -> None:
        self.rng = random.Random(1789)

    async def poll(self, ctx: SourceCtx) -> Batch:
        rng, t = self.rng, now_ms()
        sc = ctx.scratch
        syms = sorted(ctx.universe) or ["SPY"]
        items: list[IntelItem] = []
        for _ in range(rng.randint(1, 4)):
            s = rng.choice(syms)
            tpl, lean = rng.choice(SOCIAL)
            lab = "bullish" if lean > 0.4 and rng.random() < 0.6 else "bearish" if lean < -0.4 and rng.random() < 0.6 else None
            items.append(IntelItem(id=new_id("sim-soc-"), t=t, kind="social", source=rng.choice(["reddit", "stocktwits"]),
                                   author=f"user{rng.randint(1, 999)}", text=tpl % s, symbols=[s],
                                   engagement=rng.lognormvariate(3, 1.4), label=lab))
        first = not sc.get("seeded")
        sc["seeded"] = True
        for who, mover, w, text in (rng.sample(STATEMENTS, 3) if first else ([rng.choice(STATEMENTS)] if rng.random() < 0.12 else [])):
            items.append(IntelItem(id=new_id("sim-stmt-"), t=t, kind="statement", source="truth" if who == "Trump" else "rss:sim",
                                   author=who, text=text, mover=mover, weight=w))
        mentions = []
        for s in syms[:40]:
            base = sc.setdefault("base", {}).setdefault(s, rng.uniform(5, 80))
            cur = base * math.exp(rng.gauss(0, 0.35))
            mentions.append(MentionSnap(s, t, "apewisdom", round(cur), round(base), cur * 4))
        pm = sc.setdefault("pm", {q: p for q, _, _, p in MARKETS})
        markets = []
        for q, pol, w, _ in MARKETS:
            prev = pm[q]
            pm[q] = min(0.97, max(0.03, prev + rng.gauss(0, 0.012)))
            themes, _, _ = tx.classify_market(q) or (["markets"], 0, w)
            markets.append(PredictionMarket(f"sim:{zlib.crc32(q.encode())}", "polymarket", q, "", pm[q], pm[q] - sc.setdefault("p0", {}).setdefault(q, prev),
                                            rng.uniform(5e3, 5e5), 1e5, t + 20 * 86_400_000, themes, pol, w, t))
        v = sc.get("vix", 17.0)
        v = max(10.0, v + 0.15 * (17.5 - v) + rng.gauss(0, 0.25) + (rng.random() < 0.01) * rng.uniform(3, 9))
        sc["vix"] = v
        hist = sc.setdefault("hist", [17 + 3 * math.sin(i / 9) + rng.gauss(0, 0.8) for i in range(250)])
        vix = {"vix": round(v, 2), "prev_close": hist[-1], "vix9d": round(v * rng.uniform(0.9, 1.05), 2),
               "vix3m": round(v * rng.uniform(1.02, 1.12), 2), "vvix": round(rng.uniform(85, 115), 1),
               "skew": round(rng.uniform(130, 150), 1), "hist": list(hist), "t": t}
        return Batch(items=items, mentions=mentions, markets=markets, vix=vix)

