from __future__ import annotations

import math

import numpy as np
import pytest

from vantage.quant import bs, filters, indicators, kelly, microstructure, option_ev, stats, vol

R, Q = 0.04, 0.01


def crr_american(S, K, T, r, q, sigma, is_call, n=800):
    dt = T / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1 / u
    p = (math.exp((r - q) * dt) - d) / (u - d)
    disc = math.exp(-r * dt)
    j = np.arange(n + 1)
    ST = S * u**j * d ** (n - j)
    V = np.maximum(ST - K, 0) if is_call else np.maximum(K - ST, 0)
    for i in range(n - 1, -1, -1):
        ST = S * u ** np.arange(i + 1) * d ** (i - np.arange(i + 1))
        V = disc * (p * V[1:] + (1 - p) * V[:-1])
        V = np.maximum(V, ST - K if is_call else K - ST)
    return float(V[0])


# ----------------------------------------------------------------------------- Black-Scholes
def test_put_call_parity():
    S, K, T, s = 100.0, np.array([80, 100, 120.0]), 0.5, 0.3
    c = bs.bs_price(S, K, T, R, Q, s, True)
    p = bs.bs_price(S, K, T, R, Q, s, False)
    assert np.allclose(c - p, S * math.exp(-Q * T) - K * math.exp(-R * T), atol=1e-10)


@pytest.mark.parametrize("K", [60.0, 90.0, 100.0, 115.0, 160.0])
@pytest.mark.parametrize("T", [3 / 365, 0.25, 2.0])
@pytest.mark.parametrize("call", [True, False])
def test_iv_roundtrip(K, T, call):
    S, sig = 100.0, 0.35
    px = bs.bs_price(S, K, T, R, 0.0, sig, call)
    fwd_intrinsic = max(0.0, (S - K * math.exp(-R * T)) if call else (K * math.exp(-R * T) - S))
    if px - fwd_intrinsic < 1e-6:
        pytest.skip("no time value: IV is not identifiable")
    iv = bs.implied_vol(px, S, K, T, R, 0.0, call)
    assert abs(iv - sig) < 1e-4


def test_iv_outside_bounds_is_nan():
    assert math.isnan(bs.implied_vol(0.01, 100, 50, 0.5, R, 0, True))  # below intrinsic


def test_greeks_vs_finite_differences():
    S, K, T, s = 105.0, 100.0, 0.4, 0.25
    for call in (True, False):
        g = bs.bs_greeks(S, K, T, R, Q, s, call)
        h = 1e-3
        f = lambda call=call, **kw: bs.bs_price(kw.get("S", S), K, kw.get("T", T), R, Q, kw.get("s", s), call)  # noqa: E731
        assert abs(g["delta"] - (f(S=S + h) - f(S=S - h)) / (2 * h)) < 1e-5
        assert abs(g["gamma"] - (f(S=S + h) - 2 * f() + f(S=S - h)) / h**2) < 1e-3
        assert abs(g["vega"] - (f(s=s + 1e-4) - f(s=s - 1e-4)) / 2e-4 / 100) < 1e-5
        assert abs(g["theta"] - (f(T=T - 1e-5) - f(T=T + 1e-5)) / 2e-5 / 365) < 1e-4


def test_bjerksund_stensland():
    S, K, T, s = 100.0, 100.0, 1.0, 0.3
    assert abs(bs.bjerksund_stensland(S, K, T, R, 0.0, s, True) - bs.bs_price(S, K, T, R, 0.0, s, True)) < 1e-8
    am_put = bs.bjerksund_stensland(S, K, T, 0.06, 0.0, s, False)
    eu_put = bs.bs_price(S, K, T, 0.06, 0.0, s, False)
    assert am_put >= eu_put - 1e-9
    assert abs(am_put - crr_american(S, K, T, 0.06, 0.0, s, False)) / am_put < 0.01


# ----------------------------------------------------------------------------- volatility
def gbm_ohlc(sigma=0.3, n=600, seed=1):
    rs = np.random.default_rng(seed)
    steps = 78
    dt = 1 / (252 * steps)
    path = 100 * np.exp(np.cumsum(rs.normal(-0.5 * sigma**2 * dt, sigma * math.sqrt(dt), n * steps)))
    days = path.reshape(n, steps)
    o = np.r_[100.0, days[:-1, -1]]
    return o, days.max(axis=1), days.min(axis=1), days[:, -1]


def test_vol_estimators_recover_sigma():
    o, h, l, c = gbm_ohlc()
    for est in (vol.yang_zhang(o, h, l, c, 250), vol.garman_klass(o, h, l, c, 250), vol.close_to_close(c, 250)):
        assert 0.24 < est < 0.36, est
    assert 0.2 < vol.forecast_vol(o, h, l, c, 10) < 0.4
    assert vol.iv_rank(0.3, np.linspace(0.2, 0.4, 30)) == pytest.approx(0.5)
    assert vol.iv_rank(0.3, [0.2] * 5) is None


# ----------------------------------------------------------------------------- stats
def test_psr_dsr():
    assert stats.probabilistic_sharpe_ratio(0.1, 252, 0, 3, 0.1) == pytest.approx(0.5)
    a = stats.probabilistic_sharpe_ratio(0.1, 252, 0, 3)
    b = stats.probabilistic_sharpe_ratio(0.1, 1000, 0, 3)
    assert 0.5 < a < b < 1
    assert stats.deflated_sharpe_ratio(0.1, 252, 0, 3, 50, 1 / 252) < a


def test_performance_metrics():
    eq = np.array([100, 110, 99, 120.0])
    assert stats.max_drawdown(eq) == pytest.approx(0.1)
    assert stats.profit_factor([10, -5, 5]) == pytest.approx(3.0)
    assert stats.win_rate([1, -1, 1, 1]) == 0.75
    rs = np.random.default_rng(3)
    x = np.cumsum(rs.normal(size=2000))
    assert 0.4 < stats.hurst_exponent(x) < 0.6
    ou = np.zeros(2000)
    for i in range(1, 2000):
        ou[i] = 0.9 * ou[i - 1] + rs.normal()
    assert stats.ou_half_life(ou) == pytest.approx(math.log(2) / 0.1, rel=0.3)
    s = rs.normal(size=200)
    assert stats.spearman_ic(s, s + rs.normal(size=200) * 0.1) > 0.9


# ----------------------------------------------------------------------------- kelly
def test_kelly():
    assert kelly.kelly_binary(0.6, 1, 1) == pytest.approx(0.2)
    assert kelly.kelly_discrete([1, -1], [0.6, 0.4]) == pytest.approx(0.2, abs=1e-5)
    assert kelly.kelly_discrete([0.5, -0.5], [0.4, 0.6]) == 0.0
    R = np.array([[1, -1], [2, -1], [0.5, -1.0]])
    batch = kelly.kelly_discrete_batch(R, [0.6, 0.4])
    for i in range(3):
        assert batch[i] == pytest.approx(kelly.kelly_discrete(R[i], [0.6, 0.4]), abs=1e-4)


# ----------------------------------------------------------------------------- option EV
def test_option_ev_martingale_identity():
    """Under μ = r, σ_p = IV and constant IV, E[V_h] = BS·e^{rh} (discounted price is a martingale)."""
    S, K, T, iv, h = 100.0, np.array([90, 100, 110.0]), 0.5, 0.3, 0.1
    for call in (True, False):
        out = option_ev.evaluate_long_options(S, K, T, call, 1.0, 0.0, R, 0.0, iv, R, iv, h)
        bsp = bs.bs_price(S, K, T, R, 0.0, iv, call)
        assert np.allclose(out["value_h"], bsp * math.exp(R * h), rtol=2e-3)
        out = option_ev.evaluate_long_options(S, K, T, call, bsp, 0.0, R, 0.0, iv, R, iv, h)
        assert np.allclose(out["ev_pct"], math.exp(R * h) - 1, atol=3e-3)


def test_option_ev_drift_and_costs():
    S, K, T = 100.0, 105.0, 0.25
    px_c = bs.bs_price(S, K, T, R, 0, 0.3, True)
    px_p = bs.bs_price(S, K, T, R, 0, 0.3, False)
    lo = option_ev.evaluate_long_options(S, K, T, True, px_c, 0.0, R, 0, 0.3, 0.0, 0.3, 0.05)
    hi = option_ev.evaluate_long_options(S, K, T, True, px_c, 0.0, R, 0, 0.3, 0.6, 0.3, 0.05)
    assert hi["ev_pct"][0] > lo["ev_pct"][0]
    assert hi["kelly"][0] >= lo["kelly"][0]
    p_lo = option_ev.evaluate_long_options(S, K, T, False, px_p, 0.0, R, 0, 0.3, 0.0, 0.3, 0.05)
    p_hi = option_ev.evaluate_long_options(S, K, T, False, px_p, 0.0, R, 0, 0.3, 0.6, 0.3, 0.05)
    assert p_hi["ev_pct"][0] < p_lo["ev_pct"][0]
    costly = option_ev.evaluate_long_options(S, K, T, True, px_c, 0.05, R, 0, 0.3, 0.6, 0.3, 0.05)
    assert costly["ev_pct"][0] < hi["ev_pct"][0]
    crushed = option_ev.evaluate_long_options(S, K, T, True, px_c, 0.0, R, 0, 0.3, 0.6, 0.3, 0.05, iv_crush=0.3)
    assert crushed["ev_pct"][0] < hi["ev_pct"][0]


# ----------------------------------------------------------------------------- indicators / filters / micro
def test_indicators():
    x = np.arange(1, 11, dtype=float)
    assert indicators.sma(x, 3)[2] == 2.0 and math.isnan(indicators.sma(x, 3)[1])
    assert indicators.rsi(x, 2)[-1] == 100.0
    up, dn = indicators.donchian(x, x, 3)
    assert up[3] == 3.0 and dn[3] == 1.0 and math.isnan(up[2])
    assert indicators.ibs([10], [0], [2])[0] == pytest.approx(0.2)
    assert indicators.rel_volume([1, 1, 1, 3], 3)[3] == 3.0
    vw = indicators.session_vwap([1, 3, 10], [1, 1, 5], [0, 0, 1])
    assert vw[1] == 2.0 and vw[2] == 10.0
    assert indicators.atr([2, 3, 4], [1, 2, 3], [1.5, 2.5, 3.5], 2)[1] == pytest.approx(1.25)


def test_kalman_trend_sign():
    rs = np.random.default_rng(0)
    y = np.log(100) + 0.002 * np.arange(300) + rs.normal(0, 0.01, 300)
    _, slope, var = filters.kalman_trend(y)
    assert slope[-1] > 0 and slope[-1] / math.sqrt(var[-1]) > 2
    _, slope2, _ = filters.kalman_trend(-y)
    assert slope2[-1] < 0


def test_microstructure():
    q = microstructure.quote_rule([10.1, 9.9, 10.0, 10.06], [9.9] * 4, [10.1] * 4)
    assert list(q) == [1, -1, 0, 1]
    assert list(microstructure.tick_rule([1, 2, 2, 1, 1])) == [0, 1, 1, -1, -1]
    assert list(microstructure.lee_ready([10.0, 10.2, 10.0], [9.9] * 3, [10.1] * 3)) == [0, 1, -1]
    assert microstructure.ofi_increment(10, 5, 10.1, 5, 10, 8, 10.1, 5) == 3.0
    assert microstructure.vpin([10, 0], [0, 10]) == 1.0
    x = np.arange(10.0)
    assert microstructure.kyle_lambda(2 * x + 1, x) == pytest.approx(2.0)
