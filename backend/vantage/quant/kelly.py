"""Growth-optimal (Kelly) bet sizing. See docs/QUANT.md §4.

For a discrete payoff distribution {R_j, p_j} (return on the stake, R_j ≥ −1) the Kelly fraction maximises
G(f) = Σ p_j log(1 + f R_j). G is concave, so G'(f) = Σ p_j R_j / (1 + f R_j) is decreasing and the optimum
is found by bisection on G' (robust, no derivatives of G' needed).
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike


def kelly_binary(p: float, win: float, loss: float) -> float:
    """f* = p/loss − (1−p)/win for a bet that gains ``win`` or loses ``loss`` (fractions of stake)."""
    if win <= 0 or loss <= 0 or not 0 <= p <= 1:
        return 0.0
    return max(0.0, p / loss - (1 - p) / win)


def kelly_gaussian(mu: float, sigma: float) -> float:
    """Continuous-time Kelly leverage μ/σ² (μ, σ over the same period)."""
    if sigma <= 0 or not math.isfinite(mu):
        return 0.0
    return mu / sigma**2


def _bounds(R: np.ndarray, max_f: float) -> float:
    worst = float(np.min(R))
    hi = max_f
    if worst < 0:
        hi = min(hi, 0.999 / -worst)
    return hi


def kelly_discrete(returns: ArrayLike, probs: ArrayLike, max_f: float = 1.0) -> float:
    R = np.asarray(returns, dtype=float).ravel()
    p = np.asarray(probs, dtype=float).ravel()
    ok = np.isfinite(R) & np.isfinite(p)
    R, p = R[ok], p[ok]
    if len(R) == 0 or p.sum() <= 0:
        return 0.0
    p = p / p.sum()
    if float(np.dot(p, R)) <= 0:
        return 0.0
    hi = _bounds(R, max_f)

    def g(f: float) -> float:
        return float(np.dot(p, R / (1 + f * R)))

    if g(hi) >= 0:
        return hi
    lo = 0.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if g(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def kelly_discrete_batch(returns_2d: ArrayLike, probs: ArrayLike, max_f: float = 1.0) -> np.ndarray:
    """Row-wise Kelly for an (M, n) matrix of outcomes with shared (n,) or per-row (M, n) probabilities."""
    R = np.asarray(returns_2d, dtype=float)
    if R.ndim == 1:
        R = R[None, :]
    P = np.broadcast_to(np.asarray(probs, dtype=float), R.shape).copy()
    bad = ~np.isfinite(R) | ~np.isfinite(P)
    R = np.where(bad, 0.0, R)
    P = np.where(bad, 0.0, P)
    s = P.sum(axis=1, keepdims=True)
    P = np.divide(P, s, out=np.zeros_like(P), where=s > 0)
    M = R.shape[0]
    out = np.zeros(M)
    ev = (P * R).sum(axis=1)
    worst = R.min(axis=1)
    hi = np.where(worst < 0, np.minimum(max_f, 0.999 / np.maximum(-worst, 1e-12)), max_f)
    active = ev > 0

    def g(f: np.ndarray) -> np.ndarray:
        return (P * R / (1 + f[:, None] * R)).sum(axis=1)

    at_hi = active & (g(hi) >= 0)
    out[at_hi] = hi[at_hi]
    todo = active & ~at_hi
    if todo.any():
        lo = np.zeros(M)
        h = hi.copy()
        for _ in range(60):
            mid = 0.5 * (lo + h)
            pos = g(mid) > 0
            lo = np.where(todo & pos, mid, lo)
            h = np.where(todo & ~pos, mid, h)
        out[todo] = 0.5 * (lo + h)[todo]
    return out
