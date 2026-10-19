"""Technical indicators. All return arrays the same length as the input, NaN where undefined."""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

Arr = NDArray[np.float64]


def _f(x: ArrayLike) -> Arr:
    return np.asarray(x, dtype=float)


def sma(x: ArrayLike, n: int) -> Arr:
    a = _f(x)
    out = np.full(len(a), np.nan)
    if n <= 0 or len(a) < n:
        return out
    c = np.cumsum(np.insert(a, 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def ema(x: ArrayLike, n: int) -> Arr:
    """Exponential moving average, α = 2/(n+1), seeded with the SMA of the first n values."""
    a = _f(x)
    out = np.full(len(a), np.nan)
    if n <= 0 or len(a) < n:
        return out
    alpha = 2.0 / (n + 1)
    v = float(np.mean(a[:n]))
    out[n - 1] = v
    for i in range(n, len(a)):
        v = alpha * a[i] + (1 - alpha) * v
        out[i] = v
    return out


def _wilder(x: Arr, n: int, start: int) -> Arr:
    """Wilder smoothing (α = 1/n) seeded with the mean of x[start:start+n]."""
    out = np.full(len(x), np.nan)
    if len(x) < start + n:
        return out
    v = float(np.mean(x[start : start + n]))
    out[start + n - 1] = v
    for i in range(start + n, len(x)):
        v = (v * (n - 1) + x[i]) / n
        out[i] = v
    return out


def rsi(c: ArrayLike, n: int = 14) -> Arr:
    """Wilder's RSI."""
    a = _f(c)
    out = np.full(len(a), np.nan)
    if len(a) <= n:
        return out
    d = np.diff(a, prepend=np.nan)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = _wilder(up, n, 1)
    ad = _wilder(dn, n, 1)
    with np.errstate(all="ignore"):
        rs = au / ad
        out = np.where(ad == 0, np.where(au == 0, 50.0, 100.0), 100 - 100 / (1 + rs))
    out[: n] = np.nan
    return out


def true_range(h: ArrayLike, l: ArrayLike, c: ArrayLike) -> Arr:
    h, l, c = _f(h), _f(l), _f(c)
    pc = np.roll(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    if len(tr):
        tr[0] = h[0] - l[0]
    return tr


def atr(h: ArrayLike, l: ArrayLike, c: ArrayLike, n: int = 14) -> Arr:
    return _wilder(true_range(h, l, c), n, 0)


def bollinger(c: ArrayLike, n: int = 20, k: float = 2.0) -> tuple[Arr, Arr, Arr]:
    a = _f(c)
    mid = sma(a, n)
    sd = np.full(len(a), np.nan)
    if len(a) >= n:
        w = np.lib.stride_tricks.sliding_window_view(a, n)
        sd[n - 1 :] = w.std(axis=1, ddof=0)
    return mid, mid + k * sd, mid - k * sd


def ibs(h: ArrayLike, l: ArrayLike, c: ArrayLike) -> Arr:
    """Internal bar strength (close − low)/(high − low); 0.5 for zero-range bars."""
    h, l, c = _f(h), _f(l), _f(c)
    rng = h - l
    with np.errstate(all="ignore"):
        return np.where(rng > 0, (c - l) / rng, 0.5)


def donchian(h: ArrayLike, l: ArrayLike, n: int = 20) -> tuple[Arr, Arr]:
    """Highest high / lowest low of the PRIOR n bars (excluding the current bar)."""
    h, l = _f(h), _f(l)
    up = np.full(len(h), np.nan)
    dn = np.full(len(h), np.nan)
    if len(h) <= n:
        return up, dn
    wh = np.lib.stride_tricks.sliding_window_view(h, n)
    wl = np.lib.stride_tricks.sliding_window_view(l, n)
    up[n:] = wh.max(axis=1)[:-1]
    dn[n:] = wl.min(axis=1)[:-1]
    return up, dn


def session_vwap(price: ArrayLike, volume: ArrayLike, session_id: ArrayLike) -> Arr:
    p, v = _f(price), _f(volume)
    sid = np.asarray(session_id)
    out = np.full(len(p), np.nan)
    pv = vv = 0.0
    for i in range(len(p)):
        if i > 0 and sid[i] != sid[i - 1]:
            pv = vv = 0.0
        pv += p[i] * v[i]
        vv += v[i]
        out[i] = pv / vv if vv > 0 else p[i]
    return out


def rel_volume(v: ArrayLike, n: int = 20) -> Arr:
    """Volume divided by the mean of the prior n bars."""
    a = _f(v)
    out = np.full(len(a), np.nan)
    if len(a) <= n:
        return out
    prior = sma(a, n)[n - 1 : -1]
    with np.errstate(all="ignore"):
        out[n:] = np.where(prior > 0, a[n:] / prior, np.nan)
    return out


def returns(c: ArrayLike) -> Arr:
    a = _f(c)
    out = np.full(len(a), np.nan)
    with np.errstate(all="ignore"):
        out[1:] = a[1:] / a[:-1] - 1
    return out


def log_returns(c: ArrayLike) -> Arr:
    a = _f(c)
    out = np.full(len(a), np.nan)
    with np.errstate(all="ignore"):
        out[1:] = np.log(a[1:] / a[:-1])
    return out
