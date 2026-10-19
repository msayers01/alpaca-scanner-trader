"""Intel layer: pure maths, text analytics, collectors (mocked HTTP), service, strategy gating and API — all offline."""

from __future__ import annotations

import math
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from vantage.config import Settings
from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import Signal
from vantage.core.util import now_ms
from vantage.intel import text as tx
from vantage.intel.service import IntelService, _check_url, validate_config
from vantage.intel.sources import (
    DEFAULT_CONFIG,
    KalshiSource,
    PolymarketSource,
    SourceCtx,
    VixSource,
    parse_feed,
)
from vantage.intel.types import IntelItem
from vantage.main import create_app
from vantage.quant import sentiment as qs
from vantage.strategies.base import StrategyContext
from vantage.strategies.intel import IntelCatalyst, composite

UNIVERSE = frozenset({"SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "XLE"})
HOUR = 3_600_000


# ---------------------------------------------------------------------------- quant/sentiment.py
def test_shrunk_polarity_closed_form():
    assert qs.shrunk_polarity(0, 0) == 0.0
    assert qs.shrunk_polarity(6, 2, prior=4) == pytest.approx(4 / 12)
    # more evidence -> closer to the raw (bull-bear)/(bull+bear)
    assert qs.shrunk_polarity(600, 200) == pytest.approx(0.5, abs=0.01)
    assert -1 < qs.shrunk_polarity(0, 1e9) < 0


def test_attention_ratio_and_decay():
    assert qs.attention_ratio(10, 10) == 0.0
    assert qs.attention_ratio(30, 10, k=0) == pytest.approx(math.log(3))
    assert qs.attention_ratio(0, 0) == 0.0
    assert qs.decay_weight(6 * HOUR, 6.0) == pytest.approx(0.5)
    assert qs.decay_weight(-5, 6.0) == 1.0
    assert qs.engagement_weight(0) == 1.0 and qs.engagement_weight(1e6) < 6.0


def test_social_score_gates_and_crowding():
    quiet, _, _ = qs.social_score(0.8, -1.0, 20)
    normal, conf, crowd = qs.social_score(0.8, 0.5, 20)
    assert abs(quiet) < abs(normal) and not crowd and conf > 0.9
    loud, _, crowded = qs.social_score(0.8, 3.0, 20)
    assert crowded and abs(loud) < abs(normal)  # crowded one-sided retail is shrunk, not amplified
    assert qs.social_score(0.8, 3.0, 0)[1] == 0.0


def _hist(n=250, level=16.0, seed=1):
    rng = np.random.default_rng(seed)
    return level * np.exp(np.cumsum(rng.normal(0, 0.04, n)) * 0.2)


def test_vix_regime_labels_and_direction():
    h = _hist()
    calm = qs.vix_regime(13.0, 13.1, h, vix9d=12.5, vix3m=15.0)
    assert calm["label"] in ("calm", "normal") and calm["risk_on"] > 0
    panic = qs.vix_regime(38.0, 24.0, h, vix9d=42.0, vix3m=33.0)
    assert panic["label"] == "panic" and panic["risk_on"] < -0.3 and panic["inverted"]
    spike = qs.vix_regime(26.0, 18.0, h, vix3m=24.0)  # +36 % in a day to >= 25: a z>=3 spike is classed as panic
    assert spike["label"] == "panic" and spike["z_chg"] > 3
    stressed = qs.vix_regime(26.0, 25.0, h, vix3m=27.0)
    assert stressed["label"] == "stressed"
    for r in (calm, panic, spike, stressed):
        assert -1 <= r["risk_on"] <= 1
    # missing history degrades to neutral percentile instead of raising
    assert qs.vix_regime(20.0, None, np.array([]))["pct"] == 0.5


def test_percentile_rank():
    assert qs.percentile_rank(5, np.arange(1, 11)) == 0.5
    assert qs.percentile_rank(100, np.arange(10)) == 1.0


def test_event_risk_and_macro_tilt():
    assert qs.event_risk([]) == 0.0
    coin = qs.event_risk([(0.5, 0.0, 1.0, 1.0)])
    sure = qs.event_risk([(0.99, 0.0, 1.0, 1.0)])
    far = qs.event_risk([(0.5, 0.0, 1.0, 300.0)])
    assert coin > sure and coin > far
    assert qs.event_risk([(0.5, 0.2, 1.0, 1.0)]) > coin  # repricing adds risk
    assert 0 <= qs.event_risk([(0.5, 0.3, 1.0, 0.0)] * 50) < 1
    assert qs.macro_tilt([(-1, 0.10, 1.0)]) == pytest.approx(-math.tanh(0.10 / 0.08))
    assert qs.macro_tilt([(-1, 0.05, 1.0), (1, 0.05, 1.0)]) == pytest.approx(0.0)
    assert qs.macro_tilt([]) == 0.0


# ---------------------------------------------------------------------------- text
def test_extract_symbols():
    assert tx.extract_symbols("$nvda and TSLA to the moon, also $GME", UNIVERSE) == ["NVDA", "TSLA"]
    assert tx.extract_symbols("buying $GME", UNIVERSE, cashtags_any=True) == ["GME"]
    assert tx.extract_symbols("Nvidia and Tesla beat", UNIVERSE) == ["TSLA", "NVDA"] or set(tx.extract_symbols("Nvidia and Tesla beat", UNIVERSE)) == {"NVDA", "TSLA"}
    assert tx.extract_symbols("a long day, ALL IN", {"A", "ALL"}) == []  # ambiguous English words need a cashtag


def test_social_sentiment_directions_and_negation():
    bull, sb = tx.social_sentiment("$NVDA calls printing 🚀 bullish breakout")
    bear, _ = tx.social_sentiment("$NVDA puts, this will dump, bearish 🐻")
    neg, _ = tx.social_sentiment("not bullish at all")
    assert bull > 0.5 and bear < -0.5 and neg < 0 and sb > 0.5
    assert tx.social_sentiment("what time is it") == (0.0, 0.0)


def test_statement_impact_masks_overlapping_phrases():
    assert tx.statement_impact("We will pause tariffs for 90 days")[0] > 0  # 'pause tariff' is not also bearish 'tariff'
    assert tx.statement_impact("New 25% tariff on all chips, trade war escalates")[0] < -0.3
    assert tx.statement_impact("No tariffs on allies")[0] > 0  # negator flips
    assert tx.statement_impact("Great rally tonight in Ohio!") == (0.0, 0.0)
    s, strength = tx.statement_impact("A trade deal has been reached; rate cut expected")
    assert s > 0.3 and 0 < strength <= 1


def test_themes_and_symbols():
    th = tx.themes_of("Tariffs on semiconductor chips from China")
    assert {"tariffs", "china", "tech"} <= set(th)
    assert "NVDA" in tx.theme_symbols(th, UNIVERSE) and "XLE" not in tx.theme_symbols(th, UNIVERSE)


def test_classify_market():
    r = tx.classify_market("Will the US enter a recession in 2026?")
    assert r and r[1] == -1 and "recession" in r[0]
    r = tx.classify_market("Will the Fed cut rates at the next meeting?")
    assert r and r[1] == 1
    r = tx.classify_market("Will the Fed hike rates in December?")
    assert r and r[1] == -1
    assert tx.classify_market("Will the Lakers win the NBA championship?") is None
    assert tx.classify_market("Who will win the Super Bowl?") is None
    assert tx.classify_market("Will Taylor Swift release an album?") is None


# ---------------------------------------------------------------------------- feeds
RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>Fed holds rates</title><link>https://x/1</link><guid>g1</guid><pubDate>Mon, 19 Oct 2026 14:00:00 GMT</pubDate>
<description>&lt;p&gt;The Committee &lt;b&gt;decided&lt;/b&gt; to hold.&lt;/p&gt;</description><dc:creator>FOMC</dc:creator></item></channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Post</title><updated>2026-10-19T14:00:00Z</updated>
<link href="https://r/1"/><content>hello $NVDA</content><author><name>u</name></author></entry></feed>"""


def test_parse_feed_rss_and_atom():
    (e,) = parse_feed(RSS)
    assert e["id"] == "g1" and e["text"] == "The Committee decided to hold." and e["author"] == "FOMC" and e["t"] > 1.7e12
    (a,) = parse_feed(ATOM)
    assert a["url"] == "https://r/1" and "NVDA" in a["text"]


def test_parse_feed_rejects_entity_bombs():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    with pytest.raises(Exception):  # noqa: B017 - defusedxml raises EntitiesForbidden
        parse_feed(bomb)


# ---------------------------------------------------------------------------- collectors (mock transport)
def _sctx(handler, cfg=None, universe=UNIVERSE):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SourceCtx(client, universe, cfg or DEFAULT_CONFIG, {}, {})


async def test_polymarket_source_filters_and_parses():
    rows = [
        {"id": "1", "question": "Will the US enter a recession in 2026?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.3", "0.7"]',
         "volume24hr": 50000, "liquidityNum": 90000, "endDate": "2026-12-31T00:00:00Z", "oneDayPriceChange": 0.04, "slug": "rec",
         "events": [{"slug": "recession-2026"}]},
        {"id": "2", "question": "Will the Lakers win the NBA title?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.3", "0.7"]',
         "volume24hr": 900000, "liquidityNum": 9e5, "endDate": "2027-06-01T00:00:00Z"},
        {"id": "3", "question": "Will the S&P 500 close above 7000?", "outcomes": '["Over", "Under"]', "outcomePrices": '["0.5", "0.5"]',
         "volume24hr": 90000, "liquidityNum": 9e5},
        {"id": "4", "question": "Will tariffs on EU be raised?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]',
         "volume24hr": 10, "liquidityNum": 5},
    ]
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("offset"))
        return httpx.Response(200, json=rows if req.url.params.get("offset") == "0" else [])

    b = await PolymarketSource().poll(_sctx(handler))
    assert [m.id for m in b.markets] == ["polymarket:1"]
    m = b.markets[0]
    assert m.p == 0.3 and m.dp24 == 0.04 and m.polarity == -1 and m.url.endswith("recession-2026")


async def test_kalshi_source_skips_sports_and_parses_dollars():
    ev = {"events": [
        {"event_ticker": "KXMVECROSS-1", "category": "Sports", "title": "x", "markets": []},
        {"event_ticker": "KXRECSS-26", "category": "Economics", "title": "Will the US enter a recession in 2026?",
         "markets": [{"ticker": "KXRECSS-26-Y", "yes_sub_title": "Yes", "yes_bid_dollars": "0.20", "yes_ask_dollars": "0.24",
                      "previous_price_dollars": "0.18", "volume_24h_fp": "5000.0", "open_interest_fp": "9000",
                      "close_time": "2026-12-31T00:00:00Z"}]}], "cursor": ""}
    b = await KalshiSource().poll(_sctx(lambda r: httpx.Response(200, json=ev)))
    assert len(b.markets) == 1 and b.markets[0].p == pytest.approx(0.22) and b.markets[0].dp24 == pytest.approx(0.04)


async def test_vix_source_history_split_and_cboe_fallback():
    def chart(price, closes):
        return {"chart": {"result": [{"meta": {"regularMarketPrice": price}, "indicators": {"quote": [{"close": closes}]}}]}}

    def handler(req: httpx.Request) -> httpx.Response:
        sym = req.url.path.rsplit("/", 1)[-1]
        if "VIX9D" in sym:
            return httpx.Response(200, json=chart(18.0, [18.0]))
        if "VIX3M" in sym:
            return httpx.Response(200, json=chart(20.0, [20.0]))
        if "VVIX" in sym or "SKEW" in sym:
            return httpx.Response(404)
        return httpx.Response(200, json=chart(19.0, [15.0, 16.0, 17.0, 19.0]))  # last bar == spot -> today's bar, excluded

    b = await VixSource().poll(_sctx(handler))
    v = b.vix
    assert v and v["vix"] == 19.0 and v["prev_close"] == 17.0 and v["hist"][-1] == 17.0
    assert v["vix3m"] == 20.0 and v["vvix"] is None  # failed auxiliary series degrade to None, not an error

    csv_text = "DATE,OPEN,HIGH,LOW,CLOSE\n" + "\n".join(f"01/{i:02d}/2026,1,1,1,{15 + i * 0.1}" for i in range(1, 29))

    def fallback(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=csv_text) if "cboe" in req.url.host else httpx.Response(500)

    b = await VixSource().poll(_sctx(fallback))
    assert b.vix and b.vix["vix"] == pytest.approx(17.8)


# ---------------------------------------------------------------------------- config safety
def test_validate_config_and_url_checks():
    ok = validate_config({"subreddits": ["r/wallstreetbets", "stocks"], "x_handles": ["@elonmusk"], "sources": {"x": False}})
    assert ok["subreddits"] == ["wallstreetbets", "stocks"] and ok["x_handles"] == ["elonmusk"] and ok["sources"] == {"x": False}
    with pytest.raises(ValueError):
        validate_config({"subreddits": ["bad name!"]})
    with pytest.raises(ValueError):
        validate_config({"rss": [{"url": "http://127.0.0.1:8765/api/status"}]})
    with pytest.raises(ValueError):
        validate_config({"rss": [{"url": "file:///etc/passwd"}]})
    for bad in ("http://localhost/x", "http://192.168.1.5/feed", "http://10.0.0.1/", "http://169.254.169.254/", "ftp://example.com/x"):
        with pytest.raises(ValueError):
            _check_url(bad)
    _check_url("https://www.federalreserve.gov/feeds/press_all.xml")
    assert validate_config({"movers": [{"name": "X", "weight": 9}]})["movers"][0]["weight"] == 1.0


# ---------------------------------------------------------------------------- service
@pytest.fixture
def svc():
    store = Store(":memory:")
    s = IntelService(Bus(), store, "sim", {}, lambda: sorted(UNIVERSE), None)
    yield s
    store.close()


def _soc(i, sym, text, t=None, label=None, eng=10.0):
    return IntelItem(f"s{i}", t or now_ms(), "social", "reddit", "u", text, symbols=[sym], engagement=eng, label=label)


def test_social_signal_bullish_with_attention_and_thin_data_guard(svc):
    for i in range(12):
        svc._accept(_soc(i, "NVDA", "$NVDA calls printing 🚀 bullish breakout", label="bullish" if i % 2 else None))
    r = svc.social_signal("NVDA")
    assert r and r[0] > 0 and r[1] > 0.5
    for i in range(2):  # only two posts: below the minimum sample -> no signal
        svc._accept(_soc(100 + i, "AMD", "$AMD bullish"))
    assert svc.social_signal("AMD") is None
    assert svc.social_signal("TSLA") is None


def test_social_dedup_and_old_items_dropped(svc):
    it = _soc(1, "NVDA", "$NVDA bullish")
    assert svc._accept(it) and not svc._accept(it)
    assert not svc._accept(_soc(2, "NVDA", "$NVDA bullish", t=now_ms() - 100 * HOUR))
    # social posts without any known ticker are not kept
    assert not svc._accept(IntelItem("n1", now_ms(), "social", "reddit", "u", "just vibes today", symbols=[]))


def _stmt(i, text, mover="Trump", w=1.0, age_h=0.2):
    return IntelItem(f"m{i}", now_ms() - int(age_h * HOUR), "statement", "truth", mover, text, mover=mover, weight=w)


def test_statement_signal_market_wide_theme_and_decay(svc):
    svc._accept(_stmt(1, "We just reached a trade deal on tariffs with China, rate cut coming"))
    up = svc.statement_signal("NVDA", beta=1.5)
    assert up and up[0] > 0 and up[1] > 0
    assert svc.statement_signal("XLE", beta=0.0)[0] == pytest.approx(0.0, abs=1e-9)  # beta 0 market-wide effect vanishes
    svc2 = IntelService(Bus(), Store(":memory:"), "sim", {}, lambda: sorted(UNIVERSE), None)
    svc2._accept(_stmt(1, "New 25% tariffs on semiconductors, trade war", age_h=0.1))
    fresh = svc2.statement_signal("NVDA")
    svc3 = IntelService(Bus(), Store(":memory:"), "sim", {}, lambda: sorted(UNIVERSE), None)
    svc3._accept(_stmt(1, "New 25% tariffs on semiconductors, trade war", age_h=9))
    stale = svc3.statement_signal("NVDA")
    assert fresh[0] < 0 and abs(stale[0]) < abs(fresh[0]) / 3


def test_irrelevant_statements_filtered_unless_high_weight(svc):
    assert not svc._accept(_stmt(1, "Happy birthday to a great friend", mover="Sec", w=0.4))
    assert svc._accept(_stmt(2, "Happy birthday to a great friend", mover="Trump", w=1.0))  # kept for the feed/LLM
    assert svc.statement_signal("SPY") is None  # but carries no signal (strength 0)


async def test_sim_source_feeds_every_signal_input(svc):
    for _ in range(25):
        svc._ingest(svc.sources["sim"], await svc.sources["sim"].poll(SourceCtx(None, svc.universe, svc.cfg, {}, svc._scratch["sim"])))  # type: ignore[arg-type]
    assert svc.vix and svc.markets and svc.items and svc.mentions
    m = svc.macro()
    assert -1 <= m["risk_on"] <= 1 and 0 <= m["event_risk"] <= 1 and m["prediction"]["n"] >= 3
    assert svc.leaderboard(5) and svc.summary()["macro"]["vix"]["label"]
    assert svc.status_bits()["vix"] > 0
    d = svc.dossier("SPY")
    assert d["macro"]["vix_regime"] and "social" in d


def test_prediction_market_dp24_uses_own_history(svc):
    from vantage.intel.types import PredictionMarket

    t = now_ms()
    m = PredictionMarket("pm1", "polymarket", "Will tariffs rise?", "", 0.6, 0.0, 1e5, 1e5, None, ["tariffs"], -1, 0.9, t)
    svc._pm_hist["pm1"].append((t - 23 * HOUR, 0.4))
    svc._set_markets([m])
    assert m.dp24 == pytest.approx(0.2)


async def test_config_roundtrip_persists(svc):
    svc.update_config({"enabled": False, "subreddits": ["stocks"], "sources": {"sim": False}})
    assert svc.states["sim"].enabled is False and not svc.enabled
    saved = svc.store.kv_get("global", "intel")
    assert saved["subreddits"] == ["stocks"] and saved["enabled"] is False


# ---------------------------------------------------------------------------- strategy
class _Sigs:
    def __init__(self, latest):
        self.latest = latest

    def forecast(self, s, h):
        return {"z": 1.0, "exp_return": 0.01, "prob_up": 0.55, "vol": 0.3, "contributions": [{"model": "x"}], "mu_annual": 0.1}


def _ctx(scores, vix_label="normal", event_risk=0.2, price=102.0, prev=100.0, crowded=False):
    latest = {}
    for m, (s, c) in scores.items():
        latest[("NVDA", m)] = Signal(m, "NVDA", s, c, 1, now_ms(), {"crowded": crowded} if m == "social_sentiment" else {})
    intel = SimpleNamespace(enabled=True, macro=lambda: {"vix": {"label": vix_label, "vix": 20.0}, "event_risk": event_risk})
    cand = {"contract": "NVDA261120C00250000", "underlying": "NVDA", "type": "call", "strike": 250.0, "expiration": "2026-11-20",
            "bid": 2.0, "ask": 2.1, "volume": 500, "open_interest": 900, "iv": 0.4, "delta": 0.5, "theta": -0.05, "entry_price": 2.05,
            "ev_pct": 0.2, "pop": 0.5, "kelly": 0.1, "edge_score": 3.0, "iv_rank": 0.4, "iv_rv_ratio": 1.0, "dte": 30,
            "spread_pct": 0.05, "breakeven": 252.1, "type_": "call"}

    async def evaluate(u, params, side, forecast):
        return [cand]

    async def daily_bars(u, n):
        t = now_ms()
        return [SimpleNamespace(t=t - 2 * 86_400_000, c=prev), SimpleNamespace(t=t, c=price)]

    ms = clock.MarketState(True, "regular", 0, 0, 100.0, 200.0, 0)
    return StrategyContext("sim", SimpleNamespace(price=lambda u: price, daily_bars=daily_bars), None, _Sigs(latest),
                           SimpleNamespace(evaluate=evaluate), None, None, None, None, {"equity": 10_000}, [], "small",
                           {"kelly_multiplier": 0.25, "max_risk_per_trade": 0.05, "max_position_pct": 0.5}, ["NVDA"], ms, intel=intel)


async def test_catalyst_requires_agreement_and_primary():
    s = IntelCatalyst()
    both = {"social_sentiment": (0.7, 0.9), "statement_shock": (0.8, 0.9)}
    assert len(await s.generate(_ctx(both))) == 1
    only_one = {"social_sentiment": (0.9, 1.0)}
    assert await s.generate(_ctx(only_one)) == []  # one noisy source never trades
    secondary_only = {"prediction_tilt": (0.9, 0.9), "vix_regime": (0.9, 0.9)}
    assert await s.generate(_ctx(secondary_only)) == []  # needs a primary catalyst
    conflict = {"social_sentiment": (0.7, 0.9), "statement_shock": (0.8, 0.9), "prediction_tilt": (-0.9, 0.9)}
    assert await s.generate(_ctx(conflict)) == []  # a strong opposing component vetoes


async def test_catalyst_regime_filters_only_shrink_or_veto():
    s = IntelCatalyst()
    both = {"social_sentiment": (0.7, 0.9), "statement_shock": (0.8, 0.9)}
    assert await s.generate(_ctx(both, vix_label="panic")) == []
    (stressed,) = await s.generate(_ctx(both, vix_label="stressed"))
    assert stressed.size_multiplier == pytest.approx(0.5)
    (risky,) = await s.generate(_ctx(both, event_risk=0.9))
    assert risky.size_multiplier == pytest.approx(0.7)
    (ok,) = await s.generate(_ctx(both))
    assert ok.size_multiplier == 1.0 and ok.use_llm_gate and ok.direction == 1
    # bearish idea in panic is allowed (puts), and the multiplier never exceeds 1
    bear = {"social_sentiment": (-0.7, 0.9), "statement_shock": (-0.8, 0.9)}
    (p,) = await s.generate(_ctx(bear, vix_label="panic", price=98.0))
    assert p.direction == -1 and p.size_multiplier <= 1.0


async def test_catalyst_price_confirmation_and_crowding():
    s = IntelCatalyst()
    both = {"social_sentiment": (0.7, 0.9), "statement_shock": (0.8, 0.9)}
    assert await s.generate(_ctx(both, price=100.0)) == []  # no confirming move
    assert await s.generate(_ctx(both, price=110.0)) == []  # already extended (>6 %)
    assert await s.generate(_ctx(both, price=99.0)) == []  # moving the wrong way
    assert await s.generate(_ctx(both, crowded=True)) == []  # crowd already one-sided in our direction


async def test_catalyst_is_inert_without_intel_or_in_backtest():
    s = IntelCatalyst()
    c = _ctx({"social_sentiment": (0.7, 0.9), "statement_shock": (0.8, 0.9)})
    c.backtest = True
    assert await s.generate(c) == []
    c.backtest, c.intel = False, None
    assert await s.generate(c) == []


def test_composite_is_bounded():
    c = _ctx({m: (1.0, 1.0) for m in ("social_sentiment", "statement_shock", "prediction_tilt", "vix_regime")})
    assert composite(c, "NVDA")["score"] == pytest.approx(1.0)
    assert composite(c, "AAPL")["score"] == 0.0


# ---------------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    s = Settings(_env_file=None, vantage_mode="sim", data_dir=tmp_path_factory.mktemp("d"), llm_enabled=False,
                 universe=["SPY", "QQQ", "AAPL", "NVDA", "TSLA"])
    with TestClient(create_app(s, start_mode="sim")) as c:
        yield c


def test_api_intel_endpoints(client):
    import time

    for _ in range(40):  # the sim source's first poll runs ~1 s after startup
        s = client.get("/api/intel/summary").json()
        if s["macro"]["vix"] and s["markets"]:
            break
        time.sleep(0.25)
    assert s["macro"]["vix"]["label"] and s["markets"] and s["social"] is not None
    assert client.get("/api/intel/items?kind=bogus").status_code == 400
    assert isinstance(client.get("/api/intel/items?kind=social&limit=5").json(), list)
    assert client.get("/api/intel/markets").json()
    src = client.get("/api/intel/sources").json()
    assert [x["id"] for x in src["sources"]] == ["sim"] and "REDDIT_CLIENT_SECRET" not in str(src)
    assert client.put("/api/intel/config", json={"subreddits": ["bad name"]}).status_code == 400
    r = client.put("/api/intel/config", json={"subreddits": ["stocks"]}).json()
    assert r["config"]["subreddits"] == ["stocks"]
    assert client.post("/api/intel/refresh", json={"source": "nope"}).status_code == 404
    assert client.post("/api/intel/refresh", json={"source": "sim"}).status_code == 200
    assert client.get("/api/status").json()["intel"]["regime"]
    assert any(m["id"] == "social_sentiment" for m in client.get("/api/signals/models").json())
    assert any(x["id"] == "intel_catalyst" and not x["enabled"] for x in client.get("/api/strategies").json())


def test_ws_intel_channels_accepted(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "sub", "ch": ["intel", "macro"]})
        got = set()
        for _ in range(40):
            msg = ws.receive_json(mode="text")
            for b in msg.get("b", []):
                got.add(b["ch"])
            if "macro" in got:
                break
        assert "macro" in got


# ---------------------------------------------------------------------------- LLM reply validation (shared gateway)
def test_validate_clamps_and_drops_malformed_array_items():
    from vantage.llm.gateway import _validate
    from vantage.llm.prompts import INTEL_SCHEMA, NEWS_SCHEMA

    d = {"items": [{"symbol": "AAPL", "direction": 5, "magnitude": 0.5, "relevance": 1, "novelty": 0.5, "horizon": "days"},
                   {"symbol": "MSFT", "direction": 0.2},  # missing required keys -> dropped
                   "junk"]}
    assert _validate(d, NEWS_SCHEMA) is None
    assert [i["symbol"] for i in d["items"]] == ["AAPL"] and d["items"][0]["direction"] == 1  # clamped to the schema max
    r = {"market_relevant": True, "market_direction": -3, "magnitude": 2, "novelty": 1, "affected": [{"symbol": "NVDA", "direction": -9}]}
    assert _validate(r, INTEL_SCHEMA) is None
    assert r["market_direction"] == -1 and r["magnitude"] == 1 and r["affected"][0]["direction"] == -1
    assert _validate({"market_relevant": "yes", "market_direction": 0, "magnitude": 0, "novelty": 0}, INTEL_SCHEMA) == "market_relevant not a boolean"
    assert _validate({"market_relevant": True, "market_direction": True, "magnitude": 0, "novelty": 0}, INTEL_SCHEMA) == "market_direction not a number"
