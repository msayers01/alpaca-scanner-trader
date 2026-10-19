from __future__ import annotations

from datetime import date, timedelta

from vantage.core import clock
from vantage.core.types import OptionContract, TradeIdea
from vantage.execution.risk import RiskManager, can_close_same_day, daytrade_capable, is_pdt_rejection

MS = clock.MarketState(True, "regular", 0, 0, 0, 120.0, 200.0)


def account(eq=10_000.0, **kw):
    a = {"equity": eq, "last_equity": eq, "cash": eq, "buying_power": eq, "options_buying_power": eq,
         "daytrade_count": 0, "pattern_day_trader": False, "daytrading_buying_power": 0.0}
    a.update(kw)
    return a


def opt_idea(price=2.0, kelly=0.2, spread=0.03, oi=1000, vol=500, edge=5.0, **kw):
    c = OptionContract("AAPL261120C00250000", "AAPL", "call", 250, date.today() + timedelta(days=30),
                       bid=price * (1 - spread / 2), ask=price * (1 + spread / 2), volume=vol, open_interest=oi)
    return TradeIdea("options_edge", c.symbol, "AAPL", "option", 1, price, edge, 0.2, 0.5, kelly, 10, "test",
                     contract=c, stop_pct=0.4, **kw)


def test_option_sizing_binds_on_smallest_limit():
    rm = RiskManager({"kelly_multiplier": 0.25, "max_risk_per_trade": 0.02, "max_position_pct": 0.10}, "paper")
    plan, why = rm.size_and_check(opt_idea(), account(), [], set(), MS)
    # risk budget: 200 / (200*0.5) = 2 contracts; kelly: 10000*0.05/200 = 2.5; cap 1000/200 = 5
    assert plan is not None and plan.qty == 2.0, why
    assert plan.sizing["binding"] == "risk_budget"


def test_never_rounds_up_to_one_contract():
    rm = RiskManager({"max_risk_per_trade": 0.01}, "paper")
    plan, why = rm.size_and_check(opt_idea(price=5.0), account(1_000), [], set(), MS)
    assert plan is None and "rounds to 0" in why


def test_min_one_contract_only_within_hard_limits():
    # Kelly allows 0.25 contracts (10k * 0.01*0.25 / 100) but one contract fits the $300 risk budget
    rm = RiskManager({"max_risk_per_trade": 0.03, "kelly_multiplier": 0.25}, "paper")
    plan, why = rm.size_and_check(opt_idea(price=1.0, kelly=0.01), account(), [], set(), MS)
    assert plan is not None and plan.qty == 1 and plan.sizing["binding"] == "min_one_contract", why
    rm = RiskManager({"max_risk_per_trade": 0.03, "min_one_contract": False}, "paper")
    assert rm.size_and_check(opt_idea(price=1.0, kelly=0.01), account(), [], set(), MS)[0] is None
    rm = RiskManager({"max_risk_per_trade": 0.001}, "paper")  # $10 budget < $50 at-risk
    assert rm.size_and_check(opt_idea(price=1.0, kelly=0.01), account(), [], set(), MS)[0] is None
    rm = RiskManager({"max_risk_per_trade": 0.03}, "paper")  # zero Kelly = no edge = no trade
    assert rm.size_and_check(opt_idea(price=1.0, kelly=0.0), account(), [], set(), MS)[0] is None


def test_liquidity_and_edge_filters():
    rm = RiskManager({"max_spread_pct": 0.05, "min_edge_score": 2}, "paper")
    assert rm.size_and_check(opt_idea(spread=0.2), account(), [], set(), MS)[0] is None
    assert rm.size_and_check(opt_idea(edge=1.0), account(), [], set(), MS)[0] is None
    assert rm.size_and_check(opt_idea(oi=5), account(), [], set(), MS)[0] is None


def test_exposure_limits_and_duplicates():
    rm = RiskManager({"max_open_positions": 1}, "paper")
    pos = [{"symbol": "MSFT", "underlying": "MSFT", "asset_class": "equity", "market_value": 100}]
    plan, why = rm.size_and_check(opt_idea(), account(), pos, set(), MS)
    assert plan is None and "max open positions" in why
    rm = RiskManager({}, "paper")
    plan, why = rm.size_and_check(opt_idea(), account(), [], {"AAPL261120C00250000"}, MS)
    assert plan is None


def test_market_hours_and_entry_window():
    rm = RiskManager({"entry_start_min": 10}, "paper")
    early = clock.MarketState(True, "regular", 0, 0, 0, 5.0, 385.0)
    assert "first" in rm.size_and_check(opt_idea(), account(), [], set(), early)[1]
    closed = clock.MarketState(False, "closed", 0, 0, 0, None, None)
    assert rm.size_and_check(opt_idea(), account(), [], set(), closed)[1] == "market closed"


def test_kill_switch_daily_loss_and_drawdown():
    rm = RiskManager({"max_daily_loss": 0.03, "max_drawdown": 0.1}, "paper")
    assert rm.check_kill(account(9_600, last_equity=10_000), 10_000) is not None
    assert rm.check_kill(account(9_800, last_equity=10_000), 10_000) is None
    assert rm.check_kill(account(8_900, last_equity=8_950), 10_000) is not None
    rm.kill("x")
    assert "kill switch" in rm.size_and_check(opt_idea(), account(), [], set(), MS)[1]


def test_day_trading_unlimited_since_pdt_elimination():
    # FINRA removed the PDT rule effective 2026-06-04: small accounts may day trade without a count limit
    assert daytrade_capable(account(1_000), "auto")
    assert can_close_same_day(account(1_000, daytrade_count=12), "auto")
    rm = RiskManager({}, "paper")
    assert rm.size_and_check(opt_idea(), account(10_000), [], set(), MS, intraday=True)[0] is not None
    # user preference still honoured
    assert not daytrade_capable(account(100_000), "never")
    rm = RiskManager({"allow_daytrades": "never"}, "paper")
    plan, why = rm.size_and_check(opt_idea(), account(10_000), [], set(), MS, intraday=True)
    assert plan is None and "disabled" in why


def test_falls_back_to_legacy_allowance_if_broker_still_enforces_pdt():
    assert is_pdt_rejection("trade denied due to pattern day trading protection")
    assert not is_pdt_rejection("insufficient buying power")
    assert not is_pdt_rejection("insufficient day trading buying power")  # intraday margin, not PDT
    assert not daytrade_capable(account(10_000), "auto", broker_pdt=True)
    assert daytrade_capable(account(30_000), "auto", broker_pdt=True)
    assert daytrade_capable(account(10_000), "always", broker_pdt=True)
    assert can_close_same_day(account(10_000, daytrade_count=2), "auto", broker_pdt=True)
    assert not can_close_same_day(account(10_000, daytrade_count=3), "auto", broker_pdt=True)
    rm = RiskManager({}, "paper")
    rm.broker_pdt = True
    plan, why = rm.size_and_check(opt_idea(), account(10_000), [], set(), MS, intraday=True)
    assert plan is None and "legacy PDT" in why


def test_earnings_blackout_unless_event_strategy():
    rm = RiskManager({"earnings_blackout_days": 2}, "paper")
    assert rm.size_and_check(opt_idea(), account(), [], set(), MS, days_to_earnings=1)[0] is None
    idea = opt_idea()
    idea.signals["event_ok"] = True
    assert rm.size_and_check(idea, account(), [], set(), MS, days_to_earnings=1)[0] is not None


def test_equity_fractional_vs_whole_shares():
    rm = RiskManager({"max_position_pct": 0.3, "max_risk_per_trade": 0.05, "kelly_multiplier": 0.25}, "paper")
    idea = TradeIdea("tsmom_equity", "NVDA", "NVDA", "equity", 1, 900.0, 5.0, 0.02, 0.55, 1.0, 20, "t", stop_pct=0.1)
    plan, _ = rm.size_and_check(idea, account(500), [], set(), MS, tier_fractional=True)
    assert plan is not None and 0 < plan.qty < 1
    plan, why = rm.size_and_check(idea, account(500), [], set(), MS, tier_fractional=False)
    assert plan is None and "0 shares" in why


def test_llm_multiplier_only_shrinks():
    rm = RiskManager({}, "paper")
    a = opt_idea(kelly=0.5)
    b = opt_idea(kelly=0.5)
    b.size_multiplier = 3.0  # must be clamped to 1
    pa, _ = rm.size_and_check(a, account(100_000), [], set(), MS)
    pb, _ = rm.size_and_check(b, account(100_000), [], set(), MS)
    assert pa.qty == pb.qty
    c = opt_idea(kelly=0.5)
    c.size_multiplier = 0.1
    pc, _ = rm.size_and_check(c, account(100_000), [], set(), MS)
    assert pc.qty <= pa.qty
