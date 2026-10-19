"""Expected value of long options under the PHYSICAL measure at a planned exit horizon.

See docs/QUANT.md §5. S_h = S·exp((μ − σ_p²/2)h + σ_p√h·z); option re-priced at h with Black-Scholes using
the IV path; exit cost deducted; expectation over z by Gauss-Hermite quadrature. Vectorised over contracts.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike
from scipy.special import ndtr

from vantage.quant.bs import bs_price
from vantage.quant.kelly import kelly_discrete_batch


@lru_cache(maxsize=16)
def _gh(n: int) -> tuple[np.ndarray, np.ndarray]:
    x, w = np.polynomial.hermite.hermgauss(n)
    z = np.sqrt(2.0) * x
    p = w / np.sqrt(np.pi)
    z.flags.writeable = False
    p.flags.writeable = False
    return z, p


def gauss_hermite_normal(n: int = 48) -> tuple[np.ndarray, np.ndarray]:
    """Nodes z and probabilities p with Σ p f(z) ≈ E[f(Z)], Z ~ N(0, 1)."""
    z, p = _gh(int(n))
    return z.copy(), p.copy()


def iv_path(iv: ArrayLike, iv_target: ArrayLike | None, h_years: ArrayLike, tau_days: float = 30.0) -> np.ndarray:
    """IV after h years mean-reverting toward ``iv_target`` with time constant ``tau_days``."""
    iv = np.asarray(iv, dtype=float)
    if iv_target is None:
        return iv
    tgt = np.asarray(iv_target, dtype=float)
    k = 1.0 - np.exp(-np.asarray(h_years, dtype=float) * 365.0 / tau_days)
    return iv + (tgt - iv) * k


def evaluate_long_options(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    is_call: ArrayLike,
    entry_price: ArrayLike,
    exit_cost_frac: ArrayLike,
    r: float,
    q: float,
    iv: ArrayLike,
    mu: ArrayLike,
    sigma_p: ArrayLike,
    h: ArrayLike,
    iv_target: ArrayLike | None = None,
    n_nodes: int = 48,
    iv_crush: float = 0.0,
) -> dict[str, np.ndarray]:
    K = np.atleast_1d(np.asarray(K, dtype=float))
    M = K.shape[0]

    def col(x: ArrayLike) -> np.ndarray:
        return np.broadcast_to(np.asarray(x, dtype=float), (M,)).astype(float)

    S_, T_, mu_, sp_, h_ = col(S), col(T), col(mu), col(sigma_p), col(h)
    call = np.broadcast_to(np.asarray(is_call, dtype=bool), (M,))
    entry = col(entry_price)
    cost = np.clip(col(exit_cost_frac), 0.0, 1.0)
    iv_ = col(iv)
    h_ = np.clip(h_, 0.0, T_)
    sp_ = np.maximum(sp_, 1e-6)

    z, p = _gh(int(n_nodes))
    drift = (mu_ - 0.5 * sp_**2) * h_
    Sh = S_[:, None] * np.exp(drift[:, None] + (sp_ * np.sqrt(h_))[:, None] * z[None, :])  # (M, n)
    ivh = iv_path(iv_, None if iv_target is None else col(iv_target), h_) * (1.0 - iv_crush)
    ivh = np.maximum(ivh, 1e-4)
    Vh = np.asarray(bs_price(Sh, K[:, None], (T_ - h_)[:, None], r, q, ivh[:, None], call[:, None]), dtype=float)
    Pout = Vh * (1.0 - cost[:, None])
    with np.errstate(divide="ignore", invalid="ignore"):
        R = np.where(entry[:, None] > 0, Pout / entry[:, None] - 1.0, np.nan)
    ev = np.nansum(R * p[None, :], axis=1)
    pop = ((R > 0) * p[None, :]).sum(axis=1)
    kelly = kelly_discrete_batch(np.nan_to_num(R, nan=-1.0), p)
    value_h = (Vh * p[None, :]).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        d2 = (np.log(S_ / K) + (mu_ - 0.5 * sp_**2) * T_) / (sp_ * np.sqrt(np.maximum(T_, 1e-12)))
    prob_itm = np.where(call, ndtr(d2), ndtr(-d2))
    breakeven = np.where(call, K + entry, K - entry)
    return {
        "ev_pct": ev,
        "pop": pop,
        "kelly": kelly,
        "value_h": value_h,
        "prob_itm_expiry": prob_itm,
        "breakeven": breakeven,
    }
