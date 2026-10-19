"""Black-Scholes-Merton pricing, greeks, implied vol and the Bjerksund-Stensland (2002) American
approximation. See docs/QUANT.md §1.

Every function broadcasts over all of its arguments (numpy rules). All-scalar input returns a Python
``float``; otherwise an ``ndarray`` of the broadcast shape is returned. ``is_call`` may be a bool or a
bool array.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.special import ndtr

FloatOrArray = float | NDArray[np.float64]

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_IV_LO = 1e-4
_IV_HI = 5.0

# 20-point Gauss-Legendre rule on [-1, 1] (immutable constant) for the bivariate normal CDF.
_GL_X, _GL_W = np.polynomial.legendre.leggauss(20)
_GL_X.flags.writeable = False
_GL_W.flags.writeable = False


def _out(x: Any) -> FloatOrArray:
    a = np.asarray(x, dtype=float)
    return float(a) if a.ndim == 0 else a


def _f(x: ArrayLike) -> NDArray[np.float64]:
    return np.asarray(x, dtype=float)


def _npdf(x: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.exp(-0.5 * x * x) / _SQRT_2PI


def d1_d2(
    S: ArrayLike, K: ArrayLike, T: ArrayLike, r: ArrayLike, q: ArrayLike, sigma: ArrayLike
) -> tuple[FloatOrArray, FloatOrArray]:
    """BSM ``d1 = [ln(S/K) + (r − q + σ²/2)T]/(σ√T)``, ``d2 = d1 − σ√T``. ``nan`` where T ≤ 0 or σ ≤ 0."""
    S, K, T, r, q, sigma = (_f(a) for a in (S, K, T, r, q, sigma))
    with np.errstate(all="ignore"):
        live = (T > 0) & (sigma > 0)
        vs = np.where(live, sigma * np.sqrt(np.where(T > 0, T, 0.0)), np.nan)
        d1 = (np.log(S / K) + (r - q) * T) / vs + 0.5 * vs
        d2 = d1 - vs
    return _out(d1), _out(d2)


def _bs_core(S, K, T, r, q, sigma):
    """Shared BSM internals. Returns dict of broadcast arrays (no degenerate-case handling)."""
    with np.errstate(all="ignore"):
        Tp = np.where(T > 0, T, 0.0)
        dq = np.exp(-q * Tp)
        dr = np.exp(-r * Tp)
        F = S * dq  # discounted forward  S e^{-qT}
        Kd = K * dr  # discounted strike   K e^{-rT}
        live = (T > 0) & (sigma > 0)
        vs = np.where(live, sigma * np.sqrt(Tp), 1.0)
        d1 = np.log(F / Kd) / vs + 0.5 * vs
        d2 = d1 - vs
    return {"Tp": Tp, "dq": dq, "dr": dr, "F": F, "Kd": Kd, "live": live, "vs": vs, "d1": d1, "d2": d2}


def bs_price(
    S: ArrayLike, K: ArrayLike, T: ArrayLike, r: ArrayLike, q: ArrayLike, sigma: ArrayLike, is_call: ArrayLike
) -> FloatOrArray:
    """Black-Scholes-Merton (1973) price with continuous dividend yield ``q``.

    call ``= S e^{−qT} N(d1) − K e^{−rT} N(d2)``; put ``= K e^{−rT} N(−d2) − S e^{−qT} N(−d1)`` (parity).
    ``T ≤ 0`` → intrinsic ``max(±(S−K), 0)``; ``σ ≤ 0`` (T > 0) → discounted forward intrinsic.
    """
    S, K, T, r, q, sigma = (_f(a) for a in (S, K, T, r, q, sigma))
    call = np.asarray(is_call, dtype=bool)
    c = _bs_core(S, K, T, r, q, sigma)
    F, Kd, d1, d2 = c["F"], c["Kd"], c["d1"], c["d2"]
    with np.errstate(all="ignore"):
        cp = F * ndtr(d1) - Kd * ndtr(d2)
        pp = Kd * ndtr(-d2) - F * ndtr(-d1)
        price = np.where(call, cp, pp)
        intr_now = np.where(call, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0))
        intr_fwd = np.where(call, np.maximum(F - Kd, 0.0), np.maximum(Kd - F, 0.0))
        out = np.where(c["live"], price, np.where(T > 0, intr_fwd, intr_now))
        bad = np.isnan(S + K + T + r + q + sigma)
        out = np.where(bad, np.nan, np.maximum(out, 0.0))
    return _out(out)


def bs_greeks(
    S: ArrayLike, K: ArrayLike, T: ArrayLike, r: ArrayLike, q: ArrayLike, sigma: ArrayLike, is_call: ArrayLike
) -> dict[str, FloatOrArray]:
    """BSM greeks (Hull, ch. 19). Units: ``delta``, ``gamma`` raw; ``vega`` per 1 vol point (raw/100);
    ``theta`` per calendar day (raw/365, negative = decay); ``rho`` per 1 % rate change (raw/100).

    Degenerate cases (T ≤ 0 or σ ≤ 0): delta is the (discounted) exercise indicator, gamma = vega = 0,
    theta/rho are those of the discounted forward intrinsic (0 when T ≤ 0).
    """
    S, K, T, r, q, sigma = (_f(a) for a in (S, K, T, r, q, sigma))
    call = np.asarray(is_call, dtype=bool)
    c = _bs_core(S, K, T, r, q, sigma)
    Tp, dq, dr, F, Kd, live, vs, d1, d2 = (c[k] for k in ("Tp", "dq", "dr", "F", "Kd", "live", "vs", "d1", "d2"))
    with np.errstate(all="ignore"):
        sqT = np.sqrt(Tp)
        nd1 = _npdf(d1)
        Nd1, Nd2, Nmd1, Nmd2 = ndtr(d1), ndtr(d2), ndtr(-d1), ndtr(-d2)
        delta = np.where(call, dq * Nd1, -dq * Nmd1)
        gamma = dq * nd1 / (S * vs)
        vega = F * nd1 * sqT
        decay = -F * nd1 * sigma / (2.0 * sqT)
        theta = np.where(call, decay - r * Kd * Nd2 + q * F * Nd1, decay + r * Kd * Nmd2 - q * F * Nmd1)
        rho = np.where(call, K * Tp * dr * Nd2, -K * Tp * dr * Nmd2)

        # Degenerate: value = max(±(F − Kd), 0) (F = S, Kd = K when T ≤ 0).
        itm = np.where(call, F > Kd, Kd > F)
        sgn = np.where(call, 1.0, -1.0)
        pos_t = T > 0
        d_delta = np.where(itm, sgn * dq, 0.0)
        d_theta = np.where(itm & pos_t, sgn * (q * F - r * Kd), 0.0)
        d_rho = np.where(itm & pos_t, sgn * K * Tp * dr, 0.0)

        delta = np.where(live, delta, d_delta)
        gamma = np.where(live, gamma, 0.0)
        vega = np.where(live, vega, 0.0)
        theta = np.where(live, theta, d_theta)
        rho = np.where(live, rho, d_rho)
        bad = np.isnan(S + K + T + r + q + sigma)
        res = {
            "delta": delta,
            "gamma": gamma,
            "vega": vega / 100.0,
            "theta": theta / 365.0,
            "rho": rho / 100.0,
        }
        return {k: _out(np.where(bad, np.nan, v)) for k, v in res.items()}


def _otm_price_vega(F, Kd, T, sig, call):
    """BSM price and raw vega in forward/discounted-strike form (no edge handling; T, sig > 0)."""
    vs = sig * np.sqrt(T)
    d1 = np.log(F / Kd) / vs + 0.5 * vs
    d2 = d1 - vs
    price = np.where(call, F * ndtr(d1) - Kd * ndtr(d2), Kd * ndtr(-d2) - F * ndtr(-d1))
    vega = F * _npdf(d1) * np.sqrt(T)
    return price, vega


def implied_vol(
    price: ArrayLike,
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    q: ArrayLike,
    is_call: ArrayLike,
    tol: float = 1e-7,
    max_iter: int = 60,
) -> FloatOrArray:
    """Implied volatility by safeguarded Newton (on vega) with bisection fallback in ``[1e-4, 5]``.

    The price is checked against no-arbitrage bounds (call: ``max(Se^{−qT}−Ke^{−rT},0) ≤ C ≤ Se^{−qT}``,
    put: ``max(Ke^{−rT}−Se^{−qT},0) ≤ P ≤ Ke^{−rT}``), clamped into them (tiny tolerance), and the
    problem is solved on the out-of-the-money side (time value = OTM price by parity) for precision.
    Initial guess: Corrado-Miller (1996). Returns ``nan`` outside the bounds, when T ≤ 0, when the
    price has no time value (at intrinsic) or the solution is outside ``[1e-4, 5]``.
    Convergence: ``|Δσ| < tol`` or bracket width < ``tol``.
    """
    arrs = np.broadcast_arrays(_f(price), _f(S), _f(K), _f(T), _f(r), _f(q), np.asarray(is_call, dtype=bool))
    shape = arrs[0].shape
    p, S_, K_, T_, r_, q_ = (np.array(a, dtype=float).ravel() for a in arrs[:6])
    call = np.array(arrs[6], dtype=bool).ravel()
    n = p.size
    out = np.full(n, np.nan)
    with np.errstate(all="ignore"):
        Tp = np.where(T_ > 0, T_, 0.0)
        F = S_ * np.exp(-q_ * Tp)
        Kd = K_ * np.exp(-r_ * Tp)
        lower = np.where(call, np.maximum(F - Kd, 0.0), np.maximum(Kd - F, 0.0))
        upper = np.where(call, F, Kd)
        eps = 1e-10 * np.maximum(upper, 1.0)
        valid = (
            np.isfinite(p + S_ + K_ + T_ + r_ + q_)
            & (T_ > 0)
            & (S_ > 0)
            & (K_ > 0)
            & (p > 0)
            & (p >= lower - eps)
            & (p <= upper + eps)
        )
        pc = np.clip(p, lower, upper)
        target = pc - lower  # time value == price of the OTM counterpart
        solve_call = F <= Kd  # call is OTM (or ATM) on the forward
        valid &= target > 0

        idx = np.flatnonzero(valid)
        if idx.size == 0:
            return _out(out.reshape(shape))
        Fi, Ki, Ti, tg, ci = F[idx], Kd[idx], Tp[idx], target[idx], solve_call[idx]

        lo = np.full(idx.size, _IV_LO)
        hi = np.full(idx.size, _IV_HI)
        p_lo, _ = _otm_price_vega(Fi, Ki, Ti, lo, ci)
        p_hi, _ = _otm_price_vega(Fi, Ki, Ti, hi, ci)
        ok = (tg >= p_lo) & (tg <= p_hi)

        # Corrado-Miller initial guess on the call price C = tv + max(F − Kd, 0).
        C = tg + np.maximum(Fi - Ki, 0.0)
        m = C - 0.5 * (Fi - Ki)
        disc = np.maximum(m * m - (Fi - Ki) ** 2 / np.pi, 0.0)
        guess = _SQRT_2PI / (Fi + Ki) * (m + np.sqrt(disc)) / np.sqrt(Ti)
        sig = np.where(np.isfinite(guess), np.clip(guess, 0.01, 2.0), 0.25)

        active = ok.copy()
        for _ in range(max_iter):
            a = np.flatnonzero(active)
            if a.size == 0:
                break
            s = sig[a]
            pr, vg = _otm_price_vega(Fi[a], Ki[a], Ti[a], s, ci[a])
            diff = pr - tg[a]
            lo_a = np.where(diff < 0, s, lo[a])
            hi_a = np.where(diff > 0, s, hi[a])
            newton = s - diff / vg
            use_bis = ~np.isfinite(newton) | (newton <= lo_a) | (newton >= hi_a)
            new = np.where(use_bis, 0.5 * (lo_a + hi_a), newton)
            done = (np.abs(new - s) < tol) | (hi_a - lo_a < tol) | (diff == 0)
            lo[a], hi[a], sig[a] = lo_a, hi_a, np.where(diff == 0, s, new)
            active[a] = ~done
        res = np.where(ok, sig, np.nan)
        out[idx] = res
    return _out(out.reshape(shape))


def _bvn_cdf(a: NDArray[np.float64], b: NDArray[np.float64], rho: NDArray[np.float64]) -> NDArray[np.float64]:
    """Bivariate standard normal CDF ``M(a, b; ρ)`` via
    ``M = N(a)N(b) + (1/2π) ∫_0^{asin ρ} exp(−(a² + b² − 2ab sinθ)/(2cos²θ)) dθ`` (Genz 2004 form),
    20-point Gauss-Legendre. Accurate for |ρ| ≲ 0.9 (here |ρ| = √0.618 ≈ 0.786)."""
    a = np.clip(a, -40.0, 40.0)[..., None]
    b = np.clip(b, -40.0, 40.0)[..., None]
    asr = np.arcsin(rho)[..., None]
    th = 0.5 * asr * (_GL_X + 1.0)
    sn = np.sin(th)
    cs2 = 1.0 - sn * sn
    integ = np.exp(-(a * a + b * b - 2.0 * a * b * sn) / (2.0 * cs2))
    integral = 0.5 * asr[..., 0] * np.sum(_GL_W * integ, axis=-1)
    return ndtr(a[..., 0]) * ndtr(b[..., 0]) + integral / (2.0 * np.pi)


def _bs2002_call(S, K, T, r, b, v):
    """Bjerksund & Stensland (2002) American call, cost of carry ``b = r − q`` (Haug 2007, §3.1.2).
    Inputs are 1-D arrays where early exercise may be optimal (b < r, T > 0, v > 0)."""
    v2 = v * v
    sqrt5 = math.sqrt(5.0)
    t1 = 0.5 * (sqrt5 - 1.0) * T
    beta = (0.5 - b / v2) + np.sqrt((b / v2 - 0.5) ** 2 + 2.0 * r / v2)
    b_inf = beta / (beta - 1.0) * K
    b0 = np.maximum(K, r / (r - b) * K)
    den = (b_inf - b0) * b0
    ht1 = -(b * t1 + 2.0 * v * np.sqrt(t1)) * K * K / den
    ht2 = -(b * T + 2.0 * v * np.sqrt(T)) * K * K / den
    i1 = b0 + (b_inf - b0) * (1.0 - np.exp(ht1))
    i2 = b0 + (b_inf - b0) * (1.0 - np.exp(ht2))
    a1 = (i1 - K) * i1 ** (-beta)
    a2 = (i2 - K) * i2 ** (-beta)

    def phi(t, g, h, i):
        lam = (-r + g * b + 0.5 * g * (g - 1.0) * v2) * t
        vt = v * np.sqrt(t)
        d = -(np.log(S / h) + (b + (g - 0.5) * v2) * t) / vt
        kap = 2.0 * b / v2 + 2.0 * g - 1.0
        return np.exp(lam) * S**g * (ndtr(d) - (i / S) ** kap * ndtr(d - 2.0 * np.log(i / S) / vt))

    def ksi(t2, g, h):
        drift = b + (g - 0.5) * v2
        vt1 = v * np.sqrt(t1)
        vt2 = v * np.sqrt(t2)
        e1 = (np.log(S / i1) + drift * t1) / vt1
        e2 = (np.log(i2 * i2 / (S * i1)) + drift * t1) / vt1
        e3 = (np.log(S / i1) - drift * t1) / vt1
        e4 = (np.log(i2 * i2 / (S * i1)) - drift * t1) / vt1
        f1 = (np.log(S / h) + drift * t2) / vt2
        f2 = (np.log(i2 * i2 / (S * h)) + drift * t2) / vt2
        f3 = (np.log(i1 * i1 / (S * h)) + drift * t2) / vt2
        f4 = (np.log(S * i1 * i1 / (h * i2 * i2)) + drift * t2) / vt2
        rho = np.sqrt(t1 / t2)
        lam = -r + g * b + 0.5 * g * (g - 1.0) * v2
        kap = 2.0 * b / v2 + (2.0 * g - 1.0)
        return (
            np.exp(lam * t2)
            * S**g
            * (
                _bvn_cdf(-e1, -f1, rho)
                - (i2 / S) ** kap * _bvn_cdf(-e2, -f2, rho)
                - (i1 / S) ** kap * _bvn_cdf(-e3, -f3, -rho)
                + (i1 / i2) ** kap * _bvn_cdf(-e4, -f4, -rho)
            )
        )

    one = np.ones_like(S)
    zero = np.zeros_like(S)
    val = (
        a2 * S**beta
        - a2 * phi(t1, beta, i2, i2)
        + phi(t1, one, i2, i2)
        - phi(t1, one, i1, i2)
        - K * phi(t1, zero, i2, i2)
        + K * phi(t1, zero, i1, i2)
        + a1 * phi(t1, beta, i1, i2)
        - a1 * ksi(T, beta, i1)
        + ksi(T, one, i1)
        - ksi(T, one, K)
        - K * ksi(T, zero, i1)
        + K * ksi(T, zero, K)
    )
    return np.where(S >= i2, S - K, val)


def bjerksund_stensland(
    S: ArrayLike, K: ArrayLike, T: ArrayLike, r: ArrayLike, q: ArrayLike, sigma: ArrayLike, is_call: ArrayLike
) -> FloatOrArray:
    """American option, Bjerksund & Stensland (2002) two-step flat-boundary approximation.

    Puts use the put-call transformation ``P(S, K, T, r, b, σ) = C(K, S, T, r − b, −b, σ)`` with
    ``b = r − q``. Where early exercise is never optimal (call with q ≤ 0, put with r ≤ 0) the BSM
    price is returned. The result is floored at ``max(European, intrinsic)``; degenerate inputs
    (T ≤ 0, σ ≤ 0) fall back to that floor.
    """
    arrs = np.broadcast_arrays(_f(S), _f(K), _f(T), _f(r), _f(q), _f(sigma), np.asarray(is_call, dtype=bool))
    shape = arrs[0].shape
    S_, K_, T_, r_, q_, v_ = (np.array(a, dtype=float).ravel() for a in arrs[:6])
    call = np.array(arrs[6], dtype=bool).ravel()
    euro = np.atleast_1d(_f(bs_price(S_, K_, T_, r_, q_, v_, call)))
    intr = np.where(call, np.maximum(S_ - K_, 0.0), np.maximum(K_ - S_, 0.0))
    floor = np.maximum(euro, intr)
    out = floor.copy()
    with np.errstate(all="ignore"):
        # transformed call parameters
        s_c = np.where(call, S_, K_)
        k_c = np.where(call, K_, S_)
        r_c = np.where(call, r_, q_)
        b_c = np.where(call, r_ - q_, q_ - r_)
        need = (T_ > 0) & (v_ > 0) & (b_c < r_c) & (S_ > 0) & (K_ > 0) & np.isfinite(floor)
        idx = np.flatnonzero(need)
        if idx.size:
            am = _bs2002_call(s_c[idx], k_c[idx], T_[idx], r_c[idx], b_c[idx], v_[idx])
            am = np.where(np.isfinite(am), am, floor[idx])
            out[idx] = np.maximum(am, floor[idx])
    return _out(out.reshape(shape))


def expected_move(S: ArrayLike, iv: ArrayLike, T: ArrayLike) -> FloatOrArray:
    """One-standard-deviation move ``S · σ_iv · √T`` (T in years; T < 0 treated as 0)."""
    S, iv, T = _f(S), _f(iv), _f(T)
    return _out(S * iv * np.sqrt(np.maximum(T, 0.0)))
