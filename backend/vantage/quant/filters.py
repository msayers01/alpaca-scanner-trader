"""State-space filters."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray


def kalman_trend(
    y: ArrayLike, q_level: float | None = None, q_slope: float | None = None, r: float | None = None
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Local-linear-trend Kalman filter (Harvey 1989).

    State [level, slope]: level_t = level_{t−1} + slope_{t−1} + η, slope_t = slope_{t−1} + ζ, y_t = level_t + ε.
    Noise heuristic when not given (v = var(Δy)): r = 0.5v (observation noise), q_level = 0.5v,
    q_slope = 1e-5·v — the slope adapts slowly, so its filtered variance gives a meaningful t-statistic
    (calibrated so driftless random walks stay within |t| < 1 while persistent trends reach |t| > 2).
    Returns filtered (level, slope, slope_variance), NaN-padded where y is not finite.
    """
    a = np.asarray(y, dtype=float)
    n = len(a)
    level = np.full(n, np.nan)
    slope = np.full(n, np.nan)
    svar = np.full(n, np.nan)
    fin = np.isfinite(a)
    if fin.sum() < 3:
        return level, slope, svar
    dy = np.diff(a[fin])
    v = float(np.var(dy)) if len(dy) > 1 else 1e-8
    v = max(v, 1e-12)
    r = 0.5 * v if r is None else r
    ql = 0.5 * v if q_level is None else q_level
    qs = 1e-5 * v if q_slope is None else q_slope
    first = int(np.argmax(fin))
    lv, sl = float(a[first]), 0.0
    p00, p01, p11 = 10.0 * v, 0.0, v
    for i in range(first, n):
        # predict
        lv = lv + sl
        p00 = p00 + 2 * p01 + p11 + ql
        p01 = p01 + p11
        p11 = p11 + qs
        yi = a[i]
        if math.isfinite(yi):
            s = p00 + r
            k0, k1 = p00 / s, p01 / s
            e = yi - lv
            lv += k0 * e
            sl += k1 * e
            p11 = p11 - k1 * p01
            p00, p01 = (1 - k0) * p00, (1 - k0) * p01
        level[i], slope[i], svar[i] = lv, sl, p11
    return level, slope, svar
