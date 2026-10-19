"""Volatility estimators and forecasts. See docs/QUANT.md §2.

Inputs are equal-length arrays of daily OHLC (or returns / realised variances). Range estimators use
the last ``window`` valid bars and return an annualised (×252) volatility as a float, ``nan`` when
there is not enough data.
"""

# ruff: noqa: E741
from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.optimize import minimize
from scipy.signal import lfilter

TRADING_DAYS = 252
_LN2 = math.log(2.0)
_GK_C = 2.0 * _LN2 - 1.0


def _f(x: ArrayLike) -> NDArray[np.float64]:
    return np.asarray(x, dtype=float).ravel()


def _ohlc(o: ArrayLike, h: ArrayLike, l: ArrayLike, c: ArrayLike):
    o_, h_, l_, c_ = _f(o), _f(h), _f(l), _f(c)
    n = min(o_.size, h_.size, l_.size, c_.size)
    return o_[-n:] if n else o_[:0], h_[-n:] if n else h_[:0], l_[-n:] if n else l_[:0], c_[-n:] if n else c_[:0]


def _safe_log(a: NDArray[np.float64], b: NDArray[np.float64]) -> NDArray[np.float64]:
    with np.errstate(all="ignore"):
        out = np.log(a / b)
    return np.where(np.isfinite(out) & (a > 0) & (b > 0), out, np.nan)


def _tail_finite(x: NDArray[np.float64], window: int) -> NDArray[np.float64] | None:
    v = x[np.isfinite(x)]
    if window < 1 or v.size < window:
        return None
    return v[-window:]


def close_to_close(c: ArrayLike, window: int = 20) -> float:
    """Sample std (ddof=1) of the last ``window`` close-to-close log returns × √252."""
    c_ = _f(c)
    r = _safe_log(c_[1:], c_[:-1])
    v = _tail_finite(r, window)
    if v is None or v.size < 2:
        return math.nan
    return float(np.std(v, ddof=1) * math.sqrt(TRADING_DAYS))


def parkinson(h: ArrayLike, l: ArrayLike, window: int = 20) -> float:
    """Parkinson (1980): ``σ² = E[ln(H/L)²] / (4 ln 2)``, annualised."""
    h_, l_ = _f(h), _f(l)
    n = min(h_.size, l_.size)
    hl = _safe_log(h_[h_.size - n :], l_[l_.size - n :])
    v = _tail_finite(hl, window)
    if v is None:
        return math.nan
    return float(math.sqrt(np.mean(v * v) / (4.0 * _LN2) * TRADING_DAYS))


def _gk_terms(o, h, lo, c):
    hl = _safe_log(h, lo)
    co = _safe_log(c, o)
    return 0.5 * hl * hl - _GK_C * co * co


def garman_klass(o: ArrayLike, h: ArrayLike, l: ArrayLike, c: ArrayLike, window: int = 20) -> float:
    """Garman-Klass (1980): ``σ² = E[½ ln(H/L)² − (2 ln 2 − 1) ln(C/O)²]``, annualised."""
    o_, h_, l_, c_ = _ohlc(o, h, l, c)
    v = _tail_finite(_gk_terms(o_, h_, l_, c_), window)
    if v is None:
        return math.nan
    return float(math.sqrt(max(np.mean(v), 0.0) * TRADING_DAYS))


def _rs_terms(o, h, lo, c):
    return _safe_log(h, c) * _safe_log(h, o) + _safe_log(lo, c) * _safe_log(lo, o)


def rogers_satchell(o: ArrayLike, h: ArrayLike, l: ArrayLike, c: ArrayLike, window: int = 20) -> float:
    """Rogers-Satchell (1991): ``σ² = E[ln(H/C) ln(H/O) + ln(L/C) ln(L/O)]`` (drift-independent)."""
    o_, h_, l_, c_ = _ohlc(o, h, l, c)
    v = _tail_finite(_rs_terms(o_, h_, l_, c_), window)
    if v is None:
        return math.nan
    return float(math.sqrt(max(np.mean(v), 0.0) * TRADING_DAYS))


def yang_zhang(o: ArrayLike, h: ArrayLike, l: ArrayLike, c: ArrayLike, window: int = 20) -> float:
    """Yang-Zhang (2000): ``σ² = σ_o² + k σ_c² + (1 − k) σ_RS²`` with ``k = 0.34/(1.34 + (n+1)/(n−1))``;
    σ_o² = var of overnight ``ln(O_t/C_{t−1})``, σ_c² = var of open-to-close ``ln(C_t/O_t)``.
    Uses the last ``window`` bars that have a previous close (needs ``window + 1`` bars)."""
    o_, h_, l_, c_ = _ohlc(o, h, l, c)
    if window < 2 or o_.size < window + 1:
        return math.nan
    ov = _safe_log(o_[1:], c_[:-1])
    oc = _safe_log(c_[1:], o_[1:])
    rs = _rs_terms(o_[1:], h_[1:], l_[1:], c_[1:])
    ok = np.isfinite(ov) & np.isfinite(oc) & np.isfinite(rs)
    if ok.sum() < window:
        return math.nan
    ov, oc, rs = ov[ok][-window:], oc[ok][-window:], rs[ok][-window:]
    n = window
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = np.var(ov, ddof=1) + k * np.var(oc, ddof=1) + (1.0 - k) * np.mean(rs)
    return float(math.sqrt(max(var, 0.0) * TRADING_DAYS))


def _ewma_mean(x: NDArray[np.float64], lam: float) -> float:
    """Exponentially weighted mean, newest weight highest: ``Σ λ^i x_{n−1−i} / Σ λ^i``."""
    if x.size == 0:
        return math.nan
    w = lam ** np.arange(x.size - 1, -1, -1, dtype=float)
    return float(np.sum(w * x) / np.sum(w))


def ewma_vol(returns: ArrayLike, lam: float = 0.94) -> float:
    """RiskMetrics (1996) EWMA vol: ``σ² = Σ (1−λ) λ^i r²_{t−i}`` (finite-sample normalised), ×√252."""
    r = _f(returns)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return math.nan
    return float(math.sqrt(_ewma_mean(r * r, lam) * TRADING_DAYS))


def daily_rv_from_ohlc(o: ArrayLike, h: ArrayLike, l: ArrayLike, c: ArrayLike) -> NDArray[np.float64]:
    """Per-day variance proxy: Garman-Klass per bar plus the squared overnight gap
    ``ln(O_t/C_{t−1})²`` for t ≥ 1 (bar 0 has no gap term). ``nan`` for invalid bars."""
    o_, h_, l_, c_ = _ohlc(o, h, l, c)
    gk = np.maximum(_gk_terms(o_, h_, l_, c_), 0.0)
    gap = np.zeros_like(gk)
    if o_.size > 1:
        g = _safe_log(o_[1:], c_[:-1])
        gap[1:] = g * g
    return gk + gap


def daily_rv_from_intraday(closes_by_day: list[ArrayLike]) -> NDArray[np.float64]:
    """Realised variance per day = Σ squared intraday log returns (Andersen-Bollerslev 1998).
    Days with < 2 valid prices give ``nan``."""
    out = np.full(len(closes_by_day), np.nan)
    for i, day in enumerate(closes_by_day):
        p = _f(day)
        p = p[np.isfinite(p) & (p > 0)]
        if p.size >= 2:
            r = np.diff(np.log(p))
            out[i] = float(np.sum(r * r))
    return out


def _har_design(rv: NDArray[np.float64]):
    """Rows t = 21 … n−2: regressors [1, RV_t, RV^(5)_t, RV^(22)_t], target RV_{t+1}."""
    n = rv.size
    cs = np.concatenate(([0.0], np.cumsum(rv)))
    t = np.arange(21, n - 1)
    rv_d = rv[t]
    rv_w = (cs[t + 1] - cs[t - 4]) / 5.0
    rv_m = (cs[t + 1] - cs[t - 21]) / 22.0
    X = np.column_stack((np.ones(t.size), rv_d, rv_w, rv_m))
    return X, rv[t + 1]


def har_rv_fit(rv: ArrayLike) -> tuple[float, float, float, float] | None:
    """Corsi (2009) HAR-RV by OLS: ``RV_{t+1} = b0 + bd RV_t + bw RV^(5)_t + bm RV^(22)_t``.
    Non-finite values are dropped. Returns ``None`` with < 60 observations."""
    x = _f(rv)
    x = x[np.isfinite(x)]
    if x.size < 60:
        return None
    X, y = _har_design(x)
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(beta)):
        return None
    return float(beta[0]), float(beta[1]), float(beta[2]), float(beta[3])


def har_rv_forecast(rv: ArrayLike, horizon_days: int = 5) -> float:
    """Annualised vol over the next ``horizon_days`` from HAR-RV, iterating the recursion and averaging
    the daily variance forecasts. Falls back to an EWMA (λ = 0.94) of the variances with < 60 obs.
    Clamped to ``[0.5×, 2×]`` the sample vol ``√(252 · mean(RV_last60))``. ``nan`` if no data."""
    x = _f(rv)
    x = x[np.isfinite(x) & (x >= 0)]
    if x.size == 0:
        return math.nan
    hdays = max(int(horizon_days), 1)
    tail = x[-60:]
    sample_var = float(np.mean(tail))
    params = har_rv_fit(x)
    if params is None:
        var = _ewma_mean(x, 0.94)
    else:
        b0, bd, bw, bm = params
        hist = list(x[-22:])
        floor = 0.05 * sample_var
        fc = []
        for _ in range(hdays):
            nxt = b0 + bd * hist[-1] + bw * float(np.mean(hist[-5:])) + bm * float(np.mean(hist[-22:]))
            nxt = max(nxt, floor) if math.isfinite(nxt) else sample_var
            fc.append(nxt)
            hist.append(nxt)
        var = float(np.mean(fc))
    vol = math.sqrt(max(var, 0.0) * TRADING_DAYS)
    svol = math.sqrt(sample_var * TRADING_DAYS)
    if svol > 0:
        vol = min(max(vol, 0.5 * svol), 2.0 * svol)
    return float(vol)


def _garch_var_path(eps: NDArray[np.float64], omega: float, alpha: float, beta: float, s0: float):
    """Conditional variances σ²_t, t = 0 … n (σ²_n is the one-step-ahead forecast).
    ``σ²_t = ω + α ε²_{t−1} + β σ²_{t−1}``, ``σ²_0 = s0``; computed with a linear filter."""
    x = omega + alpha * eps * eps  # drives σ²_{t+1}
    y, _ = lfilter([1.0], [1.0, -beta], x, zi=[beta * s0])
    return np.concatenate(([s0], y))


def garch11_fit(returns: ArrayLike) -> tuple[float, float, float] | None:
    """GARCH(1,1) (Bollerslev 1986) by Gaussian MLE on demeaned returns, scipy SLSQP with
    ``ω > 0, α, β ≥ 0, α + β < 1``. Returns are standardised for conditioning and ω rescaled.
    ``None`` with < 100 valid observations or if the optimiser fails."""
    r = _f(returns)
    r = r[np.isfinite(r)]
    if r.size < 100:
        return None
    eps = r - r.mean()
    scale = float(np.std(eps))
    if not math.isfinite(scale) or scale <= 0:
        return None
    z = eps / scale
    s0 = 1.0

    def nll(p: NDArray[np.float64]) -> float:
        w, a, b = p
        s2 = _garch_var_path(z, w, a, b, s0)[:-1]
        if np.any(s2 <= 0) or not np.all(np.isfinite(s2)):
            return 1e10
        return float(0.5 * np.sum(np.log(s2) + z * z / s2))

    best = None
    for x0 in ((0.05, 0.05, 0.90), (0.2, 0.10, 0.70)):
        try:
            res = minimize(
                nll,
                np.array(x0),
                method="SLSQP",
                bounds=[(1e-6, 10.0), (0.0, 1.0), (0.0, 1.0)],
                constraints=[{"type": "ineq", "fun": lambda p: 0.999 - p[1] - p[2]}],
                options={"maxiter": 200, "ftol": 1e-9},
            )
        except (ValueError, FloatingPointError):
            continue
        if np.all(np.isfinite(res.x)) and (best is None or res.fun < best.fun):
            best = res
    if best is None:
        return None
    w, a, b = (float(v) for v in best.x)
    if a + b >= 1.0 or w <= 0:
        return None
    return w * scale * scale, max(a, 0.0), max(b, 0.0)


def garch11_forecast(params: tuple[float, float, float] | None, returns: ArrayLike, horizon_days: int) -> float:
    """Annualised vol over ``horizon_days``: average of ``σ²_{T+k} = V_L + (α+β)^{k−1}(σ²_{T+1} − V_L)``,
    ``V_L = ω/(1−α−β)``, with σ²_{T+1} filtered through ``returns``. ``nan`` if params is None."""
    if params is None:
        return math.nan
    omega, alpha, beta = params
    r = _f(returns)
    r = r[np.isfinite(r)]
    pers = alpha + beta
    if r.size < 2 or pers >= 1.0 or omega <= 0:
        return math.nan
    eps = r - r.mean()
    s2 = _garch_var_path(eps, omega, alpha, beta, float(np.var(eps)))
    s_next = float(s2[-1])
    vl = omega / (1.0 - pers)
    hdays = max(int(horizon_days), 1)
    k = np.arange(hdays, dtype=float)
    var = vl + pers**k * (s_next - vl)
    return float(math.sqrt(max(float(np.mean(var)), 0.0) * TRADING_DAYS))


def _hist(history: ArrayLike) -> NDArray[np.float64]:
    h = _f(history)
    return h[np.isfinite(h)]


def iv_rank(current: float, history: ArrayLike) -> float | None:
    """IV rank ``(cur − min)/(max − min)`` over ``history``, clipped to [0, 1]. ``None`` if < 20 obs or
    ``current`` is not finite; 0.5 if the history is flat."""
    h = _hist(history)
    if h.size < 20 or current is None or not math.isfinite(current):
        return None
    lo, hi = float(h.min()), float(h.max())
    if hi <= lo:
        return 0.5
    return float(min(max((current - lo) / (hi - lo), 0.0), 1.0))


def iv_percentile(current: float, history: ArrayLike) -> float | None:
    """Share of ``history`` strictly below ``current``. ``None`` if < 20 obs or ``current`` not finite."""
    h = _hist(history)
    if h.size < 20 or current is None or not math.isfinite(current):
        return None
    return float(np.mean(h < current))


def forecast_vol(o: ArrayLike, h: ArrayLike, l: ArrayLike, c: ArrayLike, horizon_days: int = 10) -> float:
    """Blended physical vol forecast (annualised): ``0.5·HAR-RV + 0.3·Yang-Zhang(20) + 0.2·GARCH(1,1)``,
    weights renormalised over the components that are finite and positive. ``nan`` if none are."""
    o_, h_, l_, c_ = _ohlc(o, h, l, c)
    comps: list[tuple[float, float]] = []
    rv = daily_rv_from_ohlc(o_, h_, l_, c_)
    comps.append((0.5, har_rv_forecast(rv, horizon_days)))
    comps.append((0.3, yang_zhang(o_, h_, l_, c_, 20)))
    rets = _safe_log(c_[1:], c_[:-1]) if c_.size > 1 else np.empty(0)
    rets = rets[np.isfinite(rets)]
    params = garch11_fit(rets)
    comps.append((0.2, garch11_forecast(params, rets, horizon_days)))
    good = [(w, v) for w, v in comps if v is not None and math.isfinite(v) and v > 0]
    if not good:
        return math.nan
    wsum = sum(w for w, _ in good)
    return float(sum(w * v for w, v in good) / wsum)
