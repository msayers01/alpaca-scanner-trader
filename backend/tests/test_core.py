from __future__ import annotations

import asyncio
from datetime import date, datetime

from vantage.core import clock
from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.core.types import ParamSpec, apply_params, make_occ, parse_occ
from vantage.core.util import iso_to_ms


def test_occ_roundtrip():
    s = make_occ("AAPL", date(2026, 11, 20), "call", 250)
    assert s == "AAPL261120C00250000"
    root, exp, typ, k = parse_occ(s)
    assert (root, exp, typ, k) == ("AAPL", date(2026, 11, 20), "call", 250.0)
    assert parse_occ("SPY") is None
    assert parse_occ("SPY261218P00512500")[3] == 512.5


def test_nyse_holidays_2026():
    h = clock.nyse_holidays(2026)
    assert date(2026, 1, 1) in h  # Thursday
    assert date(2026, 4, 3) in h  # Good Friday
    assert date(2026, 7, 3) in h  # July 4th is Saturday -> observed Friday
    assert date(2026, 11, 26) in h  # Thanksgiving
    assert date(2026, 12, 25) in h
    assert not clock.is_trading_day(date(2026, 7, 3))
    assert clock.is_trading_day(date(2026, 10, 19))
    assert date(2026, 11, 27) in clock.nyse_early_closes(2026)


def test_market_state_sessions():
    et = clock.ET
    ms = clock.market_state(datetime(2026, 10, 19, 10, 0, tzinfo=et))
    assert ms.is_open and ms.session == "regular" and round(ms.minutes_since_open) == 30
    ms = clock.market_state(datetime(2026, 10, 19, 8, 0, tzinfo=et))
    assert ms.session == "pre" and not ms.is_open
    ms = clock.market_state(datetime(2026, 10, 18, 12, 0, tzinfo=et))  # Sunday
    assert ms.session == "closed"


def test_iso_parsing_nanoseconds():
    assert iso_to_ms("2026-10-19T14:30:00.123456789Z") == iso_to_ms("2026-10-19T14:30:00.123Z")


def test_param_coercion_clamps():
    schema = [ParamSpec("x", "float", 1.0, "x", min=0, max=2), ParamSpec("n", "int", 3, "n", min=1, max=5),
              ParamSpec("b", "bool", False, "b"), ParamSpec("e", "enum", "a", "e", options=["a", "b"])]
    p = apply_params(schema, {"x": 9, "n": "4.6", "b": "true", "unknown": 1})
    assert p == {"x": 2.0, "n": 5, "b": True, "e": "a"}


async def test_bus_patterns_and_drop_oldest():
    bus = Bus()
    sub = bus.subscribe("quote.*", maxsize=2)
    seen = []
    off = bus.on("quote.AAPL", lambda t, p: seen.append(p))
    for i in range(5):
        bus.publish("quote.AAPL", i)
    bus.publish("trade.AAPL", "x")
    assert seen == [0, 1, 2, 3, 4]
    assert sub.dropped == 3
    assert (await sub.get())[1] == 3
    off()
    sub.close()
    await asyncio.sleep(0)


def test_store_roundtrip(tmp_path):
    st = Store(tmp_path / "t.db")
    st.kv_set("sim", "k", {"a": 1})
    assert st.kv_get("sim", "k") == {"a": 1}
    assert st.kv_get("paper", "k") is None
    st.write("INSERT INTO events(id,d,data) VALUES(?,?,?)", ("e1", "2026-01-01", "{}"))
    st.flush()
    assert st.query_sync("SELECT id FROM events") == [{"id": "e1"}]
    st.close()
