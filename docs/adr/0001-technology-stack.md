# ADR 0001 — Technology stack

- Status: Accepted
- Date: 2026-10-18

## Context

Vantage is a single-user, locally hosted trading workstation linked to an Alpaca
brokerage account. Requirements that drive the stack:

1. Quantitative work (option pricing, volatility models, statistics, optimisation) must be
   rigorous and fast enough to scan full option chains for dozens of underlyings every minute.
2. Real-time market data, news and order-flow streams must be ingested concurrently with
   order management, without one blocking another.
3. The browser UI must feel "ultra low latency": ticks visible within one animation frame of
   arriving at the server, charts that pan/zoom at 60 fps, no React re-render per tick.
4. A local LLM (Qwen via Ollama on an RTX 5080) is an optional decision layer.
5. Another LLM must be able to pick the project up from the docs alone.
6. Runs on Windows 11 (developer box), must also run on Linux/macOS.

## Decision

### Backend — Python 3.13, asyncio, FastAPI
| Concern | Choice | Why |
|---|---|---|
| Runtime | CPython 3.14 (≥ 3.12 supported) managed by **uv** | numpy 2.5 / scipy 1.18 wheels available; uv gives reproducible, fast envs. Pinned in `backend/.python-version`. |
| Web/API | **FastAPI** + **uvicorn** | Async-native, WebSocket support, pydantic validation, OpenAPI docs for free. |
| Serialization | **orjson** (JSON), **msgpack** (Alpaca options stream) | orjson is ~10x stdlib json; Alpaca OPRA stream is msgpack-only. |
| Broker/data I/O | Own thin async clients on **httpx** (REST, keep-alive pool) and **websockets** (streams) | `alpaca-py` REST is synchronous (would block the event loop) and its streams build a pydantic model per message (too slow for OPRA). Own clients give reconnect/backoff, dynamic subscriptions and zero-copy dict handling. |
| Numerics | **numpy**, **scipy** | Vectorised Black-Scholes over whole chains, Gauss-Hermite quadrature, MLE fits, bounded optimisation for Kelly. |
| Persistence | **SQLite** (WAL) via stdlib `sqlite3` on a dedicated thread | Zero-ops, durable, plenty for one user. Journal, configs, IV history, news, decisions. |
| Settings | **pydantic-settings** reading `.env` | Typed config, secrets never leave the backend. |
| Tests/lint | **pytest**, **pytest-asyncio**, **ruff** | Standard. |

Python is not the lowest-latency language, but the end-to-end order latency to Alpaca is dominated by
network RTT (tens of ms) and Alpaca's own matching path; in-process overhead of a tuned asyncio
service is < 1 ms. Python's quant ecosystem is decisive. Hot loops (chain pricing) are vectorised.

### Frontend — React 19 + TypeScript + Vite
| Concern | Choice | Why |
|---|---|---|
| Framework | **React 19**, **TypeScript**, **Vite** | Fast HMR, small bundles, mainstream so other agents know it. |
| Charts | **TradingView lightweight-charts v5** | Canvas, ~45 KB, built for financial series, incremental `update()`, smooth pan/zoom. Styled to match Robinhood. |
| State | **zustand** | Selector subscriptions + `subscribe()` outside React so ticks update charts imperatively without re-renders. |
| Routing | **react-router** | Standard. |
| Styling | Hand-written CSS with design tokens (`src/styles/tokens.css`) | Full control of a Robinhood-like dark look; no utility-framework default aesthetic. |
| Transport | One multiplexed **WebSocket** (`/ws`) + REST (`/api`) | One socket, channel subscriptions, server-side conflation. |

### LLM — Ollama (OpenAI-compatible + native `/api/chat` with JSON-schema `format`)
Provider-agnostic gateway: anything speaking the OpenAI chat-completions protocol (Ollama, LM Studio,
vLLM, llama.cpp server) works. Default model `qwen3.6:35b-a3b` (MoE, ~3B active params → fast on a
16 GB GPU with offload). Structured output is enforced with a JSON schema and validated with pydantic.

## Consequences

- No dependency on `alpaca-py`; we own ~600 lines of client code and must track Alpaca API changes
  (endpoints are centralised in `backend/vantage/broker/alpaca_rest.py` and `alpaca_stream.py`).
- A **SIM** mode (synthetic market + simulated broker) exists so the whole system runs and is testable
  with no keys and no network.
- Single process. If scanning load ever outgrows one core, the scanner can move to a
  `ProcessPoolExecutor` without API changes (it is a pure function of snapshots).
