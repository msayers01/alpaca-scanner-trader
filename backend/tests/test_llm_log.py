from __future__ import annotations

from vantage.core.bus import Bus
from vantage.core.store import Store
from vantage.llm.gateway import LLMGateway, call_preview

SCHEMA = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["approve", "reject"]},
                                           "rationale": {"type": "string"}}, "required": ["decision", "rationale"]}


async def test_gateway_logs_full_exchange_and_publishes(tmp_path):
    store = Store(tmp_path / "t.db")
    bus = Bus()
    events = []
    bus.on("llm", lambda t, d: events.append(d))
    g = LLMGateway("http://x", "qwen-test", store, "paper", bus=bus)
    g.available, g._last_probe = True, 1e18  # skip network probe

    async def fake_call(system, user, schema, max_tokens):
        return '{"decision": "approve", "rationale": "signals agree"}', {"prompt_tokens": 812, "completion_tokens": 40}

    g._call = fake_call  # type: ignore[method-assign]
    res = await g.complete_json("gate", "SYSTEM PROMPT", "USER DOSSIER", SCHEMA, cache_ttl_s=60)
    again = await g.complete_json("gate", "SYSTEM PROMPT", "USER DOSSIER", SCHEMA, cache_ttl_s=60)
    assert res.ok and again.cached
    store.flush()
    rows = store.query_sync("SELECT * FROM llm_calls ORDER BY t")
    assert len(rows) == 2
    r = rows[0]
    assert (r["system"], r["request"], r["purpose"], r["mode"]) == ("SYSTEM PROMPT", "USER DOSSIER", "gate", "paper")
    assert r["prompt_tokens"] == 812 and r["completion_tokens"] == 40 and r["ok"] == 1 and r["cached"] == 0
    assert '"approve"' in r["parsed"] and rows[1]["cached"] == 1
    assert len(events) == 2 and events[0]["preview"].startswith("approve: signals agree")

    async def bad_call(system, user, schema, max_tokens):
        return "not json at all", {}

    g._call = bad_call  # type: ignore[method-assign]
    res = await g.complete_json("news", "S", "U2", SCHEMA)
    store.flush()
    fail = store.query_sync("SELECT * FROM llm_calls WHERE request='U2'")[0]
    assert not res.ok and fail["ok"] == 0 and fail["error"] == "unparseable JSON" and fail["response"] == "not json at all"
    store.close()


def test_call_preview_formats():
    assert call_preview({"action": "pass", "rationale": "mixed  evidence"}, "", None) == "pass: mixed evidence"
    assert call_preview({"items": [{"symbol": "NVDA", "direction": 1, "magnitude": 0.8}]}, "", None).startswith("NVDA")
    assert call_preview(None, "", "TimeoutError: ") == "TimeoutError:"
    assert len(call_preview(None, "x" * 500, None)) == 181
