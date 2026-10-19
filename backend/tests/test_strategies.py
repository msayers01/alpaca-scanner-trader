from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from vantage.core import clock
from vantage.core.types import ManagedPosition
from vantage.strategies.base import StrategyContext, default_exit
from vantage.strategies.library import REGISTRY
from vantage.strategies.presets import TIERS, tier_for_equity

DAY = 86_400_000


class _Signals:
    def __init__(self, z=0.0):
        self.z = z

    def forecast(self, sym, h):
        return {"z": self.z, "contributions": [{"model": "x"}] if self.z else []}


def ctx(z=0.0, earnings=None, now=10 * DAY):
    cal = SimpleNamespace(days_to_earnings=lambda s: earnings)
    ms = clock.MarketState(True, "regular", 0, 0, now, 100.0, 200.0)
    return StrategyContext("sim", None, None, _Signals(z), None, None, None, cal, None, {}, [], "small", {}, [], ms, now=now)


def pos(plpc=0.0, peak=0.0, entry_t=9 * DAY, asset="option", dte=30, direction=1):
    return ManagedPosition("AAPL261120C00250000", "AAPL", asset, 1, 2.0, 2.0 * (1 + plpc), 200, plpc, "options_edge",
                           direction, entry_t, peak, expiration=clock.today_et() + timedelta(days=dte))


P = {"stop_pct": 0.4, "target_pct": 0.8, "trail_pct": 0.25, "trail_activate_pct": 0.4, "time_stop_days": 14,
     "min_dte_exit": 7, "exit_on_flip": True, "flip_z": 0.75, "exit_before_earnings": True}


def test_exit_rules():
    assert default_exit(ctx(), pos(-0.45), P).urgent
    assert "target" in default_exit(ctx(), pos(0.85), P).reason
    assert "trailing" in default_exit(ctx(), pos(0.2, peak=0.7), P).reason  # 1.2/1.7-1 = -29%
    assert default_exit(ctx(), pos(0.3, peak=0.45), P) is None  # -10% give-back only
    assert "time stop" in default_exit(ctx(now=30 * DAY), pos(0.0), P).reason
    assert "DTE" in default_exit(ctx(), pos(0.0, dte=5), P).reason
    assert "earnings" in default_exit(ctx(earnings=1), pos(0.0), P).reason
    assert "flipped" in default_exit(ctx(z=-1.0), pos(0.0), P).reason
    assert default_exit(ctx(z=-1.0), pos(0.0, direction=-1), P) is None


def test_registry_schemas_complete():
    for sid, cls in REGISTRY.items():
        s = cls()
        names = {p.name for p in s.schema()}
        assert {"stop_pct", "target_pct", "time_stop_days"} <= names, sid
        assert set(s.params) == names
        assert cls.style in ("swing", "intraday")


def test_tiers_cover_equity_range_and_reference_real_strategies():
    assert tier_for_equity(500) == "micro"
    assert tier_for_equity(5_000) == "small"
    assert tier_for_equity(15_000) == "growth"
    assert tier_for_equity(100_000) == "pro"
    for t in TIERS:
        assert set(t["strategies"]) <= set(REGISTRY), t["tier"]
        assert t["risk"]["allow_daytrades"] == "auto", t["tier"]  # PDT rule eliminated 2026-06-04
