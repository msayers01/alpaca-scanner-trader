"""Pure maths for the intel signals: social polarity/attention, VIX regime, prediction-market tilt and event risk.

No I/O, no globals (docs/QUANT.md §6.6). Every function is a closed form or a bounded heuristic that is unit-tested.
"""

from __future__ import annotations

import math

import numpy as np


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def shrunk_polarity(bull: float, bear: float, prior: float = 4.0) -> float:
    """(bull - bear) / (bull + bear + prior): thin samples are shrunk toward neutral. Always in (-1, 1)."""
    bull, bear = max(bull, 0.0), max(bear, 0.0)
    return (bull - bear) / (bull + bear + max(prior, 1e-9))


def attention_ratio(now: float, base: float, k: float = 3.0) -> float:
    """Smoothed log-ratio of current mentions to their baseline: 0 = normal, ln 2 = doubled, negative = fading."""
    return math.log((max(now, 0.0) + k) / (max(base, 0.0) + k))


def decay_weight(age_ms: float, half_life_h: float) -> float:
    """Exponential half-life weight; negative ages (clock skew) count as fresh."""
    if half_life_h <= 0:
        return 0.0
    return math.exp(-math.log(2) * max(age_ms, 0.0) / (half_life_h * 3_600_000))


def engagement_weight(engagement: float) -> float:
    """Log-damped weight so one viral post cannot drown out many ordinary ones: 1 .. ~4."""
    return 1.0 + math.log1p(max(engagement, 0.0)) / 3.0


def social_score(polarity: float, attn: float, n_eff: float, crowd_ratio: float = 2.0) -> tuple[float, float, bool]:
    """Combine polarity and attention into a (z, confidence, crowded) triple for the signal engine.

    Attention gates the polarity (nobody is talking -> no signal). When the crowd is both extremely loud
    (attention log-ratio above ``ln(crowd_ratio) + 1``) and extremely one-sided (|polarity| > 0.6), retail positioning is
    stretched and the score is shrunk by 60 % — retail extremes mean-revert more often than they trend.
    """
    if attn <= -0.3:  # fading interest
        gate = 0.25
    else:
        gate = 0.5 + 0.5 * clip(attn / math.log(max(crowd_ratio, 1.01)), 0.0, 1.0)
    z = 2.0 * polarity * gate
    crowded = attn > math.log(crowd_ratio) + 1.0 and abs(polarity) > 0.6
    if crowded:
        z *= 0.4
    conf = 1.0 - math.exp(-max(n_eff, 0.0) / 8.0)
    return z, conf, crowded


def percentile_rank(x: float, hist: np.ndarray) -> float:
    """Fraction of ``hist`` at or below ``x`` (0..1)."""
    h = hist[np.isfinite(hist)]
    if len(h) == 0:
        return 0.5
    return float(np.mean(h <= x))


def vix_regime(
    vix: float,
    prev_close: float | None,
    hist: np.ndarray,
    vix9d: float | None = None,
    vix3m: float | None = None,
    vvix: float | None = None,
    skew: float | None = None,
) -> dict[str, float | str | None]:
    """Classify the volatility regime and produce a risk-on score in [-1, 1] (positive = bullish for equities).

    ``hist`` is the daily VIX closes (oldest first, excluding today). Components:
    * ``z_chg``  — one-day log change in VIX in units of its own daily-change volatility (spikes are risk-off);
    * ``term``   — VIX3M / VIX - 1 (contango > 0 is calm; backwardation < 0 is stress);
    * ``pct``    — percentile of the VIX level over the history window.
    risk_on = 0.5 * (-z_chg/3) + 0.35 * clip(term / 0.10) + 0.15 * (1 - 2 * pct), each clipped to [-1, 1].
    """
    h = hist[np.isfinite(hist) & (hist > 0)]
    pct = percentile_rank(vix, h) if len(h) >= 20 else 0.5
    ch = np.diff(np.log(h)) if len(h) > 21 else np.array([])
    sd = float(np.std(ch, ddof=1)) if len(ch) > 20 else 0.07
    sd = max(sd, 0.02)
    prev = prev_close if prev_close and prev_close > 0 else (float(h[-1]) if len(h) else None)
    chg1 = math.log(vix / prev) if prev else 0.0
    z_chg = chg1 / sd
    chg5 = math.log(vix / float(h[-5])) if len(h) >= 5 else 0.0
    term = (vix3m / vix - 1.0) if vix3m and vix > 0 else None
    near = (vix / vix9d - 1.0) if vix9d and vix9d > 0 else None  # > 0 means the front of the curve is richer
    inverted = (term is not None and term < -0.02) or (near is not None and near < -0.05)
    if vix >= 35 or (z_chg >= 3 and vix >= 25):
        label = "panic"
    elif vix >= 25 or pct >= 0.85 or inverted:
        label = "stressed"
    elif pct >= 0.6 or vix >= 20:
        label = "elevated"
    elif pct < 0.25 and vix < 16:
        label = "calm"
    else:
        label = "normal"
    a = -clip(z_chg, -3.0, 3.0) / 3.0
    b = clip((term if term is not None else 0.05) / 0.10, -1.0, 1.0)
    c = 1.0 - 2.0 * pct
    risk_on = clip(0.5 * a + 0.35 * b + 0.15 * c, -1.0, 1.0)
    return {
        "vix": vix, "prev_close": prev, "chg_1d": chg1, "z_chg": z_chg, "chg_5d": chg5, "pct": pct, "term": term,
        "near": near, "vvix": vvix, "skew": skew, "label": label, "risk_on": risk_on, "inverted": bool(inverted),
    }


def binary_uncertainty(p: float) -> float:
    """4 p (1 - p): 0 when the outcome is certain, 1 at a coin flip."""
    p = clip(p, 0.0, 1.0)
    return 4.0 * p * (1.0 - p)


def event_risk(markets: list[tuple[float, float, float, float]]) -> float:
    """Aggregate prediction-market evidence of an upcoming volatility event, in [0, 1].

    Each tuple is ``(p, dp_24h, weight, days_to_resolution)``. A market contributes when the outcome is genuinely
    uncertain AND/OR its probability has just been repriced, and counts more the sooner it resolves.
    risk = 1 - exp(-(sum of the top-10 contributions) / 4): one imminent coin-flip ~ 0.14, ten of them ~ 0.78.
    """
    contrib = []
    for p, dp, w, days in markets:
        prox = clip(math.exp(-max(days, 0.0) / 30.0), 0.1, 1.0)
        contrib.append(max(w, 0.0) * (0.6 * binary_uncertainty(p) + 0.4 * min(1.0, abs(dp) / 0.15)) * prox)
    # only the ten most event-like markets count, so a long tail of near-duplicate markets cannot saturate the score
    return 1.0 - math.exp(-sum(sorted(contrib, reverse=True)[:10]) / 4.0)


def macro_tilt(markets: list[tuple[float, float, float]]) -> float:
    """Net equity tilt from repricing of macro prediction markets, in (-1, 1).

    Each tuple is ``(polarity, dp_24h, weight)`` where polarity = +1 if a higher 'yes' probability is bullish for
    equities, -1 if bearish. tilt = tanh(sum(w * polarity * dp) / 0.08): a 10-pp repricing of one full-weight bearish
    market gives about -0.85.
    """
    s = sum(w * pol * dp for pol, dp, w in markets)
    return math.tanh(s / 0.08)


def decayed_tilt(items: list[tuple[float, float, float]], half_life_h: float, scale: float = 1.0) -> tuple[float, float]:
    """Decay-weighted sum of ``(value, age_ms, weight)`` -> (tanh(sum/scale), effective count)."""
    s = 0.0
    n = 0.0
    for v, age, w in items:
        d = decay_weight(age, half_life_h)
        s += v * w * d
        n += w * d
    return math.tanh(s / max(scale, 1e-9)), n
