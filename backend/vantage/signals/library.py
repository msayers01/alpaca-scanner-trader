"""Concrete signal models. Each returns (z, confidence, meta) or None; the engine squashes z.

All statistics are standardised by the instrument's own volatility so scores are comparable
across symbols. References are in docs/QUANT.md §6.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from vantage.quant import filters, indicators, stats
from vantage.signals.base import ModelSpec, SignalContext, SymbolData

Out = tuple[float, float, dict[str, Any]] | None


def _daily_sigma(c: np.ndarray, n: int = 60) -> float:
    r = np.diff(np.log(c[-(n + 1) :]))
    s = float(np.std(r, ddof=1)) if len(r) > 5 else math.nan
    return s if s > 1e-6 else math.nan


def tsmom(d: SymbolData, ctx: SignalContext) -> Out:
    c = d.daily["c"]
    if len(c) < 70:
        return None
    sd = _daily_sigma(c)
    if not math.isfinite(sd):
        return None
    zs, used = [], []
    skip = 5
    for n in (21, 63, 126, 252):
        if len(c) > n + skip:
            r = math.log(c[-1 - skip] / c[-1 - skip - n])
            zs.append(r / (sd * math.sqrt(n)))
            used.append(n)
    if not zs:
        return None
    z = float(np.mean(zs)) * 1.5
    return z, min(1.0, len(zs) / 4), {"horizons": used, "z_each": [round(x, 2) for x in zs]}


def trend_kalman(d: SymbolData, ctx: SignalContext) -> Out:
    c = d.daily["c"]
    if len(c) < 60:
        return None
    y = np.log(c[-250:])
    level, slope, svar = filters.kalman_trend(y)
    s, v = float(slope[-1]), float(svar[-1])
    if not (math.isfinite(s) and math.isfinite(v)) or v <= 0:
        return None
    t = s / math.sqrt(v)
    return t / 2.0, 0.7, {"slope_ann": round(s * 252, 4), "t": round(t, 2)}


def meanrev_st(d: SymbolData, ctx: SignalContext) -> Out:
    c, h, lo = d.daily["c"], d.daily["h"], d.daily["l"]
    if len(c) < 210:
        return None
    sd = _daily_sigma(c)
    sma200 = float(np.mean(c[-200:]))
    r5 = math.log(c[-1] / c[-6])
    z5 = r5 / (sd * math.sqrt(5))
    rsi2 = float(indicators.rsi(c[-60:], 2)[-1])
    ibs = float(indicators.ibs(h[-1:], lo[-1:], c[-1:])[-1])
    meta = {"rsi2": round(rsi2, 1), "ibs": round(ibs, 2), "z5": round(z5, 2), "above_sma200": bool(c[-1] > sma200)}
    if c[-1] > sma200 and z5 < 0:
        boost = 1.0 + (0.5 if rsi2 < 10 else 0) + (0.3 if ibs < 0.2 else 0)
        return -z5 * boost, 0.6, meta
    if c[-1] < sma200 and z5 > 0:
        boost = 1.0 + (0.5 if rsi2 > 90 else 0) + (0.3 if ibs > 0.8 else 0)
        return -z5 * boost * 0.7, 0.5, meta  # reversal of rallies in downtrends is weaker
    return 0.0, 0.2, meta


def breakout(d: SymbolData, ctx: SignalContext) -> Out:
    o, h, lo, c, v = (d.daily[k] for k in "ohlcv")
    if len(c) < 60:
        return None
    up, dn = indicators.donchian(h, lo, 20)
    atr = float(indicators.atr(h, lo, c, 14)[-1])
    rv = float(indicators.rel_volume(v, 20)[-1])
    if not (math.isfinite(atr) and atr > 0):
        return None
    meta = {"atr": round(atr, 4), "rel_volume": round(rv, 2) if math.isfinite(rv) else None}
    conf_vol = min(1.0, max(0.3, (rv if math.isfinite(rv) else 1) / 2))
    if c[-1] > up[-1]:
        return 1.0 + (c[-1] - up[-1]) / atr, conf_vol, meta | {"side": "up"}
    if c[-1] < dn[-1]:
        return -1.0 - (dn[-1] - c[-1]) / atr, conf_vol, meta | {"side": "down"}
    return 0.0, 0.2, meta


def rel_strength(d: SymbolData, ctx: SignalContext) -> Out:
    z = d.extra.get("xs_rs_z")
    if z is None or not math.isfinite(z):
        return None
    return float(z), 0.6, {"excess_63d": d.extra.get("xs_excess")}


def vwap_rev(d: SymbolData, ctx: SignalContext) -> Out:
    i = d.intraday
    if i is None or len(i["c"]) < 30 or not ctx.session_open:
        return None
    c, v = i["c"], i["v"]
    vw = indicators.session_vwap(c, v, np.zeros(len(c)))
    r = np.diff(np.log(c))
    s = float(np.std(r[-120:], ddof=1)) if len(r) > 20 else math.nan
    if not (math.isfinite(s) and s > 0):
        return None
    dev = math.log(c[-1] / vw[-1]) / (s * math.sqrt(30))
    hurst = stats.hurst_exponent(np.log(c[-200:]), max_lag=20) if len(c) >= 60 else 0.5
    if not math.isfinite(hurst):
        hurst = 0.5
    conf = 0.5 if hurst < 0.45 else 0.25 if hurst < 0.55 else 0.1
    return -dev, conf, {"vwap": round(float(vw[-1]), 4), "dev_z": round(dev, 2), "hurst": round(hurst, 2)}


def orb(d: SymbolData, ctx: SignalContext) -> Out:
    i = d.intraday
    if i is None or len(i["c"]) < 10 or not ctx.session_open:
        return None
    hi = float(np.max(i["h"][:5]))
    lo = float(np.min(i["l"][:5]))
    rng = hi - lo
    if rng <= 0:
        return None
    first_dir = 1 if i["c"][4] > i["o"][0] else -1
    p = float(i["c"][-1])
    rv = d.extra.get("rel_volume_today", 1.0) or 1.0
    meta = {"or_high": hi, "or_low": lo, "first_candle": first_dir, "rel_volume": round(rv, 2)}
    conf = min(1.0, 0.3 + 0.2 * rv)
    if p > hi and first_dir > 0:
        return 1.0 + (p - hi) / rng, conf, meta
    if p < lo and first_dir < 0:
        return -1.0 - (lo - p) / rng, conf, meta
    return 0.0, 0.15, meta


def options_flow(d: SymbolData, ctx: SignalContext) -> Out:
    if ctx.flow is None:
        return None
    f = ctx.flow.flow_score(d.symbol)
    if f["n"] == 0:
        return None
    sweep_tilt = (f["sweeps_bull"] - f["sweeps_bear"]) / max(1, f["sweeps_bull"] + f["sweeps_bear"])
    z = 2.5 * f["score"] + 0.5 * sweep_tilt
    return z, float(f.get("confidence", 0.3)), {k: round(v, 2) if isinstance(v, float) else v for k, v in f.items()}


def ofi(d: SymbolData, ctx: SignalContext) -> Out:
    if ctx.flow is None or not ctx.session_open:
        return None
    s, n = ctx.flow.ofi_score(d.symbol)
    if n < 20:
        return None
    return s, min(0.6, n / 300), {"n_quotes": n, "ofi": round(s, 3)}


def news_sentiment(d: SymbolData, ctx: SignalContext) -> Out:
    if ctx.news is None:
        return None
    s, n = ctx.news.sentiment_score(d.symbol, half_life_h=24.0, now=ctx.now or None)
    if n == 0:
        return None
    return 2.0 * s, 1 - math.exp(-n / 2), {"items": n, "raw": round(s, 3)}


def event_drift(d: SymbolData, ctx: SignalContext) -> Out:
    """PEAD proxy: large volume-confirmed gap within the last 40 sessions, decaying with age."""
    o, c, v = d.daily["o"], d.daily["c"], d.daily["v"]
    if len(c) < 80:
        return None
    sd = _daily_sigma(c[:-40] if len(c) > 120 else c)
    rv = indicators.rel_volume(v, 20)
    best = None
    for k in range(1, 41):
        i = len(c) - k
        gap = math.log(o[i] / c[i - 1])
        day = math.log(c[i] / c[i - 1])
        z = day / sd if sd > 0 else 0
        if abs(gap) / sd >= 2.0 and abs(z) >= 2.5 and math.isfinite(rv[i]) and rv[i] >= 2.0 and np.sign(gap) == np.sign(day):
            best = (k, z, float(rv[i]))
            break
    known = ctx.calendar.days_to_earnings(d.symbol) if ctx.calendar else None
    if best is None:
        return None
    age, z, rvol = best
    decay = math.exp(-age / 20)
    # drift after the event: continuation, but fades if price already retraced half of the move
    i = len(c) - age
    retrace = (c[-1] - c[i]) / (c[i] - c[i - 1]) if c[i] != c[i - 1] else 0
    cont = 0.4 if retrace < -0.5 else 1.0
    return np.sign(z) * min(abs(z) / 2.5, 2.0) * decay * cont, 0.5 * decay + 0.2, {
        "age_days": age, "event_z": round(z, 2), "rel_volume": round(rvol, 1), "days_to_next_earnings": known}


def vol_premium(d: SymbolData, ctx: SignalContext) -> Out:
    if not d.atm_iv or not d.vol_forecast:
        return None
    ratio = d.atm_iv / d.vol_forecast
    # positive score = implied vol cheap relative to forecast (good for long premium)
    return -3.0 * math.log(ratio), 0.7, {"iv": round(d.atm_iv, 4), "rv_forecast": round(d.vol_forecast, 4),
                                         "iv_rv": round(ratio, 3)}


MODELS: list[ModelSpec] = [
    ModelSpec("tsmom", "Time-series momentum", 20, "daily", True, 1.0,
              "Vol-scaled 1/3/6/12-month returns skipping the last week (Moskowitz-Ooi-Pedersen).", tsmom),
    ModelSpec("trend_kalman", "Kalman trend", 10, "daily", True, 0.8,
              "Local-linear-trend Kalman filter slope t-stat on log prices.", trend_kalman),
    ModelSpec("meanrev_st", "Short-term reversal", 3, "daily", True, 0.7,
              "5-day return reversal gated by the 200-day trend, boosted by RSI(2)/IBS extremes.", meanrev_st),
    ModelSpec("breakout", "Donchian breakout", 5, "daily", True, 0.6,
              "Close beyond the prior 20-day range, in ATRs, confirmed by relative volume.", breakout),
    ModelSpec("rel_strength", "Relative strength", 20, "daily", True, 0.7,
              "Cross-sectional z-score of 3-month return in excess of SPY.", rel_strength),
    ModelSpec("vwap_rev", "VWAP reversion", 0.5, "intraday", True, 0.4,
              "Intraday deviation from session VWAP, weighted by anti-persistence (Hurst < 0.5).", vwap_rev),
    ModelSpec("orb", "Opening-range breakout", 1, "intraday", True, 0.5,
              "Break of the first 5-minute range in the direction of the first candle, scaled by relative volume.", orb),
    ModelSpec("options_flow", "Options flow", 2, "event", True, 0.8,
              "Decayed aggressive premium imbalance (calls bought + puts sold vs puts bought + calls sold), sweep-weighted.",
              options_flow),
    ModelSpec("ofi", "Order-flow imbalance", 0.1, "intraday", True, 0.3,
              "Cont-Kukanov-Stoikov OFI over 5 minutes normalised by depth.", ofi),
    ModelSpec("news_sentiment", "News sentiment", 2, "event", True, 0.8,
              "Decayed sentiment of recent news (LLM-scored when available, lexicon otherwise).", news_sentiment),
    ModelSpec("event_drift", "Event drift (PEAD)", 20, "daily", True, 0.7,
              "Continuation after a volume-confirmed gap (earnings/news event) in the last 40 sessions.", event_drift),
    ModelSpec("vol_premium", "Volatility premium", 10, "daily", False, 0.0,
              "ln(implied / forecast vol); positive = options cheap. Non-directional, used by the scanner.", vol_premium),
]
MODEL_BY_ID = {m.id: m for m in MODELS}
