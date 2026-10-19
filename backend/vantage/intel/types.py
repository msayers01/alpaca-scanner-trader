"""Domain types for the intel layer (social chatter, statements by market movers, prediction markets, VIX)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Kind = Literal["social", "statement"]


@dataclass(slots=True)
class IntelItem:
    """One post / statement. ``sentiment`` in [-1, 1] (positive = bullish for the named symbols / the market)."""

    id: str
    t: int
    kind: Kind
    source: str  # reddit | stocktwits | x | truth | rss:<id> | news:<id> | sim
    author: str
    text: str
    url: str = ""
    symbols: list[str] = field(default_factory=list)
    sentiment: float | None = None
    strength: float | None = None  # 0..1 confidence in the sentiment (lexicon strength, or 1 for user-tagged / LLM)
    engagement: float = 0.0  # upvotes / likes / reshares
    themes: list[str] = field(default_factory=list)
    mover: str | None = None  # configured market mover this statement is attributed to
    weight: float = 1.0  # mover importance (statements) or source reliability (social)
    label: str | None = None  # explicit bullish/bearish tag from the platform (Stocktwits)
    llm: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MentionSnap:
    """Aggregated attention for a ticker from a source that only publishes counts (ApeWisdom)."""

    symbol: str
    t: int
    source: str
    mentions: float
    mentions_prev: float  # same source 24 h earlier
    upvotes: float = 0.0
    rank: int | None = None
    rank_prev: int | None = None


@dataclass(slots=True)
class PredictionMarket:
    id: str
    source: str  # polymarket | kalshi
    question: str
    url: str
    p: float  # probability of 'yes' in [0, 1]
    dp24: float  # 24 h change in p
    volume24h: float
    liquidity: float
    end_ms: int | None
    themes: list[str]
    polarity: int  # +1: higher yes-probability is bullish for equities, -1 bearish, 0 volatility only
    weight: float
    t: int

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Batch:
    """What one source poll returns."""

    items: list[IntelItem] = field(default_factory=list)
    mentions: list[MentionSnap] = field(default_factory=list)
    markets: list[PredictionMarket] = field(default_factory=list)
    vix: dict[str, Any] | None = None  # {"vix","prev_close","vix9d","vix3m","vvix","skew","hist":[...]}


@dataclass(slots=True)
class SourceState:
    id: str
    label: str
    kind: str  # social | statement | prediction | volatility
    enabled: bool = True
    configured: bool = True  # False when required credentials are missing
    needs: str | None = None  # e.g. "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"
    note: str = ""
    interval_s: float = 300.0
    last_poll: int | None = None
    last_ok: int | None = None
    last_error: str | None = None
    n_total: int = 0
    n_last: int = 0

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)
