"""AlpacaREST order submission: a retried POST must never leave a live order untracked."""

from __future__ import annotations

import httpx
import pytest

from vantage.broker.alpaca_rest import AlpacaREST, fmt_price
from vantage.broker.base import BrokerError


def _client(handler) -> AlpacaREST:
    c = AlpacaREST("paper", "k", "s")
    c._trade = httpx.AsyncClient(base_url="https://paper-api.alpaca.markets", transport=httpx.MockTransport(handler))
    return c


ORDER = {"id": "b-1", "client_order_id": "vt-1", "symbol": "AAPL", "side": "buy", "qty": "1", "status": "accepted", "type": "limit",
         "asset_class": "us_equity"}
REQ = {"symbol": "AAPL", "side": "buy", "qty": 1, "type": "limit", "limit_price": 150.0, "client_order_id": "vt-1"}


async def test_duplicate_client_order_id_after_retry_adopts_the_live_order():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            if len([c for c in calls if c[0] == "POST"]) == 1:
                return httpx.Response(503, headers={"retry-after": "0"})  # accepted upstream, response lost
            return httpx.Response(422, json={"message": "client_order_id must be unique", "code": 40010001})
        return httpx.Response(200, json=ORDER)  # GET /v2/orders:by_client_order_id

    o = await _client(handler).submit_order(REQ)
    assert o["id"] == "b-1" and o["client_order_id"] == "vt-1"
    assert calls[-1] == ("GET", "/v2/orders:by_client_order_id")


async def test_real_rejection_is_still_raised():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(403, json={"message": "insufficient buying power", "code": 40310000})
        return httpx.Response(404, json={"message": "order not found"})

    with pytest.raises(BrokerError, match="insufficient buying power"):
        await _client(handler).submit_order(REQ)


async def test_422_without_a_matching_order_is_raised():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"message": "limit price must be positive"})
        return httpx.Response(404, json={"message": "order not found"})

    with pytest.raises(BrokerError, match="limit price"):
        await _client(handler).submit_order(REQ)


def test_fmt_price():
    assert fmt_price(150.0) == "150.00" and fmt_price(2.05) == "2.05" and fmt_price(0.45) == "0.45"
    assert fmt_price(0.5023) == "0.5023" and fmt_price(0.5) == "0.50" and fmt_price(1.0) == "1.00"
