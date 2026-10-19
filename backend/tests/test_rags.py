from __future__ import annotations

import math

import pytest

from vantage.core import clock
from vantage.core.types import Bar, TradeIdea
from vantage.data.market import MarketDataService
from vantage.execution.risk import RiskManager
from vantage.quant import growth
from vantage.strategies.base import StrategyContext
from vantage.strategies.presets import TIER_BY_ID, presets_wire, tier_for_equity
from vantage.strategies.rags import AGGRESSION, RagsToRiches

MS = clock.MarketState(True, "regular", 0, 0, 0, 60.0, 300.0)


# ----------------------------------------------------------------------------- odds math
def test_baseline_and_kelly():
    assert growth.baseline_win_rate(2.5) == pytest.approx(1 / 3.5)
    assert growth.baseline_win_rate(1.0) == 0.5
    # binary bet: win +1, lose -1, p=0.6 -> Kelly 0.2 of the stake at risk; with s=1 that's 20 % of equity
    assert growth.kelly_fraction(0.6, 1.0, 1.0, 0.0) == pytest.approx(0.2)
    assert growth.kelly_fraction(0.3, 0.02, 2.5, 0.0005, 0.15) == 0.0  # below break-even: no bet
    be = growth.breakeven_win_rate(0.02, 2.5, 0.0005, 0.15)
    assert be > growth.baseline_win_rate(2.5)  # friction raises break-even above chance


def test_goal_odds_monotonic_and_validated():
    kw = dict(start=50, goal=1000, floor=10, position_frac=0.7, stop_frac=0.02, reward_r=2.5, max_trades=1500,
              sims=4000, edge_sd=0.0)
    none = growth.goal_odds(edge_pp=0.0, **kw)
    good = growth.goal_odds(edge_pp=0.10, **kw)
    assert none["p_floor"] > 0.9 and none["p_goal"] < 0.05
    assert good["p_goal"] > 0.9
    assert good["kelly_fraction"] > 0 and math.isfinite(good["log_growth_per_trade"])
    with pytest.raises(ValueError):
        growth.goal_odds(start=50, goal=40, floor=10, position_frac=0.5, stop_frac=0.02, reward_r=2)
    with pytest.raises(ValueError):  # one loss would wipe out the account
        growth.goal_odds(start=50, goal=100, floor=1, position_frac=1.0, stop_frac=0.99, reward_r=2, stop_slippage=0.5)


# ----------------------------------------------------------------------------- risk sizing by target fraction
def test_target_fraction_sizes_exactly_within_caps():
    acct = {"equity": 50.0, "last_equity": 50.0, "cash": 50.0, "buying_power": 50.0, "options_buying_power": 50.0}
    rm = RiskManager(TIER_BY_ID["rags"]["risk"], "paper")
    idea = TradeIdea("rags_to_riches", "TQQQ", "TQQQ", "equity", 1, 100.0, 5.0, 0.01, 0.45, 0.0, 0.2, "t",
                     stop_pct=0.02, target_fraction=0.7)
    plan, why = rm.size_and_check(idea, acct, [], set(), MS, intraday=True, tier_fractional=True)
    assert plan is not None, why
    assert plan.qty * 100.0 == pytest.approx(35.0, rel=0.01)  # 70 % of $50 in fractional shares
    capped = RiskManager({**TIER_BY_ID["rags"]["risk"], "max_position_pct": 0.2}, "paper")
    plan, _ = capped.size_and_check(idea, acct, [], set(), MS, intraday=True, tier_fractional=True)
    assert plan.qty * 100.0 == pytest.approx(10.0, rel=0.01)  # hard cap still binds


# ----------------------------------------------------------------------------- strategy behaviour
def _bars(sym: str, closes: list[float], vols: list[float], t0: int = 1_700_000_000_000) -> list[Bar]:
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, vols, strict=True)):
        o = prev
        out.append(Bar(sym, t0 + i * 60_000, o, max(o, c) + 0.02, min(o, c) - 0.02, c, v))
        prev = c
    return out


def _orb_breakout(base: float = 100.0) -> list[Bar]:
    drive = [base, base + 0.3, base + 0.6, base + 0.8, base + 1.0]  # opening drive up (range ~ base..base+1.02)
    chop = [base + 0.6 + 0.2 * math.sin(i) for i in range(20)]  # consolidation inside the range
    brk = [base + 1.6]  # breakout on volume
    vols = [3000] * 5 + [1000] * 20 + [6000]
    return _bars("X", drive + chop + brk, vols)


class FakeMarket:
    def __init__(self, series: dict[str, list[Bar]]):
        self.series = series
        self.acquired: list[str] = []

    async def acquire(self, syms):  # noqa: ANN001
        self.acquired += list(syms)

    async def intraday_bars(self, sym, n=390):  # noqa: ANN001
        return [Bar(sym, b.t, b.o, b.h, b.l, b.c, b.v) for b in self.series.get(sym, [])]

    def price(self, sym):  # noqa: ANN001
        bars = self.series.get(sym)
        return bars[-1].c if bars else None

    async def ensure_price(self, sym):  # noqa: ANN001
        return self.price(sym)

    ohlcv = staticmethod(MarketDataService.ohlcv)


def _ctx(market, equity=50.0, last=50.0, today=None, owned=None, now=1_700_010_000_000):
    acct = {"equity": equity, "last_equity": last, "cash": equity, "buying_power": equity}
    return StrategyContext("sim", market, None, None, None, None, None, None, None, acct, [], "rags", {}, [], MS,
                           now=now, today=today or {}, owned=owned or {})


def _strategy(**params):
    s = RagsToRiches({"symbols": "TQQQ", "allow_options": False, "use_most_actives": False, **params})
    return s


async def test_orb_breakout_produces_sized_idea():
    m = FakeMarket({"TQQQ": _orb_breakout(), "QQQ": _orb_breakout(500)})
    s = _strategy()
    ideas = await s.generate(_ctx(m))
    assert ideas, "expected an ORB idea"
    i = ideas[0]
    eff = AGGRESSION["aggressive"]
    assert i.symbol == "TQQQ" and i.target_fraction == eff["position_pct"]
    assert i.signals["setup"]["setup"] in ("orb", "hod_momentum") and i.signals["intraday"]
    assert 0.5 * eff["stop_pct"] <= i.stop_pct <= 2 * eff["stop_pct"]
    assert i.target_pct == pytest.approx(eff["reward_r"] * i.stop_pct)
    assert "TQQQ" in m.acquired and "SQQQ" in m.acquired  # streams bars for long + inverse legs


async def test_bearish_setup_buys_inverse_etf():
    up = _orb_breakout()
    down = [Bar("TQQQ", b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in up]  # mirror image
    m = FakeMarket({"TQQQ": down, "SQQQ": _bars("SQQQ", [20.0] * 26, [1000] * 26), "QQQ": [Bar("QQQ", b.t, 600 - b.o, 600 - b.l, 600 - b.h, 600 - b.c, b.v) for b in up]})
    ideas = await _strategy().generate(_ctx(m))
    assert ideas and ideas[0].symbol == "SQQQ"
    assert not await _strategy(allow_inverse=False).generate(_ctx(m))


async def test_brakes_block_entries():
    m = FakeMarket({"TQQQ": _orb_breakout(), "QQQ": _orb_breakout(500)})
    s = _strategy()
    now = 1_700_010_000_000
    assert not await s.generate(_ctx(m, today={s.id: {"entries": 6}}))  # daily cap (aggressive = 6)
    assert not await s.generate(_ctx(m, today={s.id: {"last_entry_t": now - 60_000}}, now=now))  # spacing
    assert not await s.generate(_ctx(m, today={s.id: {"consecutive_losses": 3}}))  # losing streak
    assert not await s.generate(_ctx(m, equity=36.0, last=50.0))  # -28 % today > 25 % daily stop
    assert not await s.generate(_ctx(m, owned={"SOXL": s.id}))  # max concurrent 1
    assert await s.generate(_ctx(m))  # control: same data trades when nothing blocks


def test_goal_and_floor_halts():
    s = _strategy(goal_equity=100.0, floor_equity=10.0)
    m = FakeMarket({})
    assert s.should_halt(_ctx(m, equity=50.0)) is None
    g = s.should_halt(_ctx(m, equity=101.0))
    assert "goal reached" in g["reason"] and g["flatten"] and g["next_preset"]
    f = s.should_halt(_ctx(m, equity=9.5))
    assert "floor" in f["reason"] and f["flatten"] and not f["next_preset"]


def test_aggressiveness_presets_and_custom():
    assert RagsToRiches({"aggressiveness": "moderate"}).effective()["position_pct"] == 0.35
    custom = RagsToRiches({"aggressiveness": "custom", "position_pct": 0.15, "max_trades_per_day": 20}).effective()
    assert custom["position_pct"] == 0.15 and custom["max_trades_per_day"] == 20
    assert RagsToRiches().effective() == {k: AGGRESSION["aggressive"][k] for k in AGGRESSION["aggressive"]}


def test_rags_preset_is_manual_only():
    assert all(tier_for_equity(x) != "rags" for x in (20, 50, 5_000, 1e6))
    assert next(p for p in presets_wire() if p["tier"] == "rags")["manual"] is True
    assert TIER_BY_ID["rags"]["strategies"] == ["rags_to_riches"]


def test_setups_need_volume_and_direction():
    s = _strategy()
    flat = _bars("X", [100.0] * 30, [1000] * 30)
    a = MarketDataService.ohlcv(flat)
    assert s._setups(a, 0) == []
