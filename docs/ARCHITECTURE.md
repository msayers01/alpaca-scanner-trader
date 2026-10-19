# Vantage — Architecture

Vantage is a single-user, locally hosted trading workstation for an Alpaca account: an options
scanner for long calls/puts, a signal engine that fuses market data, order flow, news and events,
an automated trader with institutional-style risk management, an optional local-LLM decision gate,
and a low-latency browser UI.

Read this with:
- [`API.md`](API.md) — REST + WebSocket contract between backend and frontend (source of truth).
- [`QUANT.md`](QUANT.md) — the mathematics: pricing, volatility, signals, sizing, strategies.
- [`adr/`](adr/) — architecture decision records.

---

## 1. System overview

```
                         ┌──────────────────────────── backend (Python, one asyncio process) ───────────────────────────┐
 Alpaca Trading REST ◄──►│ broker/alpaca_rest.py ──┐                                                                    │
 Alpaca trade_updates WS►│ broker/alpaca_stream.py │      ┌─────────────┐   ┌──────────────┐   ┌───────────────┐         │
 Alpaca stocks WS ──────►│   (stocks, options,     ├─────►│ core/bus.py │──►│ data/*       │──►│ signals/*     │         │
 Alpaca OPRA WS (msgpk)─►│    news, trade_updates) │      │  (pub/sub)  │   │ market, news │   │ engine +      │         │
 Alpaca news WS ────────►│ broker/sim.py (SIM mode)┘      └──────┬──────┘   │ flow, calendar│  │ ensemble      │         │
 Nasdaq/Finnhub (cal) ──►│                                       │          └──────────────┘   └──────┬────────┘         │
 Ollama (Qwen) ◄────────►│ llm/gateway.py ◄──────────────────────┼──────────────┐                     │                  │
                         │                                       │       ┌──────┴────────────┐  ┌─────▼──────────┐        │
                         │ api/ (FastAPI REST + /ws hub) ◄───────┤       │ autotrader/engine │◄─┤ strategies/*   │        │
                         │                                       │       │  ideas→LLM gate→  │  │ scanner/       │        │
                         │ core/store.py (SQLite WAL)            │       │  risk→execution   │  └────────────────┘        │
                         └───────────────────────────────────────┼───────┴────────┬──────────┴────────────────────────────┘
                                                                 │                │ execution/engine.py (smart limit orders)
                                     browser ◄── /ws + /api ─────┘                ▼
                              React + lightweight-charts                     Alpaca orders
```

Everything runs in **one asyncio event loop** in one process. Long CPU work (chain scans,
backtests, GARCH fits) runs in a thread/process pool via `asyncio.to_thread` / executor so the loop
never stalls. SQLite writes go through a single writer thread.

## 2. Repository layout

```
CLAUDE.md                 Working agreement for AI agents (start here)
README.md                 Human quick start
.env.example              All configuration keys (copy to .env)
docs/                     ARCHITECTURE.md, API.md, QUANT.md, adr/
scripts/                  dev.ps1 / dev.sh — run backend + frontend together
backend/
  pyproject.toml          uv-managed; `uv run pytest`, `uv run vantage`
  vantage/
    main.py               FastAPI app factory + lifespan (wires services), `run()` entry point
    config.py             Settings (pydantic-settings, .env)
    container.py          Service container: builds/tears down everything for a trading mode
    core/
      types.py            Domain dataclasses (Bar, Quote, Trade, OptionContract, Signal, NewsItem,
                          FlowEvent, TradeIdea, Decision, ParamSpec …) — the shared vocabulary
      bus.py              In-process async pub/sub with bounded, drop-oldest queues for market data
      clock.py            US/Eastern market clock, sessions, trading-day maths
      store.py            SQLite persistence (WAL, single writer thread)
      util.py             ids, time helpers, orjson helpers
    broker/
      base.py             Broker protocol (account, positions, orders, history, market data)
      alpaca_rest.py      Async REST client: trading + market data + news + screener
      alpaca_stream.py    Async WS clients: stocks (json), options (msgpack), news, trade_updates
      sim.py              SimBroker + SimMarket: synthetic GBM/jump market, fills, portfolio history
    data/
      market.py           MarketDataService: quotes/bars cache, subscriptions, snapshots, history
      options.py          OptionsService: chains (snapshots), contract metadata, IV history
      news.py             NewsService: ingest, dedupe, lexicon + LLM sentiment → signals
      flow.py             FlowService: options trade classification, sweeps, unusual activity, OFI
      calendar.py         CalendarService: earnings (Nasdaq/Finnhub), FOMC, OPEX, holidays, user events
    quant/                Pure math, no I/O (see QUANT.md): bs, vol, stats, kelly, option_ev,
                          indicators, filters, microstructure
    signals/
      base.py             SignalModel interface
      library.py          Concrete signal models (momentum, mean-reversion, trend, vol premium, flow,
                          news, earnings, relative strength …)
      engine.py           SignalEngine: runs models on schedule, keeps latest per (symbol, model),
                          tracks realised IC online, produces the ensemble forecast
    scanner/
      options_scanner.py  Ranks long calls/puts by physical-measure EV, Kelly, liquidity
    strategies/
      base.py             Strategy ABC, StrategyContext, ExitPolicy
      library.py          Preset strategies
      presets.py          Account tiers + per-tier strategy/risk presets
    execution/
      risk.py             RiskManager: pre-trade checks, sizing, PDT guard, kill switch
      engine.py           ExecutionEngine: smart limit orders (mid → ladder), cancel/replace, fills
    autotrader/
      engine.py           AutoTrader loop: universe → ideas → LLM gate → risk → execute; exits
    llm/
      gateway.py          OpenAI/Ollama-compatible client, JSON-schema output, cache, concurrency
      prompts.py          Prompt templates (trade gate, news → signal, earnings → signal)
    backtest/
      engine.py           Event-driven bar backtester (same Strategy objects) + metrics
    api/
      routes.py           REST endpoints (see API.md)
      ws.py               WebSocket hub: channel subscriptions, conflation, backpressure
  tests/                  pytest suite (quant correctness, risk, strategies, API smoke)
frontend/
  src/
    lib/                  api.ts (REST), ws.ts (multiplexed socket), format.ts, types.ts
    store/                zustand stores (market, account, feeds, ui)
    components/charts/    PortfolioChart, PriceChart, OptionChart (lightweight-charts)
    components/           shared UI (tables, tickets, badges)
    pages/                Dashboard, Symbol, Scanner, Feeds, AutoTrader, Calendar, Backtest,
                          Settings, Help
    styles/               tokens.css, global.css
data/                     (git-ignored) vantage.db, caches, logs
```

## 3. Trading modes

| Mode | Broker | Market data | Keys needed | Use |
|---|---|---|---|---|
| `sim`  | `SimBroker` | `SimMarket` synthetic ticks/bars/chains/news | none | development, demos, tests |
| `paper`| Alpaca paper (`paper-api.alpaca.markets`) | Alpaca data API | `ALPACA_PAPER_KEY/SECRET` | forward testing |
| `live` | Alpaca live (`api.alpaca.markets`) | Alpaca data API | `ALPACA_LIVE_KEY/SECRET` + `ALLOW_LIVE_TRADING=true` | real money |

- The active mode is held by `Container` (`container.py`). Switching mode (`POST /api/mode`) stops
  the auto-trader, closes streams, and rebuilds broker + services for the new mode.
- **Live safety interlocks** (all must pass): `ALLOW_LIVE_TRADING=true` in `.env`; UI sends
  `confirm: "LIVE"`; the auto-trader always starts **disarmed** after a restart or mode switch;
  every live order passes the same `RiskManager` as paper.
- Startup mode = `VANTAGE_MODE` (default `sim`, or `paper` if paper keys exist).

## 4. Event bus and data flow

`core/bus.py` provides `Bus.publish(topic, payload)` and `Bus.subscribe(topic_pattern) -> Subscription`
(async iterator). Topics:

| Topic | Payload | Producer | Notes |
|---|---|---|---|
| `quote.<SYM>` | `Quote` | market | high rate, conflated for UI |
| `trade.<SYM>` | `Trade` | market | |
| `bar.<SYM>` | `Bar` (1Min) | market | |
| `oquote.<OCC>` / `otrade.<OCC>` | `Quote` / `Trade` | options stream | OCC = option symbol |
| `news` | `NewsItem` | news | |
| `flow` | `FlowEvent` | flow | |
| `signal` | `Signal` | signal engine | |
| `decision` | `Decision` | autotrader | |
| `order` | order dict | broker trade_updates | **never dropped** |
| `account` | account dict | account poller | |
| `status` | status dict | container | |

Market-data subscriptions use bounded queues with *drop-oldest*: a slow consumer can never
back-pressure ingestion. Order/account topics use unbounded queues.

## 5. Low-latency UI path

1. Stream message arrives → decoded (orjson / msgpack) into a slotted dataclass → `bus.publish`.
2. `api/ws.py` hub holds, per client, a dict of *latest value per channel key*. A flush task sends
   pending values every `WS_FLUSH_MS` (default 50 ms; ≤ 20 msgs/s per key) as a single batched
   frame `{"b": [...]}`. Feeds (news/flow/decisions/orders) are never conflated, only batched.
3. Browser `ws.ts` decodes and dispatches to zustand stores. Chart components subscribe with
   `store.subscribe` outside React and call `series.update()` directly — **no React render per tick**.
4. Lists (news, flow, signals) are windowed (render only visible rows).

Order path: strategy → risk → `ExecutionEngine.submit` → pooled keep-alive HTTPS → Alpaca. No
disk I/O on the critical path (journal writes are queued to the writer thread).

## 6. Core interfaces

### 6.1 Domain types (`core/types.py`)
All timestamps are **epoch milliseconds (int, UTC)** inside the backend and on the wire.
Option symbols use the OCC format Alpaca uses: `AAPL261120C00250000`.

Key types (abridged — see source for every field):
- `Bar(symbol, t, o, h, l, c, v, vw=0.0, n=0)`
- `Quote(symbol, t, bid, ask, bid_size, ask_size)` with `.mid`, `.spread`, `.spread_pct`
- `Trade(symbol, t, price, size, exchange, conditions)`
- `OptionContract(symbol, underlying, type, strike, expiration, bid, ask, last, volume, open_interest,
  iv, delta, gamma, theta, vega, t)` with `.mid`, `.dte`
- `Signal(model, symbol, score[-1,1], confidence[0,1], horizon_days, t, meta)`
- `NewsItem(id, t, headline, summary, source, url, symbols, sentiment, relevance, llm)`
- `FlowEvent(t, contract, underlying, type, strike, expiration, price, size, premium, side,
  aggressor, is_sweep, unusual_score, direction)`
- `TradeIdea(strategy_id, symbol, underlying, asset_class, direction, contract, limit_price, stop_pct,
  target_pct, trail_pct, horizon_days, expected_return, win_prob, kelly_fraction, edge_score,
  rationale, signals, use_llm_gate)`
- `Decision(id, t, strategy_id, symbol, action, reason, idea, llm, order_id)`
- `ParamSpec(name, type, default, min, max, step, options, label, help, group)` — drives the UI's
  auto-generated parameter forms.

### 6.2 Strategy (`strategies/base.py`)
```python
class Strategy(ABC):
    id: ClassVar[str]; name: ClassVar[str]; description: ClassVar[str]
    asset_class: ClassVar[str]           # "option" | "equity"
    style: ClassVar[str]                 # "intraday" | "swing"
    params_schema: ClassVar[list[ParamSpec]]
    def __init__(self, params: dict | None = None): ...
    async def generate(self, ctx: StrategyContext) -> list[TradeIdea]: ...   # entries
    async def manage(self, ctx: StrategyContext, pos: ManagedPosition) -> ExitSignal | None: ...
```
`StrategyContext` exposes read-only services (market, options, signals, scanner, news, flow,
calendar), the account snapshot and tier, the risk config, `now`, and the LLM gateway. Strategies
**never place orders**; they emit `TradeIdea`s. Default `manage()` applies the shared `ExitPolicy`
(stop, target, trailing, time stop, DTE floor, pre-earnings exit, signal-reversal exit).

Adding a strategy = subclass in `strategies/library.py`, add to `REGISTRY`, document in QUANT.md §6,
add a test in `tests/test_strategies.py`.

### 6.3 Auto-trader cycle (`autotrader/engine.py`)
Every `cycle_seconds` (default 30 s during market hours):
1. Refresh account; compute tier; check kill switch (daily loss, drawdown, error rate).
2. **Manage** open positions: for each `ManagedPosition`, the owning strategy's `manage()` → exits.
3. **Generate**: each enabled strategy → `TradeIdea`s.
4. De-duplicate and rank ideas by `edge_score`; drop ideas for symbols already held.
5. **LLM gate** (if `idea.use_llm_gate` and LLM enabled): send a compact dossier; the LLM may
   `approve`, `reject` or `reduce` (size multiplier ≤ 1). **The LLM can never increase size or
   bypass risk.** Timeout/failure → configurable `on_llm_failure` (`skip` default, or `approve`).
6. **Risk**: `RiskManager.size_and_check(idea)` → `OrderPlan` or rejection reason.
7. **Execute**: `ExecutionEngine` places a limit at mid (or mid + aggression × half-spread),
   re-prices up a ladder every few seconds, cancels after timeout.
8. Every step emits a `Decision` (persisted + streamed) so the UI shows *why* each action happened.

### 6.4 Risk manager (`execution/risk.py`)
Hard checks (any failure = reject): mode/arming, market session & entry windows, max open
positions, max positions per underlying, per-trade risk budget, max position % of equity, max
options allocation %, buying power, daily trade cap, daily-loss kill switch, drawdown kill switch,
PDT guard (uses the broker's `daytrade_count` / `pattern_day_trader` / equity fields rather than
hard-coding the rule, so it stays correct if FINRA rules change), liquidity (spread %, OI, volume),
earnings blackout, duplicate-order guard.
Sizing: `contracts = floor(min(kelly_f × κ × equity, risk_budget, max_position) / (premium × 100))`
with κ the fractional-Kelly multiplier; equities use notional (fractional shares) for small accounts.

## 7. Persistence (`core/store.py`)

SQLite file `data/vantage.db`, WAL mode. Tables: `kv` (settings), `strategy_config`,
`risk_config`, `decisions`, `orders`, `positions_meta` (strategy ownership, entry signals, stops),
`journal` (closed trades + attribution), `news`, `flow`, `signals_log` (for online IC),
`iv_history` (daily ATM IV per underlying), `events` (user calendar events), `llm_calls`.
Schema is created/migrated idempotently at startup (`Store.migrate()`). Every table includes the
`mode` column so sim/paper/live data never mix.

## 8. Configuration (`.env`)
See `.env.example` for the authoritative list. Highlights: `VANTAGE_MODE`, `ALPACA_PAPER_KEY`,
`ALPACA_PAPER_SECRET`, `ALPACA_LIVE_KEY`, `ALPACA_LIVE_SECRET`, `ALLOW_LIVE_TRADING`,
`ALPACA_STOCK_FEED` (`iex`|`sip`), `ALPACA_OPTIONS_FEED` (`indicative`|`opra`), `LLM_BASE_URL`
(default `http://127.0.0.1:11434`), `LLM_MODEL`, `FINNHUB_API_KEY` (optional), `HOST`/`PORT`
(default `127.0.0.1:8765`), `WS_FLUSH_MS`, `UNIVERSE`.

## 9. Security
- Server binds to `127.0.0.1` only. CORS + an Origin check middleware allow only the local UI
  origins; mutating requests with a foreign `Origin` are rejected (blocks drive-by CSRF from
  other websites).
- API keys live only in `.env` on the backend; the UI only sees booleans (`paper_keys: true`).
- `.env`, `data/` are git-ignored.

## 10. Data entitlements and graceful degradation
Free Alpaca market data = IEX stocks + `indicative` options (delayed trades, modified quotes).
Algo Trader Plus = SIP + OPRA. Every service degrades gracefully:
- Flow analytics need real-time OPRA trades for sweep detection; on `indicative` the flow service
  falls back to **snapshot-based unusual activity** (volume/OI, premium, IV jumps) polled each minute.
- Quotes on IEX are a subset of the NBBO; spreads look wider — liquidity filters account for it.
- No LLM running → LLM-gated ideas follow `on_llm_failure`; news uses the lexicon scorer.
- No Finnhub key → earnings from Nasdaq's public calendar; macro events from the built-in FOMC
  table + computed OPEX/holidays + user-entered events.

## 11. Testing
- `backend/tests/test_quant_*.py`: closed-form identities (put-call parity, IV round-trip, Greeks
  vs finite differences, Kelly analytics, vol estimators on simulated GBM, PSR/DSR known values).
- `test_risk.py`, `test_strategies.py`, `test_autotrader.py`: run against SIM mode.
- `test_api.py`: FastAPI TestClient smoke tests of every route in API.md.
- Frontend: `npm run build` (type-check + bundle) must pass.

## 12. Roadmap (post v1)
Options backtesting on historical OPRA bars; multi-leg spreads (debit verticals) as a cheaper
expression of directional views; walk-forward parameter optimisation with DSR gating;
per-strategy capital allocation via online mean-variance of strategy returns; alerts
(desktop/phone push); dark-pool prints (requires a third-party feed).
