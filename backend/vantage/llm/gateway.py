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


def _validate(data: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Minimal JSON-schema check (required keys, enums, number bounds) — enough to reject garbage."""
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
        if t == "number" and not isinstance(v, (int, float)):
            return f"{k} not a number"
        if t == "number":
            if "minimum" in spec and v < spec["minimum"]:
                data[k] = spec["minimum"]
            if "maximum" in spec and v > spec["maximum"]:
                data[k] = spec["maximum"]
        if t == "array" and not isinstance(v, list):
            return f"{k} not an array"
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
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.store = store
        self.mode = mode
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
                return LLMResult(res.ok, res.data, res.text, res.latency_ms, res.error, cached=True)
        self.stats.calls += 1
        self.stats.by_purpose[purpose] = self.stats.by_purpose.get(purpose, 0) + 1
        t0 = time.perf_counter()
        try:
            async with self._sem:
                text = await asyncio.wait_for(self._call(system, user, schema, max_tokens), self.timeout_s)
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
        if self.store:
            self.store.write(
                "INSERT INTO llm_calls(id,mode,t,purpose,model,latency_ms,ok,request,response) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id(), self.mode, now_ms(), purpose, self.model, res.latency_ms, int(res.ok), user[:8000],
                 (res.text or res.error or "")[:8000]),
            )
        return res

    async def _call(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
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
            return r.json()["message"]["content"]
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
        return r.json()["choices"][0]["message"]["content"]

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
