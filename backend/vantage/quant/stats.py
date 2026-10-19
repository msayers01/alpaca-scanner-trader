"""Statistics: z-scores, persistence (Hurst, OU half-life), performance ratios, PSR/DSR, IC.

See docs/QUANT.md §3. All functions are pure and NaN-tolerant.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import stats as sps

EULER_GAMMA = 0.5772156649015329


def _f(x: ArrayLike) -> NDArray[np.float64]:
    a = np.asarray(x, dtype=float).ravel()
    return a[np.isfinite(a)]


def zscore(x: ArrayLike, window: int) -> float:
    """z of the last value against the trailing ``window`` observations (including it)."""
    a = _f(x)[-window:]
    if len(a) < 3:
        return math.nan
    sd = float(np.std(a, ddof=1))
    return (float(a[-1]) - float(np.mean(a))) / sd if sd > 0 else 0.0


def rolling_zscore(x: ArrayLike, window: int) -> NDArray[np.float64]:
    a = np.asarray(x, dtype=float)
    out = np.full(len(a), np.nan)
    if window < 3 or len(a) < window:
        return out
    w = np.lib.stride_tricks.sliding_window_view(a, window)
    mu = w.mean(axis=1)
    sd = w.std(axis=1, ddof=1)
    with np.errstate(all="ignore"):
        out[window - 1 :] = np.where(sd > 0, (a[window - 1 :] - mu) / sd, 0.0)
    return out


def hurst_exponent(x: ArrayLike, max_lag: int = 50) -> float:
    """Hurst exponent from the scaling of lagged-difference dispersion: std(x_{t+τ} − x_t) ∝ τ^H.

    H ≈ 0.5 random walk, < 0.5 mean-reverting, > 0.5 trending. ``x`` is a level series (e.g. log price).
    """
    a = _f(x)
    max_lag = min(max_lag, len(a) // 2)
    if max_lag < 4:
        return math.nan
    lags = np.arange(2, max_lag + 1)
    tau = np.array([np.std(a[lag:] - a[:-lag]) for lag in lags])
    ok = tau > 0
    if ok.sum() < 3:
        return math.nan
    slope = np.polyfit(np.log(lags[ok]), np.log(tau[ok]), 1)[0]
    return float(slope)


def ou_half_life(x: ArrayLike) -> float:
    """Ornstein-Uhlenbeck half-life from the AR(1) regression Δx_t = a + b·x_{t−1}: −ln 2 / b."""
    a = _f(x)
    if len(a) < 10:
        return math.nan
    y = np.diff(a)
    X = np.column_stack([np.ones(len(y)), a[:-1]])
    b = np.linalg.lstsq(X, y, rcond=None)[0][1]
    return float(-math.log(2) / b) if b < 0 else math.inf


def sharpe(returns: ArrayLike, periods: int = 252) -> float:
    r = _f(returns)
    if len(r) < 2:
        return 0.0
    sd = float(np.std(r, ddof=1))
    return float(np.mean(r) / sd * math.sqrt(periods)) if sd > 0 else 0.0


def sortino(returns: ArrayLike, periods: int = 252) -> float:
    r = _f(returns)
    if len(r) < 2:
        return 0.0
    downside = np.minimum(r, 0.0)
    dd = math.sqrt(float(np.mean(downside**2)))
    return float(np.mean(r) / dd * math.sqrt(periods)) if dd > 0 else 0.0


def max_drawdown(equity: ArrayLike) -> float:
    e = _f(equity)
    if len(e) < 2:
        return 0.0
    peak = np.maximum.accumulate(e)
    with np.errstate(all="ignore"):
        dd = np.where(peak > 0, 1 - e / peak, 0.0)
    return float(np.max(dd))


def cagr(equity: ArrayLike, periods_per_year: int = 252) -> float:
    e = _f(equity)
    if len(e) < 2 or e[0] <= 0 or e[-1] <= 0:
        return 0.0
    years = (len(e) - 1) / periods_per_year
    return float((e[-1] / e[0]) ** (1 / years) - 1) if years > 0 else 0.0


def calmar(equity: ArrayLike, periods_per_year: int = 252) -> float:
    mdd = max_drawdown(equity)
    return cagr(equity, periods_per_year) / mdd if mdd > 0 else 0.0


def sharpe_stats(returns: ArrayLike) -> dict[str, float]:
    """Per-period Sharpe ratio, sample size, skewness and (non-excess) kurtosis of a return series."""
    r = _f(returns)
    n = len(r)
    if n < 3 or np.std(r) == 0:
        return {"sr": 0.0, "n": float(n), "skew": 0.0, "kurt": 3.0}
    return {
        "sr": float(np.mean(r) / np.std(r, ddof=1)),
        "n": float(n),
        "skew": float(sps.skew(r)),
        "kurt": float(sps.kurtosis(r, fisher=False)),
    }


def probabilistic_sharpe_ratio(sr: float, n: float, skew: float, kurt: float, sr_benchmark: float = 0.0) -> float:
    """Bailey & López de Prado (2012): P(true SR > SR*) given a per-period SR estimated on n observations.

    PSR = Φ( (SR − SR*)·√(n−1) / √(1 − γ3·SR + (γ4 − 1)/4·SR²) ), γ4 non-excess kurtosis.
    """
    if n < 2 or not all(math.isfinite(v) for v in (sr, skew, kurt, sr_benchmark)):
        return math.nan
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return math.nan
    return float(sps.norm.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom)))


def deflated_sharpe_ratio(sr: float, n: float, skew: float, kurt: float, n_trials: int, sr_var: float) -> float:
    """Bailey & López de Prado (2014): PSR against the expected maximum SR of ``n_trials`` unskilled trials.

    SR* = √V · ((1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))), γ = Euler-Mascheroni, V = variance of trial SRs.
    """
    if n_trials <= 1:
        return probabilistic_sharpe_ratio(sr, n, skew, kurt, 0.0)
    if sr_var <= 0 or not math.isfinite(sr_var):
        return math.nan
    N = float(n_trials)
    sr_star = math.sqrt(sr_var) * ((1 - EULER_GAMMA) * sps.norm.ppf(1 - 1 / N)
                                   + EULER_GAMMA * sps.norm.ppf(1 - 1 / (N * math.e)))
    return probabilistic_sharpe_ratio(sr, n, skew, kurt, float(sr_star))


def spearman_ic(scores: ArrayLike, fwd_returns: ArrayLike) -> float:
    s = np.asarray(scores, dtype=float).ravel()
    f = np.asarray(fwd_returns, dtype=float).ravel()
    ok = np.isfinite(s) & np.isfinite(f)
    if ok.sum() < 5 or np.ptp(s[ok]) == 0 or np.ptp(f[ok]) == 0:
        return math.nan
    return float(sps.spearmanr(s[ok], f[ok]).statistic)


def profit_factor(pnls: ArrayLike) -> float:
    p = _f(pnls)
    gains = float(p[p > 0].sum())
    losses = float(-p[p < 0].sum())
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def win_rate(pnls: ArrayLike) -> float:
    p = _f(pnls)
    return float(np.mean(p > 0)) if len(p) else 0.0
