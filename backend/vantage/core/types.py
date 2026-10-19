"""Domain types shared by every layer. Timestamps are epoch milliseconds (UTC)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

AssetClass = Literal["equity", "option"]
OptionType = Literal["call", "put"]


@dataclass(slots=True)
class Bar:
    symbol: str
    t: int
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float
    vw: float = 0.0
    n: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"t": self.t, "o": self.o, "h": self.h, "l": self.l, "c": self.c, "v": self.v}


@dataclass(slots=True)
class Quote:
    symbol: str
    t: int
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ask or self.bid

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0) if self.bid > 0 and self.ask > 0 else math.nan

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return self.spread / m if m > 0 else math.nan


@dataclass(slots=True)
class Trade:
    symbol: str
    t: int
    price: float
    size: float
    exchange: str = ""
    conditions: str = ""


@dataclass(slots=True)
class OptionContract:
    symbol: str
    underlying: str
    type: OptionType
    strike: float
    expiration: date
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: float = 0.0
    open_interest: float = 0.0
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    t: int = 0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last or self.ask or self.bid

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if self.bid > 0 and self.ask > 0 and m > 0:
            return (self.ask - self.bid) / m
        return math.inf

    def dte(self, today: date) -> int:
        return (self.expiration - today).days

    def to_wire(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "type": self.type,
            "strike": self.strike,
            "expiration": self.expiration.isoformat(),
            "bid": self.bid,
            "ask": self.ask,
            "mid": round(self.mid, 4),
            "last": self.last,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "iv": self.iv,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
        }


@dataclass(slots=True)
class Signal:
    model: str
    symbol: str
    score: float  # [-1, 1], positive = bullish
    confidence: float  # [0, 1]
    horizon_days: float
    t: int
    meta: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "horizon_days": self.horizon_days,
            "t": self.t,
            "meta": self.meta,
        }


@dataclass(slots=True)
class NewsItem:
    id: str
    t: int
    headline: str
    summary: str
    source: str
    url: str
    symbols: list[str]
    sentiment: float | None = None
    relevance: float | None = None
    llm: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FlowEvent:
    t: int
    contract: str
    underlying: str
    type: OptionType
    strike: float
    expiration: str
    price: float
    size: float
    premium: float
    side: Literal["ask", "bid", "mid"]
    is_sweep: bool
    unusual_score: float
    direction: int  # +1 bullish, -1 bearish, 0 neutral
    source: Literal["stream", "snapshot"] = "stream"

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParamSpec:
    name: str
    type: Literal["float", "int", "bool", "enum", "str"]
    default: Any
    label: str
    help: str = ""
    group: str = "General"
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[str] | None = None

    def to_wire(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    def coerce(self, value: Any) -> Any:
        if self.type == "bool":
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type == "int":
            v = int(round(float(value)))
        elif self.type == "float":
            v = float(value)
        elif self.type == "enum":
            if self.options and value not in self.options:
                raise ValueError(f"{self.name}: {value!r} not in {self.options}")
            return value
        else:
            return str(value)
        if self.min is not None:
            v = max(v, type(v)(self.min))
        if self.max is not None:
            v = min(v, type(v)(self.max))
        return v


def apply_params(schema: list[ParamSpec], values: dict[str, Any] | None) -> dict[str, Any]:
    """Merge user values onto schema defaults, coercing and clamping each one."""
    out = {p.name: p.default for p in schema}
    by_name = {p.name: p for p in schema}
    for k, v in (values or {}).items():
        if k in by_name and v is not None:
            out[k] = by_name[k].coerce(v)
    return out


@dataclass(slots=True)
class TradeIdea:
    strategy_id: str
    symbol: str  # instrument to trade (OCC for options)
    underlying: str
    asset_class: AssetClass
    direction: int  # +1 bullish exposure, -1 bearish exposure
    limit_price: float  # reference price per share / per contract (not x100)
    edge_score: float
    expected_return: float  # expected return on capital at horizon (fraction)
    win_prob: float
    kelly_fraction: float  # full-Kelly fraction of equity
    horizon_days: float
    rationale: str
    contract: OptionContract | None = None
    stop_pct: float | None = None  # e.g. 0.4 = exit at -40 %
    target_pct: float | None = None
    trail_pct: float | None = None
    trail_activate_pct: float | None = None
    time_stop_days: float | None = None
    min_dte_exit: int | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    use_llm_gate: bool = False
    size_multiplier: float = 1.0  # set by LLM gate (<= 1)
    spread_pct: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__slots__ if k != "contract"}  # type: ignore[attr-defined]
        if self.contract is not None:
            d["contract"] = self.contract.to_wire()
        return d


@dataclass(slots=True)
class Decision:
    id: str
    t: int
    strategy_id: str
    symbol: str
    action: Literal["enter", "exit", "skip", "reject", "error"]
    reason: str
    edge_score: float | None = None
    llm: dict[str, Any] | None = None
    order_id: str | None = None
    idea: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "t": self.t,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "action": self.action,
            "reason": self.reason,
            "edge_score": self.edge_score,
            "llm": self.llm,
            "order_id": self.order_id,
        }


@dataclass(slots=True)
class ExitSignal:
    reason: str
    pct: float = 1.0  # fraction of position to close
    urgent: bool = False  # cross the spread instead of working the order


@dataclass(slots=True)
class ManagedPosition:
    """A broker position joined with the metadata Vantage stored when it opened it."""

    symbol: str
    underlying: str
    asset_class: AssetClass
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_plpc: float
    strategy_id: str | None = None
    direction: int = 1
    entry_t: int | None = None
    peak_plpc: float = 0.0
    stop_pct: float | None = None
    target_pct: float | None = None
    trail_pct: float | None = None
    trail_activate_pct: float | None = None
    time_stop_days: float | None = None
    min_dte_exit: int | None = None
    expiration: date | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def parse_occ(symbol: str) -> tuple[str, date, OptionType, float] | None:
    """Parse an OCC option symbol like AAPL261120C00250000 -> (root, expiry, type, strike)."""
    s = symbol.strip().upper()
    if len(s) < 16:
        return None
    tail = s[-15:]
    root = s[:-15]
    try:
        yy, mm, dd = int(tail[0:2]), int(tail[2:4]), int(tail[4:6])
        cp = tail[6]
        strike = int(tail[7:]) / 1000.0
        if cp not in ("C", "P") or not root:
            return None
        return root, date(2000 + yy, mm, dd), ("call" if cp == "C" else "put"), strike
    except ValueError:
        return None


def make_occ(root: str, expiration: date, type_: OptionType, strike: float) -> str:
    return f"{root.upper()}{expiration:%y%m%d}{'C' if type_ == 'call' else 'P'}{int(round(strike * 1000)):08d}"
