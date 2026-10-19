"""Market microstructure: trade classification, order-flow imbalance, VPIN, Kyle's lambda."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.special import ndtr


def quote_rule(price: ArrayLike, bid: ArrayLike, ask: ArrayLike) -> NDArray[np.int64]:
    """+1 buyer-initiated (at/above ask or above mid), −1 seller-initiated, 0 at the midpoint."""
    p, b, a = (np.asarray(x, dtype=float) for x in (price, bid, ask))
    mid = (b + a) / 2
    out = np.where(p >= a, 1, np.where(p <= b, -1, np.sign(p - mid)))
    return out.astype(np.int64)


def tick_rule(prices: ArrayLike) -> NDArray[np.int64]:
    """Direction of the last non-zero price change, carried forward (first trade = 0)."""
    p = np.asarray(prices, dtype=float)
    out = np.zeros(len(p), dtype=np.int64)
    last = 0
    for i in range(1, len(p)):
        d = p[i] - p[i - 1]
        if d > 0:
            last = 1
        elif d < 0:
            last = -1
        out[i] = last
    return out


def lee_ready(price: ArrayLike, bid: ArrayLike, ask: ArrayLike) -> NDArray[np.int64]:
    """Lee & Ready (1991): quote rule, midpoint trades resolved by the tick rule."""
    q = quote_rule(price, bid, ask)
    t = tick_rule(price)
    return np.where(q != 0, q, t).astype(np.int64)


def ofi_increment(pb, pbs, pa, pas, b, bs, a, as_):  # type: ignore[no-untyped-def]
    """Cont, Kukanov & Stoikov (2014) order-flow imbalance contribution e_n between consecutive quotes:

    e = 1[b ≥ pb]·bs − 1[b ≤ pb]·pbs − 1[a ≤ pa]·as + 1[a ≥ pa]·pas.
    """
    pb, pbs, pa, pas, b, bs, a, as_ = (np.asarray(x, dtype=float) for x in (pb, pbs, pa, pas, b, bs, a, as_))
    e = (b >= pb) * bs - (b <= pb) * pbs - (a <= pa) * as_ + (a >= pa) * pas
    return float(e) if e.ndim == 0 else e


def bulk_volume_buy_fraction(dp: ArrayLike, sigma: float):  # type: ignore[no-untyped-def]
    """Bulk volume classification (Easley, López de Prado & O'Hara 2012): buy share = Φ(Δp/σ)."""
    d = np.asarray(dp, dtype=float)
    if sigma <= 0:
        out = np.where(d > 0, 1.0, np.where(d < 0, 0.0, 0.5))
    else:
        out = ndtr(d / sigma)
    return float(out) if np.ndim(out) == 0 else out


def vpin(buy_vol: ArrayLike, sell_vol: ArrayLike) -> float:
    """Volume-synchronised probability of informed trading: mean |B − S| / (B + S) over volume buckets."""
    b = np.asarray(buy_vol, dtype=float)
    s = np.asarray(sell_vol, dtype=float)
    tot = b + s
    ok = tot > 0
    if not ok.any():
        return math.nan
    return float(np.mean(np.abs(b[ok] - s[ok]) / tot[ok]))


def kyle_lambda(price_changes: ArrayLike, signed_volume: ArrayLike) -> float:
    """Price impact per unit signed volume: OLS slope of Δp on signed volume (with intercept)."""
    y = np.asarray(price_changes, dtype=float)
    x = np.asarray(signed_volume, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 or np.var(x[ok]) == 0:
        return math.nan
    X = np.column_stack([np.ones(ok.sum()), x[ok]])
    return float(np.linalg.lstsq(X, y[ok], rcond=None)[0][1])
