# CLAUDE.md — working agreement for AI agents on Vantage

Vantage is a local trading workstation for Alpaca: options scanner (long calls/puts), signal
engine (market data + order flow + news + events), auto-trader with hard risk limits, optional
local-LLM decision gate (Ollama/Qwen), and a low-latency React UI. **Real money can be at stake.**

## Read first
1. `docs/ARCHITECTURE.md` — module map, data flow, interfaces, modes, security.
2. `docs/API.md` — REST/WS contract. Backend and frontend must both match it.
3. `docs/QUANT.md` — the maths. Strategy/signal/scanner/risk code must match it.
4. `docs/adr/` — why the stack is what it is.

## Commands
```bash
# backend (from backend/)
uv sync                          # install
uv run vantage                   # start API on 127.0.0.1:8765 (mode from .env, default sim)
uv run pytest -q                 # tests (all run offline in SIM mode)
uv run ruff check . && uv run ruff format --check .

# frontend (from frontend/)
npm install
npm run dev                      # http://localhost:5173 (proxies /api and /ws to :8765)
npm run build                    # type-check + production bundle (must pass)

# both at once (from repo root)
./scripts/dev.ps1                # Windows
./scripts/dev.sh                 # Linux/macOS
```
Production-style single server: `npm run build` then `uv run vantage` serves `frontend/dist` at `/`.

## Non-negotiable rules
1. **Safety first.** Never weaken the live-trading interlocks (`ALLOW_LIVE_TRADING`, `confirm: "LIVE"`,
   auto-trader starts disarmed, risk checks on every order). Never add a code path that sends an
   order without going through `execution/risk.py` → `execution/engine.py`.
2. **The LLM can only veto or shrink.** Its `size_multiplier` is clamped to `[0, 1]`; it never adds
   trades the deterministic pipeline did not propose, and never overrides a risk rejection.
3. **No secrets in code, logs, API responses or the frontend.** Keys come from `.env` only.
4. **Don't block the event loop.** Network I/O is async (httpx/websockets). CPU-heavy work
   (chain scans, fits, backtests) goes through `asyncio.to_thread` or the process pool.
5. **`quant/` is pure.** No I/O, no globals, vectorised numpy, unit-tested against closed forms.
6. **Mode isolation.** Everything persisted carries `mode`; sim/paper/live never mix.
7. **Keep the contract.** Changing a payload means updating `docs/API.md`, `backend/vantage/api/`,
   and `frontend/src/lib/types.ts` together.
8. **Every trading decision is explainable** — emit a `Decision` with a human-readable `reason`.

## Code conventions
- Python 3.13, type hints everywhere, `from __future__ import annotations`, dataclasses with
  `slots=True` for hot-path types, ruff (line length 110). Timestamps are epoch **ms** ints (UTC);
  market-time logic uses `core/clock.py` (America/New_York).
- Logging via `logging.getLogger(__name__)`; no `print`.
- Errors from external services must be caught at the service boundary and surfaced as status /
  `Decision(action="error")`, never crash the loop.
- Frontend: TypeScript strict, functional components, zustand for state. High-frequency data
  (quotes, bars, equity ticks) is pushed into charts imperatively via `store.subscribe` — do not
  put tick data into React state that re-renders big trees. Colours/spacing only from
  `src/styles/tokens.css`.
- Tests: add/adjust pytest for any backend logic change; quant changes need a closed-form or
  simulation test.

## Where things go
| Task | Location |
|---|---|
| New signal model | `backend/vantage/signals/library.py` + register in `MODELS` + QUANT.md §6 |
| New strategy | `backend/vantage/strategies/library.py` + `REGISTRY` + QUANT.md §7 + test |
| New REST route | `backend/vantage/api/routes.py` + API.md + `frontend/src/lib/api.ts` |
| New WS channel | `backend/vantage/api/ws.py` + API.md + `frontend/src/lib/ws.ts` |
| Alpaca endpoint | `backend/vantage/broker/alpaca_rest.py` (only place that knows URLs) |
| Risk rule | `backend/vantage/execution/risk.py` + `tests/test_risk.py` |
| Strategy-controlled size / goal halt | `TradeIdea.target_fraction`; `Strategy.should_halt()` (see `strategies/rags.py`) |
| UI page | `frontend/src/pages/` + route in `App.tsx` + nav + Help page section |

## Gotchas
- Alpaca options stream is **msgpack only**; quotes can't use `*` wildcard (error 413).
- Free data = IEX + `indicative` options (delayed trades). Flow analytics fall back to snapshots.
- Free IEX stream: quotes + trades together are capped at ~30 symbols per connection (error `405 symbol limit
  exceeded`); bars/updatedBars are effectively unlimited; free plans allow ONE data-stream connection.
  `MarketDataService` streams bars for everything, gives the 30 quote slots to UI-viewed symbols first, skips
  trades on IEX and REST-polls snapshots every 10 s for the rest (polled quotes have zero sizes so OFI ignores them).
- `/ws` must send **text** frames (`send_text`). Binary frames were once silently dropped by the browser,
  killing every live update while REST kept the UI looking alive. Test clients must use `receive_json(mode="text")`.
- **No PDT rule.** FINRA eliminated the pattern-day-trader rule ($25k minimum, 3-in-5-days limit) effective
  2026-06-04 (Notice 26-10). Don't reintroduce PDT limits; day trades are unlimited unless the broker
  rejects one (legacy protection allowed until 2027-10-20) — see `is_pdt_rejection` in `execution/risk.py`.
- Portfolio history: `period=1M` is > 30 days and rejects intraday timeframes (use `30D`); `start` without
  `end` defaults to a 1-month window (always send both for YTD).
- Options orders: `day` TIF, whole contracts, no extended hours, no broker-side brackets
  (exits are software-managed in `autotrader/`).
- Portfolio history `period` > 30 days only supports `1D` timeframe.
- Windows: use `uv run`, not bare `python`; `uvloop` is unavailable (asyncio default loop is used).
- The SIM market is synthetic: never draw conclusions about strategy profitability from it.
- **Windows PowerShell 5.1 corrupts UTF-8 files** (`Get-Content -Raw` reads cp1252, `Set-Content -Encoding utf8`
  writes a BOM). Source files contain `—`, `σ`, `≤`. Edit with the Edit tool or Python (`read_text/write_text`
  with `encoding="utf-8"`), never PowerShell string replacement. A BOM in `pyproject.toml` breaks `uv`.
- Long options rarely clear the EV bar when IV > forecast vol (the variance risk premium). Option strategies
  abstaining is usually correct, not a bug — check the backtest "Funnel" note before loosening thresholds.
- First LLM call after Ollama starts can take 60–90 s (model load); the container warms the model on startup.
