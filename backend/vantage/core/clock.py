"""US equity market clock (America/New_York) with rule-based NYSE holidays.

The broker calendar (Alpaca ``/v2/calendar``) is authoritative when connected; these rules are the
offline fallback and are also used by SIM mode and the event calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
Session = Literal["pre", "regular", "post", "closed"]

PRE_OPEN = time(4, 0)
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
POST_CLOSE = time(20, 0)


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:  # Saturday -> Friday (NYSE skips this for New Year's Day)
        return None if (d.month, d.day) == (1, 1) else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> dict[date, str]:
    out: dict[date, str] = {}

    def add(d: date | None, name: str) -> None:
        if d is not None:
            out[d] = name

    add(_observed(date(year, 1, 1)), "New Year's Day")
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, 0, 3), "Washington's Birthday")
    add(_easter(year) - timedelta(days=2), "Good Friday")
    add(_last_weekday(year, 5, 0), "Memorial Day")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add(_observed(date(year, 12, 25)), "Christmas Day")
    # New Year's falling on Saturday next year is not observed on Dec 31 (NYSE rule) – nothing to add.
    return out


@lru_cache(maxsize=64)
def nyse_early_closes(year: int) -> dict[date, str]:
    out: dict[date, str] = {}
    hol = nyse_holidays(year)
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in hol:
        out[jul3] = "Independence Day eve (1:00 PM close)"
    out[_nth_weekday(year, 11, 3, 4) + timedelta(days=1)] = "Day after Thanksgiving (1:00 PM close)"
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in hol:
        out[dec24] = "Christmas Eve (1:00 PM close)"
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(a: date, b: date) -> int:
    """Number of trading days in (a, b]."""
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def close_time(d: date) -> time:
    return EARLY_CLOSE if d in nyse_early_closes(d.year) else CLOSE


def et_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(ET)


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def et_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET)


@dataclass(slots=True)
class MarketState:
    is_open: bool
    session: Session
    next_open: int
    next_close: int
    now: int
    minutes_since_open: float | None
    minutes_to_close: float | None

    def to_wire(self) -> dict:
        return {
            "is_open": self.is_open,
            "session": self.session,
            "next_open": self.next_open,
            "next_close": self.next_close,
            "now": self.now,
        }


def market_state(now: datetime | None = None) -> MarketState:
    n = et_now(now)
    d, t = n.date(), n.time()
    trading = is_trading_day(d)
    ct = close_time(d)
    if trading and OPEN <= t < ct:
        session: Session = "regular"
    elif trading and PRE_OPEN <= t < OPEN:
        session = "pre"
    elif trading and ct <= t < POST_CLOSE:
        session = "post"
    else:
        session = "closed"

    if trading and t < OPEN:
        nod = d
    else:
        nod = next_trading_day(d)
    next_open = et_dt(nod, OPEN)
    if session == "regular":
        next_close = et_dt(d, ct)
    else:
        next_close = et_dt(nod, close_time(nod))
    mso = mtc = None
    if session == "regular":
        mso = (n - et_dt(d, OPEN)).total_seconds() / 60
        mtc = (et_dt(d, ct) - n).total_seconds() / 60
    return MarketState(
        is_open=session == "regular",
        session=session,
        next_open=to_ms(next_open),
        next_close=to_ms(next_close),
        now=to_ms(n),
        minutes_since_open=mso,
        minutes_to_close=mtc,
    )


def session_bounds_ms(d: date) -> tuple[int, int]:
    return to_ms(et_dt(d, OPEN)), to_ms(et_dt(d, close_time(d)))


def last_session_date(now: datetime | None = None) -> date:
    """The most recent trading day whose regular session has started (today if after 9:30 ET)."""
    n = et_now(now)
    d = n.date()
    if is_trading_day(d) and n.time() >= OPEN:
        return d
    return prev_trading_day(d)


def third_friday(year: int, month: int) -> date:
    return _nth_weekday(year, month, 4, 3)


def today_et() -> date:
    return et_now().date()
