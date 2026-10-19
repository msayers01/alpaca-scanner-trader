"""Local-LLM gateway (Ollama native API preferred; any OpenAI-compatible server as fallback).

* Structured output: the JSON schema is sent to the server (Ollama ``format`` / OpenAI
  ``response_format``) and the reply is validated again locally.
* Concurrency-limited, timeout-bounded, small TTL cache, stats, every call logged to SQLite.
* Never raises into callers: returns ``LLMResult(ok=False, error=...)`` instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx
import orjson

from vantage.core.store import Store
from vantage.core.util import dumps, new_id, now_ms

log = logging.getLogger(__name__)

MAX_LOG = 60_000  # characters of prompt/response kept per call


def call_preview(data: dict[str, Any] | None, text: str, error: str | None, n: int = 180) -> str:
    """One-line summary of a reply for list views: decision/action + rationale, else the raw text or error."""
    if not data and text and not error:
        data = _extract_json(text)  # rows logged before `parsed` was stored
    if data:
        head = data.get("decision") or data.get("action") or ""
        why = data.get("rationale") or data.get("reply") or ""
        items = data.get("items")
        if not why and isinstance(items, list) and items:
            it = items[0]
            why = f"{it.get('symbol')}: dir {it.get('direction')}, mag {it.get('magnitude')} ({len(items)} item(s))"
        out = f"{head}: {why}" if head else str(why or dumps(data).decode())
    else:
        out = error or text
    out = " ".join(str(out).split())
    return out[:n] + ("…" if len(out) > n else "")


@dataclass(slots=True)
class LLMResult:
    ok: bool
    data: dict[str, Any] | None = None
    text: str = ""
    latency_ms: int = 0
    error: str | None = None
    cached: bool = False


@dataclass
class LLMStats:
    calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    total_latency_ms: int = 0
    last_error: str | None = None
    by_purpose: dict[str, int] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        ok = self.calls - self.failures
        return {
            "calls": self.calls,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "avg_latency_ms": round(self.total_latency_ms / ok) if ok else None,
            "last_error": self.last_error,
            "by_purpose": self.by_purpose,
        }


def _extract_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        v = orjson.loads(text)
        return v if isinstance(v, dict) else None
    except orjson.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            try:
                v = orjson.loads(m.group(0))
                return v if isinstance(v, dict) else None
            except orjson.JSONDecodeError:
                return None
    return None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate(data: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Minimal JSON-schema check — enough to reject garbage and to clamp what the models rely on.

    Top level: required keys, enums, types; numbers are clamped to min/max. Arrays of objects are validated item by item:
    items that miss a required key or carry a wrong type are *dropped* (one malformed row must not discard — or crash the
    consumer of — an otherwise good reply), and nested numbers are clamped the same way.
    """
    return _check_object(data, schema, top=True)


def _check_object(data: dict[str, Any], schema: dict[str, Any], top: bool) -> str | None:
    for k in schema.get("required", []):
        if k not in data:
            return f"missing key {k}"
    for k, spec in schema.get("properties", {}).items():
        if k not in data:
            continue
        v = data[k]
        t = spec.get("type")
        if "enum" in spec and v not in spec["enum"]:
            return f"{k}={v!r} not in enum"
        if t == "number":
            if not _is_number(v):
                return f"{k} not a number"
            if "minimum" in spec and v < spec["minimum"]:
                data[k] = spec["minimum"]
            if "maximum" in spec and v > spec["maximum"]:
                data[k] = spec["maximum"]
        elif t == "boolean" and not isinstance(v, bool):
            return f"{k} not a boolean"
        elif t == "string" and not isinstance(v, str):
            return f"{k} not a string"
        elif t == "array":
            if not isinstance(v, list):
                return f"{k} not an array"
            item_spec = spec.get("items") or {}
            if item_spec.get("type") == "object":
                kept = []
                for it in v:
                    if isinstance(it, dict) and _check_object(it, item_spec, top=False) is None:
                        kept.append(it)
                data[k] = kept
    return None


class LLMGateway:
    def __init__(
        self,
        base_url: str,
        model: str,
        store: Store | None = None,
        mode: str = "sim",
        enabled: bool = True,
        timeout_s: float = 45.0,
        max_concurrency: int = 2,
        temperature: float = 0.1,
        bus: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.store = store
        self.mode = mode
        self.bus = bus
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.temperature = temperature
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=3.0))
        self._cache: OrderedDict[str, tuple[float, LLMResult]] = OrderedDict()
        self.stats = LLMStats()
        self.available = False
        self.flavor: str | None = None  # "ollama" | "openai"
        self.models: list[str] = []
        self._last_probe = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    def configure(self, **kw: Any) -> None:
        if "base_url" in kw and kw["base_url"]:
            self.base_url = str(kw["base_url"]).rstrip("/")
            self.flavor = None
            self._last_probe = 0
        if "model" in kw and kw["model"]:
            self.model = str(kw["model"])
        if "enabled" in kw and kw["enabled"] is not None:
            self.enabled = bool(kw["enabled"])
        if "temperature" in kw and kw["temperature"] is not None:
            self.temperature = float(kw["temperature"])
        self._cache.clear()

    async def probe(self, force: bool = False) -> bool:
        if not force and time.monotonic() - self._last_probe < 30:
            return self.available
        self._last_probe = time.monotonic()
        try:
            r = await self._client.get(f"{self.base_url}/api/tags", timeout=3.0)
            if r.status_code == 200:
                self.flavor = "ollama"
                self.models = [m["name"] for m in r.json().get("models", [])]
                self.available = True
                return True
        except httpx.HTTPError:
            pass
        try:
            r = await self._client.get(f"{self.base_url}/v1/models", timeout=3.0)
            if r.status_code == 200:
                self.flavor = "openai"
                self.models = [m["id"] for m in r.json().get("data", [])]
                self.available = True
                return True
        except httpx.HTTPError:
            pass
        self.available = False
        return False

    def info(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "base_url": self.base_url,
            "model": self.model,
            "models": self.models,
            "flavor": self.flavor,
            "temperature": self.temperature,
            "stats": self.stats.to_wire(),
        }

    async def complete_json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        cache_ttl_s: float = 0.0,
        max_tokens: int = 700,
    ) -> LLMResult:
        if not self.enabled:
            return LLMResult(False, error="llm disabled")
        if not await self.probe():
            return LLMResult(False, error="llm unavailable")
        key = hashlib.sha1(f"{self.model}|{purpose}|{system}|{user}".encode()).hexdigest()
        if cache_ttl_s > 0:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < cache_ttl_s:
                self.stats.cache_hits += 1
                res = hit[1]
                cached = LLMResult(res.ok, res.data, res.text, 0, res.error, cached=True)
                self._log(purpose, system, user, cached, {})
                return cached
        self.stats.calls += 1
        self.stats.by_purpose[purpose] = self.stats.by_purpose.get(purpose, 0) + 1
        t0 = time.perf_counter()
        usage: dict[str, Any] = {}
        try:
            async with self._sem:
                text, usage = await asyncio.wait_for(self._call(system, user, schema, max_tokens), self.timeout_s)
            data = _extract_json(text)
            err = "unparseable JSON" if data is None else _validate(data, schema)
            res = LLMResult(err is None, data if err is None else None, text, int((time.perf_counter() - t0) * 1000), err)
        except (TimeoutError, httpx.HTTPError, KeyError, ValueError) as e:
            res = LLMResult(False, None, "", int((time.perf_counter() - t0) * 1000), f"{type(e).__name__}: {e}")
        if not res.ok:
            self.stats.failures += 1
            self.stats.last_error = res.error
        else:
            self.stats.total_latency_ms += res.latency_ms
            if cache_ttl_s > 0:
                self._cache[key] = (time.monotonic(), res)
                while len(self._cache) > 256:
                    self._cache.popitem(last=False)
        self._log(purpose, system, user, res, usage)
        return res

    def _log(self, purpose: str, system: str, user: str, res: LLMResult, usage: dict[str, Any]) -> None:
        """Persist the full exchange (for the LLM Log page) and publish a summary on the ``llm`` topic."""
        row = {
            "id": new_id("llm-"),
            "t": now_ms(),
            "purpose": purpose,
            "model": self.model,
            "latency_ms": res.latency_ms,
            "ok": res.ok,
            "cached": res.cached,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "error": res.error,
        }
        if self.store:
            self.store.write(
                "INSERT INTO llm_calls(id,mode,t,purpose,model,latency_ms,ok,request,response,system,error,parsed,"
                "cached,prompt_tokens,completion_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], self.mode, row["t"], purpose, self.model, res.latency_ms, int(res.ok), user[:MAX_LOG],
                 (res.text or "")[:MAX_LOG], system[:MAX_LOG], res.error,
                 dumps(res.data).decode() if res.data else None, int(res.cached), row["prompt_tokens"],
                 row["completion_tokens"]),
            )
        if self.bus is not None:
            self.bus.publish("llm", row | {"preview": call_preview(res.data, res.text, res.error)})

    async def _call(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[str, dict[str, Any]]:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.flavor == "ollama":
            body = {
                "model": self.model,
                "messages": msgs,
                "stream": False,
                "format": schema,
                "think": False,
                "keep_alive": "30m",
                "options": {"temperature": self.temperature, "num_predict": max_tokens},
            }
            r = await self._client.post(f"{self.base_url}/api/chat", content=dumps(body))
            if r.status_code == 400 and "think" in r.text:
                body.pop("think")
                r = await self._client.post(f"{self.base_url}/api/chat", content=dumps(body))
            r.raise_for_status()
            j = r.json()
            return j["message"]["content"], {"prompt_tokens": j.get("prompt_eval_count"),
                                             "completion_tokens": j.get("eval_count")}
        body = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
        }
        r = await self._client.post(f"{self.base_url}/v1/chat/completions", content=dumps(body),
                                    headers={"Content-Type": "application/json"})
        r.raise_for_status()
        j = r.json()
        u = j.get("usage") or {}
        return j["choices"][0]["message"]["content"], {"prompt_tokens": u.get("prompt_tokens"),
                                                       "completion_tokens": u.get("completion_tokens")}

    async def warmup(self) -> None:
        """Load the model into GPU memory (a cold load can take a minute+) without counting toward stats."""
        if not self.enabled or not await self.probe(force=True):
            return
        t0 = time.perf_counter()
        try:
            if self.flavor == "ollama":
                body = {"model": self.model, "messages": [{"role": "user", "content": "ok"}], "stream": False,
                        "keep_alive": "30m", "options": {"num_predict": 1}}
                await self._client.post(f"{self.base_url}/api/chat", content=dumps(body), timeout=300.0)
            log.info("LLM %s warm in %.1fs", self.model, time.perf_counter() - t0)
        except httpx.HTTPError as e:
            log.warning("LLM warm-up failed: %s", e)

    async def test(self, prompt: str | None = None) -> dict[str, Any]:
        await self.probe(force=True)
        schema = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]}
        res = await self.complete_json(
            "test", "You are a concise assistant. Reply in JSON.", prompt or "Say hello in five words.", schema
        )
        return {"ok": res.ok, "latency_ms": res.latency_ms, "text": (res.data or {}).get("reply") or res.error or res.text}
