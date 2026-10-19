"""End-to-end smoke tests of the REST API and WebSocket in SIM mode (no network, no keys)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from vantage.config import Settings
from vantage.main import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    s = Settings(_env_file=None, vantage_mode="sim", data_dir=d, llm_enabled=False,
                 universe=["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMD"])
    app = create_app(s, start_mode="sim")
    with TestClient(app) as c:
        yield c


def test_status(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "sim" and s["market"]["is_open"] is True
    assert s["autotrader"]["armed"] is False


def test_live_mode_refused_without_env(client):
    r = client.post("/api/mode", json={"mode": "live", "confirm": "LIVE"})
    assert r.status_code == 403
    assert client.get("/api/status").json()["mode"] == "sim"


def test_foreign_origin_blocked(client):
    r = client.post("/api/autotrader/arm", json={"armed": True}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/autotrader").json()["armed"] is False


def test_account_positions_history(client):
    a = client.get("/api/account").json()
    assert a["equity"] > 0 and a["tier"] in ("micro", "small", "growth", "pro")
    assert isinstance(client.get("/api/positions").json(), list)
    for rng in ("1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"):
        h = client.get(f"/api/portfolio/history?range={rng}").json()
        assert h["points"] and h["base_value"] > 0, rng


def test_market_data(client):
    q = client.get("/api/quote/AAPL").json()
    assert q["last"] > 0
    for rng in ("1D", "1W", "1M", "3M", "YTD", "1Y", "5Y"):
        b = client.get(f"/api/bars?symbol=AAPL&range={rng}").json()
        assert len(b["bars"]) > 2, rng
    assert client.get("/api/search?q=NV").json()[0]["symbol"] == "NVDA"


def test_options_chain_and_bars(client):
    exps = client.get("/api/options/expirations?underlying=AAPL").json()
    assert len(exps) >= 5
    ch = client.get(f"/api/options/chain?underlying=AAPL&expiration={exps[2]}").json()
    assert ch["calls"] and ch["puts"] and ch["atm_iv"] > 0
    occ = ch["calls"][len(ch["calls"]) // 2]["symbol"]
    b = client.get(f"/api/options/bars?symbol={occ}&range=1W").json()
    assert b["bars"]


def test_manual_order_flow(client):
    r = client.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1, "type": "market"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "filled"
    pos = client.get("/api/positions").json()
    deadline = time.time() + 5
    while not any(p["symbol"] == "AAPL" for p in pos) and time.time() < deadline:
        time.sleep(0.3)
        pos = client.get("/api/positions").json()
    assert any(p["symbol"] == "AAPL" for p in pos)
    r = client.post("/api/positions/AAPL/close", json={})
    assert r.status_code == 200
    bad = client.post("/api/orders", json={"symbol": "AAPL261120C00250000", "side": "buy", "qty": 1, "type": "market"})
    assert bad.status_code == 400


def test_scanner_signals_and_autotrader(client):
    f = client.get("/api/signals/forecast/AAPL").json()
    assert 0 <= f["prob_up"] <= 1
    assert client.get("/api/signals?symbol=AAPL").json()
    assert client.get("/api/signals/models").json()
    params = client.get("/api/scanner/params").json()
    assert any(p["name"] == "max_dte" for p in params)
    res = client.post("/api/scanner/run", json={"side": "both", "min_open_interest": 0, "min_volume": 0,
                                               "max_spread_pct": 0.5, "min_ev": -1}).json()
    assert "candidates" in res and res["candidates"], res["errors"]
    c0 = res["candidates"][0]
    for k in ("ev_pct", "pop", "kelly", "edge_score", "reasons"):
        assert k in c0
    st = client.get("/api/autotrader").json()
    assert st["risk_schema"] and st["presets"]
    strategies = client.get("/api/strategies").json()
    assert {s["id"] for s in strategies} >= {"options_edge", "tsmom_equity", "llm_analyst"}
    r = client.put("/api/strategies/options_edge", json={"enabled": True, "params": {"z_min": 0.1}})
    assert r.json()["params"]["z_min"] == 0.1
    assert client.post("/api/autotrader/preset", json={"tier": "micro"}).json()["tier"] == "micro"
    assert client.post("/api/autotrader/arm", json={"armed": True}).json()["armed"] is True
    assert client.post("/api/autotrader/cycle").status_code == 200
    assert isinstance(client.get("/api/autotrader/decisions").json(), list)
    k = client.post("/api/autotrader/kill", json={"flatten": False}).json()
    assert k["killed"] and not k["armed"]
    assert client.post("/api/autotrader/arm", json={"armed": True}).status_code == 409
    assert client.post("/api/autotrader/reset-kill").json()["killed"] is False


def test_calendar(client):
    ev = client.get("/api/calendar?start=2026-10-01&end=2026-12-31").json()
    kinds = {e["kind"] for e in ev}
    assert {"fomc", "opex", "holiday"} <= kinds
    new = client.post("/api/calendar", json={"date": "2026-11-02", "title": "My event", "importance": 2}).json()
    assert new["id"].startswith("user-")
    assert client.delete(f"/api/calendar/{new['id']}").json()["ok"]


def test_backtest(client):
    r = client.post("/api/backtest", json={"strategy_id": "tsmom_equity", "symbols": ["AAPL", "NVDA", "SPY"],
                                           "start": "2025-06-01", "end": "2026-09-30", "initial_equity": 10000})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["equity"] and "sharpe" in res["metrics"] and 0 <= res["metrics"]["psr"] <= 1


def test_backtest_equal_weight_actually_moves_equity(client):
    """Regression: single-symbol backtests used live per-position caps, so the curve stayed at ~$10,000."""
    body = {"strategy_id": "tsmom_equity", "symbols": ["SPY"], "start": "2025-06-01", "end": "2026-09-30",
            "initial_equity": 10000}
    ew = client.post("/api/backtest", json=body).json()
    live = client.post("/api/backtest", json={**body, "sizing": "live"}).json()
    assert ew["sizing"] == "equal_weight" and live["sizing"] == "live"
    assert ew["metrics"]["trades"] > 0 and ew["funnel"]["ideas"] > 0
    move = lambda r: max(abs(p["v"] - 10000) for p in r["equity"])  # noqa: E731
    assert move(ew) > 3 * move(live) and move(ew) > 100  # equal-weight exposure is materially larger


def test_backtest_refuses_unsupported_strategies_with_reason(client):
    for sid in ("flow_follow", "llm_analyst", "orb_intraday"):
        r = client.post("/api/backtest", json={"strategy_id": sid, "symbols": ["SPY"]})
        assert r.status_code == 400 and "can't be backtested" in r.json()["detail"], sid
    info = {s["id"]: s["backtest"] for s in client.get("/api/strategies").json()}
    assert info["tsmom_equity"]["support"] == "full" and info["options_edge"]["support"] == "synthetic"
    assert info["flow_follow"]["support"] == "none" and info["flow_follow"]["note"]
    zero = client.post("/api/backtest", json={"strategy_id": "options_edge", "symbols": ["SPY"],
                                              "start": "2026-03-01", "end": "2026-06-01"}).json()
    assert "funnel" in zero and any(n.startswith("Funnel:") for n in zero["notes"])


def test_llm_endpoints(client):
    info = client.get("/api/llm").json()
    assert "model" in info


def test_llm_call_log_endpoints(client):
    from vantage.llm.gateway import LLMResult

    llm = client.app.state.vs.container.llm
    llm._log("gate", "SYS", '{"symbol": "AAPL"}', LLMResult(True, {"decision": "reject", "rationale": "earnings"},
                                                             '{"decision":"reject"}', 1500), {"prompt_tokens": 9})
    llm._log("news", "SYS2", "headline", LLMResult(False, None, "", 45000, "TimeoutError: "), {})
    llm.store.flush()
    res = client.get("/api/llm/calls").json()
    assert {c["purpose"] for c in res["calls"]} >= {"gate", "news"} and res["by_purpose"]
    gate = next(c for c in res["calls"] if c["purpose"] == "gate")
    assert gate["preview"].startswith("reject: earnings") and gate["ok"] and "system" not in gate
    assert all(not c["ok"] for c in client.get("/api/llm/calls?status=fail").json()["calls"])
    assert [c["purpose"] for c in client.get("/api/llm/calls?purpose=news").json()["calls"]] == ["news"]
    assert client.get("/api/llm/calls?q=AAPL").json()["calls"][0]["id"] == gate["id"]
    d = client.get(f"/api/llm/calls/{gate['id']}").json()
    assert d["system"] == "SYS" and d["user"] == '{"symbol": "AAPL"}' and d["parsed"]["decision"] == "reject"
    assert client.get("/api/llm/calls/nope").status_code == 404


def test_websocket_stream(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text('{"op":"sub","ch":["quote:AAPL","account","equity"]}')
        got = set()
        deadline = time.time() + 5
        while time.time() < deadline and not {"quote:AAPL", "account"} <= got:
            msg = ws.receive_json(mode="text")  # the browser client only reads text frames
            for item in msg.get("b", []):
                got.add(item["ch"])
        assert {"quote:AAPL", "account"} <= got
