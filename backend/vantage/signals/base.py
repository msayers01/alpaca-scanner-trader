"""Signal model interface. See docs/QUANT.md §6."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class SymbolData:
    symbol: str
    price: float
    daily: dict[str, np.ndarray]  # t,o,h,l,c,v (oldest first)
    intraday: dict[str, np.ndarray] | None = None  # today's 1-min bars
    vol_forecast: float | None = None  # annualised physical vol
    atm_iv: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # cross-sectional values etc.

    @property
    def n(self) -> int:
        return len(self.daily["c"]) if self.daily else 0


@dataclass
class SignalContext:
    news: Any = None
    flow: Any = None
    calendar: Any = None
    intel: Any = None
    now: int = 0
    session_open: bool = True


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    horizon_days: float
    kind: str  # "daily" | "intraday" | "event"
    directional: bool
    prior_weight: float
    description: str
    fn: Callable[[SymbolData, SignalContext], tuple[float, float, dict[str, Any]] | None]


def squash(z: float) -> float:
    """Map a z-score to a bounded score in [-1, 1]."""
    if not math.isfinite(z):
        return 0.0
    return math.tanh(z / 2.0)


def unsquash(score: float) -> float:
    s = max(-0.999, min(0.999, score))
    return 2.0 * math.atanh(s)
