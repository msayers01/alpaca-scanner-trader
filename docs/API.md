# Vantage — Backend ⇄ Frontend contract

Base URL (dev): backend `http://127.0.0.1:8765`. The Vite dev server (`:5173`) proxies `/api` and
`/ws` to it, so the frontend always uses relative URLs.

Conventions
- JSON everywhere (orjson). Timestamps are **epoch milliseconds** (`t`, `ts`). Dates are `YYYY-MM-DD`.
- Money is a JSON number in account currency (USD). Percentages are **fractions** (`0.034` = 3.4 %)
  unless the field name ends in `_pct_display`.
- Errors: HTTP 4xx/5xx with `{"error": "<code>", "detail": "<human text>"}`.
- Option symbols are OCC (`AAPL261120C00250000`).

---

## REST

### System
| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/api/status` | — | `Status` |
| POST | `/api/mode` | `{"mode": "sim"\|"paper"\|"live", "confirm"?: "LIVE"}` | `Status` |
| GET | `/api/settings` | — | `Settings` |
| PUT | `/api/settings` | partial `Settings` (editable fields only) | `Settings` |

```ts
type Mode = "sim" | "paper" | "live";
interface Status {
  mode: Mode; live_allowed: boolean; paper_keys: boolean; live_keys: boolean;
  market: { is_open: boolean; session: "pre" | "regular" | "post" | "closed";
            next_open: number; next_close: number; now: number };
  feeds: { stocks: string; options: string; news: boolean; flow_realtime: boolean;
           connected: Record<string, boolean> };
  autotrader: { armed: boolean; running: boolean; last_cycle: number | null; killed: boolean;
                kill_reason: string | null };
  llm: { enabled: boolean; available: boolean; model: string; base_url: string };
  version: string;
}
interface Settings {
  stock_feed: "iex" | "sip"; options_feed: "indicative" | "opra";
  universe: string[];            // symbols scanned by scanner/strategies
  watchlist: string[];
  ws_flush_ms: number;
}
```

### Account & portfolio
| Method | Path | Query/Body | Response |
|---|---|---|---|
| GET | `/api/account` | — | `Account` |
| GET | `/api/positions` | — | `Position[]` |
| POST | `/api/positions/{symbol}/close` | `{"pct"?: number}` (0–1, default 1) | `Order` |
| GET | `/api/orders` | `status=open\|closed\|all`, `limit` | `Order[]` |
| POST | `/api/orders` | `OrderRequest` | `Order` |
| DELETE | `/api/orders/{id}` | — | `{"ok": true}` |
| GET | `/api/portfolio/history` | `range=1D\|1W\|1M\|3M\|YTD\|1Y\|ALL` | `PortfolioHistory` |
| GET | `/api/journal` | `limit` | `JournalEntry[]` |

```ts
interface Account {
  equity: number; last_equity: number;      // last_equity = previous close equity (daily baseline)
  cash: number; buying_power: number; options_buying_power: number;
  daytrade_count: number; pattern_day_trader: boolean; daytrading_buying_power: number;
  day_pl: number; day_pl_pct: number;       // vs last_equity
  tier: "micro" | "small" | "growth" | "pro"; status: string; currency: string;
  options_level: number;
}
interface Position {
  symbol: string; asset_class: "equity" | "option"; underlying: string;
  qty: number; side: "long" | "short"; avg_entry_price: number; current_price: number;
  market_value: number; cost_basis: number; unrealized_pl: number; unrealized_plpc: number;
  change_today_pct: number; strategy_id: string | null;
  // options only:
  option?: { type: "call" | "put"; strike: number; expiration: string; dte: number;
             delta?: number; iv?: number };
  exit?: { stop_price?: number; target_price?: number; trail_pct?: number; time_stop?: number };
}
interface OrderRequest {
  symbol: string; side: "buy" | "sell"; qty?: number; notional?: number;
  type: "market" | "limit"; limit_price?: number; time_in_force?: "day" | "gtc";
  extended_hours?: boolean;
}
interface Order {
  id: string; client_order_id: string; symbol: string; asset_class: "equity" | "option";
  side: "buy" | "sell"; qty: number | null; notional: number | null; filled_qty: number;
  filled_avg_price: number | null; type: string; limit_price: number | null;
  status: string; submitted_at: number; filled_at: number | null; strategy_id: string | null;
}
interface PortfolioHistory {
  range: string; timeframe: string;
  base_value: number;                 // baseline: previous close for 1D, first point otherwise
  points: { t: number; v: number }[]; // equity
  session_bounds?: { open: number; close: number }; // 1D only, for extended-hours shading
}
interface JournalEntry {
  id: string; symbol: string; strategy_id: string; asset_class: string; direction: number;
  entry_t: number; exit_t: number | null; entry_price: number; exit_price: number | null;
  qty: number; pnl: number | null; pnl_pct: number | null; reason_exit: string | null;
}
```

### Market data
| Method | Path | Query | Response |
|---|---|---|---|
| GET | `/api/quote/{symbol}` | — | `QuoteSnap` |
| GET | `/api/bars` | `symbol`, `range=1D\|1W\|1M\|3M\|YTD\|1Y\|5Y` | `BarsResponse` |
| GET | `/api/search` | `q` | `{symbol, name, exchange}[]` (max 20) |
| GET | `/api/options/expirations` | `underlying` | `string[]` (dates) |
| GET | `/api/options/chain` | `underlying`, `expiration` | `Chain` |
| GET | `/api/options/bars` | `symbol` (OCC), `range=1D\|1W\|1M\|3M` | `BarsResponse` |

```ts
interface QuoteSnap { symbol: string; t: number; bid: number; ask: number; last: number;
  prev_close: number; change: number; change_pct: number; volume: number; }
interface Bar { t: number; o: number; h: number; l: number; c: number; v: number; }
interface BarsResponse { symbol: string; range: string; timeframe: string; prev_close: number | null;
  bars: Bar[]; }
interface OptionRow { symbol: string; type: "call" | "put"; strike: number; expiration: string;
  bid: number; ask: number; mid: number; last: number; volume: number; open_interest: number;
  iv: number | null; delta: number | null; gamma: number | null; theta: number | null;
  vega: number | null; }
interface Chain { underlying: string; spot: number; expiration: string; dte: number;
  calls: OptionRow[]; puts: OptionRow[]; atm_iv: number | null; expected_move: number | null; }
```

### Scanner
| Method | Path | Body/Query | Response |
|---|---|---|---|
| GET | `/api/scanner` | — | `ScanResult` (last run) |
| POST | `/api/scanner/run` | `Partial<ScanParams>` | `ScanResult` |
| GET | `/api/scanner/params` | — | `ParamSpec[]` |

```ts
interface ScanCandidate {
  contract: string; underlying: string; type: "call" | "put"; strike: number; expiration: string;
  dte: number; spot: number; bid: number; ask: number; mid: number; spread_pct: number;
  volume: number; open_interest: number; iv: number; delta: number; theta: number;
  iv_rank: number | null; iv_rv_ratio: number | null;
  forecast_return: number;     // expected underlying return over horizon (fraction)
  forecast_vol: number;        // annualised physical vol forecast
  ev_pct: number;              // expected return on premium at horizon, net of costs
  pop: number;                 // probability of profit at horizon
  kelly: number;               // full-Kelly fraction of equity
  breakeven: number; edge_score: number; reasons: string[];
}
interface ScanResult { t: number; duration_ms: number; params: Record<string, unknown>;
  candidates: ScanCandidate[]; errors: string[]; }
```

### Signals, news, flow
| Method | Path | Query | Response |
|---|---|---|---|
| GET | `/api/signals` | `symbol?` | `SignalRow[]` |
| GET | `/api/signals/forecast/{symbol}` | — | `Forecast` |
| GET | `/api/news` | `symbol?`, `limit` (≤200) | `NewsItem[]` |
| GET | `/api/flow` | `underlying?`, `limit` | `FlowEvent[]` |

```ts
interface SignalRow { model: string; symbol: string; score: number; confidence: number;
  horizon_days: number; t: number; meta: Record<string, unknown>; ic?: number | null; }
interface Forecast { symbol: string; t: number; horizon_days: number; exp_return: number;
  vol: number; prob_up: number; z: number; contributions: { model: string; weight: number;
  score: number }[]; }
interface NewsItem { id: string; t: number; headline: string; summary: string; source: string;
  url: string; symbols: string[]; sentiment: number | null; relevance: number | null;
  llm?: { direction: number; magnitude: number; horizon: string; rationale: string } | null; }
interface FlowEvent { t: number; contract: string; underlying: string; type: "call" | "put";
  strike: number; expiration: string; price: number; size: number; premium: number;
  side: "ask" | "bid" | "mid"; is_sweep: boolean; unusual_score: number; direction: number;
  source: "stream" | "snapshot"; }
```

### Intel (social, statements, prediction markets, VIX)
| Method | Path | Query/Body | Response |
|---|---|---|---|
| GET | `/api/intel/summary` | — | `IntelSummary` (everything the Intel page needs in one call) |
| GET | `/api/intel/items` | `kind?` (`social`\|`statement`), `symbol?`, `mover?`, `limit` (≤500) | `IntelItem[]` (newest first) |
| GET | `/api/intel/markets` | `limit` (≤200) | `PredictionMarket[]` |
| GET | `/api/intel/social` | `symbol?`, `limit` | `SocialStat[]`, or one `SocialStat \| null` when `symbol` is given |
| GET | `/api/intel/sources` | — | `{sources: IntelSource[], config: IntelConfig, enabled}` |
| PUT | `/api/intel/config` | partial `IntelConfig` (validated; feed URLs must be public http(s)) | same as `GET /api/intel/sources` |
| POST | `/api/intel/refresh` | `{source?}` | `{sources: IntelSource[]}` — polls now |

```ts
interface IntelItem { id: string; t: number; kind: "social" | "statement"; source: string; author: string;
  text: string; url: string; symbols: string[]; sentiment: number | null;   // [-1,1], + = bullish
  strength: number | null; engagement: number; themes: string[]; mover: string | null; weight: number;
  label: "bullish" | "bearish" | null;                                        // platform-tagged (Stocktwits)
  llm?: { direction: number; magnitude: number; horizon: string; rationale: string;
          per_symbol: Record<string, number>; latency_ms: number } | null; }
interface PredictionMarket { id: string; source: "polymarket" | "kalshi"; question: string; url: string;
  p: number; dp24: number; volume24h: number; liquidity: number; end_ms: number | null; themes: string[];
  polarity: -1 | 0 | 1;  // sign of a higher 'yes' probability for US equities (0 = volatility only)
  weight: number; t: number; }
interface VixRegime { vix: number; prev_close: number | null; chg_1d: number; z_chg: number; chg_5d: number;
  pct: number; term: number | null; near: number | null; vvix: number | null; skew: number | null;
  label: "calm" | "normal" | "elevated" | "stressed" | "panic"; risk_on: number; inverted: boolean; t: number; }
interface MacroState { t: number; vix: VixRegime | null; risk_on: number; event_risk: number;   // [-1,1], [0,1]
  prediction: { tilt: number; event_risk: number; n: number; top: { question: string; p: number; dp24: number }[] };
  statements: { tilt: number; n_eff: number }; }
interface SocialStat { symbol: string; n: number; n_eff: number; bull: number; bear: number; polarity: number;
  attention: number; mentions_24h: number | null; mentions_prev: number | null; rank: number | null;
  labelled: number; crowded: boolean; sources: Record<string, number>; score_z: number; confidence: number;
  in_universe?: boolean; }
interface IntelSource { id: string; label: string; kind: "social" | "statement" | "prediction" | "volatility";
  enabled: boolean; configured: boolean; needs: string | null; note: string; interval_s: number;
  last_poll: number | null; last_ok: number | null; last_error: string | null; n_total: number; n_last: number; }
interface IntelConfig { enabled: boolean; sources: Record<string, boolean>; subreddits: string[];
  x_handles: string[]; movers: { name: string; weight: number; x?: string; query?: string; enabled?: boolean }[];
  rss: { id: string; label?: string; url: string; mover?: string; weight?: number; enabled?: boolean }[]; }
interface IntelSummary { enabled: boolean; macro: MacroState; markets: PredictionMarket[]; statements: IntelItem[];
  social: SocialStat[]; sources: IntelSource[]; }
```
`Status.intel` = `{enabled, vix, vix_chg, regime, sources_ok, sources_on}` (drives the top-bar VIX chip). Credentials
(`REDDIT_CLIENT_ID/SECRET`, `X_BEARER_TOKEN`) are read from `.env` only; the API exposes just `configured: boolean`.
`intel` items may be re-sent with the same `id` after LLM enrichment (`llm` populated) — clients replace by `id`.

### Calendar
| Method | Path | Query/Body | Response |
|---|---|---|---|
| GET | `/api/calendar` | `start`, `end` (dates) | `CalEvent[]` |
| POST | `/api/calendar` | `{date, time?, title, kind, symbol?, importance}` | `CalEvent` |
| DELETE | `/api/calendar/{id}` | — | `{"ok": true}` |

```ts
interface CalEvent { id: string; date: string; time: string | null;   // "HH:MM" ET or null
  kind: "earnings" | "fomc" | "macro" | "opex" | "holiday" | "early_close" | "user";
  title: string; symbol: string | null; importance: 1 | 2 | 3;
  meta: Record<string, unknown>; source: string; }
```

### Strategies & auto-trader
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/strategies` | — | `StrategyInfo[]` |
| PUT | `/api/strategies/{id}` | `{enabled?, params?, use_llm_gate?}` | `StrategyInfo` |
| GET | `/api/autotrader` | — | `AutoTraderState` |
| PUT | `/api/autotrader/risk` | `Partial<RiskConfig>` | `AutoTraderState` |
| POST | `/api/autotrader/preset` | `{"tier": Tier}` | `AutoTraderState` |
| POST | `/api/autotrader/arm` | `{"armed": boolean, "confirm"?: "LIVE"}` | `AutoTraderState` |
| POST | `/api/autotrader/kill` | `{"flatten": boolean}` | `AutoTraderState` |
| POST | `/api/autotrader/reset-kill` | — | `AutoTraderState` |
| POST | `/api/autotrader/cycle` | — | `AutoTraderState` (run one cycle now) |
| POST | `/api/tools/goal-odds` | `{start, goal, floor, position_frac, stop_frac, reward_r, edge_pp?, edge_sd?, win_rate?, cost_frac?, stop_slippage?, trades_per_day?, max_days?}` | `GoalOdds` (`p_goal, p_floor, p_unresolved, median_trades_to_goal, median_days_to_goal?, win_rate, baseline_win_rate, breakeven_win_rate, p_negative_edge, win_multiplier, loss_multiplier, expectancy_pct, log_growth_per_trade, kelly_fraction, kelly_ratio, max_trades`) |
| GET | `/api/autotrader/decisions` | `limit` | `Decision[]` |

```ts
interface ParamSpec { name: string; type: "float" | "int" | "bool" | "enum" | "str";
  default: unknown; min?: number; max?: number; step?: number; options?: string[];
  label: string; help: string; group: string; }
interface StrategyInfo { id: string; name: string; description: string;
  asset_class: "option" | "equity"; style: "intraday" | "swing"; enabled: boolean;
  use_llm_gate: boolean; params: Record<string, unknown>; schema: ParamSpec[];
  tiers: string[]; stats: { trades: number; win_rate: number | null; pnl: number };
  backtest: { support: "full" | "synthetic" | "none"; note: string };
  runtime: { start_equity?: number | null; started_t?: number; halted?: string | null; halted_t?: number | null };
  extra: Record<string, unknown>  // rags_to_riches: {effective, goal_equity, floor_equity, start_equity, halted}
}
interface RiskConfig { kelly_multiplier: number; max_risk_per_trade: number; max_position_pct: number;
  max_open_positions: number; max_per_underlying: number; max_options_allocation: number;
  max_daily_loss: number; max_drawdown: number; max_trades_per_day: number;
  min_option_volume: number; min_open_interest: number; max_spread_pct: number;
  allow_daytrades: "auto" | "never" | "always"; entry_start_min: number; entry_end_min: number;
  earnings_blackout_days: number; cycle_seconds: number; llm_enabled: boolean;
  on_llm_failure: "skip" | "approve"; aggression: number; order_timeout_s: number; }
interface AutoTraderState { armed: boolean; running: boolean; killed: boolean;
  kill_reason: string | null; tier: string; risk: RiskConfig; risk_schema: ParamSpec[];
  last_cycle: number | null; cycle_ms: number | null; today: { trades: number; pnl: number };
  presets: { tier: string; label: string; description: string; min_equity: number;
             max_equity: number | null; manual?: boolean }[]; }  // manual = never auto-selected (e.g. "rags")
interface Decision { id: string; t: number; strategy_id: string; symbol: string;
  action: "enter" | "exit" | "skip" | "reject" | "error";
  reason: string; edge_score: number | null; llm: { decision: string; confidence: number;
  size_multiplier: number; rationale: string; latency_ms: number } | null;
  order_id: string | null; }
```

### LLM
| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/llm` | — | `{enabled, available, base_url, model, models: string[], stats}` |
| PUT | `/api/llm` | `{enabled?, model?, base_url?, temperature?}` | same |
| POST | `/api/llm/test` | `{prompt?}` | `{ok, latency_ms, text}` |
| GET | `/api/llm/calls` | `limit` (≤1000), `purpose?` (`gate`\|`news`\|`analyst`\|`test`), `status?` (`ok`\|`fail`\|`cached`), `q?` (full-text over prompts/replies), `before?` (ms cursor) | `{calls: LlmCallSummary[], by_purpose: {purpose, n, fails, avg_ms}[]}` |
| GET | `/api/llm/calls/{id}` | — | `LlmCallDetail` |

```ts
interface LlmCallSummary { id: string; t: number; purpose: string; model: string; latency_ms: number;
  ok: boolean; cached: boolean; prompt_tokens: number | null; completion_tokens: number | null;
  error: string | null; preview: string; }            // also the payload of the `llm` WS channel
interface LlmCallDetail extends Omit<LlmCallSummary, "preview"> {
  system: string | null; user: string; response: string; parsed: Record<string, unknown> | null; }
```
Calls are persisted in `llm_calls` (per mode, up to 60,000 chars per prompt/reply); cache hits are logged with
`cached: true` and `latency_ms: 0`.

### Backtest
| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/backtest` | `{strategy_id, symbols: string[], start, end, params?, initial_equity?, iv_rv?}` (iv_rv = assumed implied/realised vol ratio for synthetic option pricing, default 1.10); `sizing?`: `equal_weight` (default — each symbol gets 1/N of equity; options ≤ 20 % premium per trade) or `live` (the auto-trader's risk settings). Strategies with `backtest.support = "none"` return 400 with the reason | `BacktestResult` |

```ts
interface BacktestResult { strategy_id: string; equity: { t: number; v: number }[];
  benchmark: { t: number; v: number }[];
  metrics: { cagr: number; sharpe: number; sortino: number; max_drawdown: number; calmar: number;
    win_rate: number; profit_factor: number; trades: number; exposure: number;
    psr: number; dsr: number; };
  trades: JournalEntry[]; notes: string[]; sizing: "equal_weight" | "live";
  funnel: { ideas: number; entered: number; top_rejections: Record<string, number> }; }
```

### Additions (implemented in v1, optional for clients)
| Method | Path | Body / query | Response |
|---|---|---|---|
| POST | `/api/sim/reset` | `{"equity": number}` ($10–$10M) — SIM mode only; resets cash, positions, journal | `Account` |
| GET | `/api/signals/models` | — | `{id, name, horizon_days, kind, directional, description, enabled, prior_weight, weight, ic, ic_sd, ic_days, ic_shrunk}[]` |

Extra fields the backend also returns:
- `Status.error` (last account-poll error or null), `Status.mode_error` (why a requested mode failed to start; the
  backend falls back to SIM), `Status.feeds.errors` (per-stream last error).
- `PortfolioHistory.points` always end at live equity (paper/live). For 1D, if the broker's bars are from an
  earlier ET date (weekend / overnight session), `base_value` = the account's `last_equity`, so the chart's
  "Today" change equals `Account.day_pl`.
- `PortfolioHistory.day_bounds` (1D only): `{start, end}` — the full session window (04:00–20:00 ET live; the sim
  "day") to pad the x-axis; `session_bounds` = regular hours.
- `AutoTraderState.preview` (bool; when disarmed, would-be entries are logged as `skip` decisions prefixed
  `preview (disarmed):`), `tier_locked` (bool; false = tier follows equity automatically),
  `manage_exits_when_disarmed` (bool). `PUT /api/autotrader/risk` also accepts these two booleans.
  `POST /api/autotrader/preset` accepts `{"tier": "auto"}` to unlock automatic tier selection.
- `orders` channel payloads add `event` (`new`, `fill`, `partial_fill`, `canceled`, `replaced`, `rejected`, …),
  `fill_price`, `fill_qty`, `event_t`.
- `news` channel: an item may be sent again with the same `id` once LLM enrichment completes
  (`llm` populated, `sentiment` updated). Clients replace by `id`.
- `Position.exit` is populated for positions opened by the auto-trader.
- `ScanCandidate.entry_price`: the modelled entry fill (mid + aggression × half-spread).

---

## WebSocket `/ws`

Client → server
```json
{"op": "sub",   "ch": ["quote:AAPL", "account", "news"]}
{"op": "unsub", "ch": ["quote:AAPL"]}
{"op": "ping",  "id": 1}
```
Server → client: always a batch frame
```json
{"b": [ {"ch": "quote:AAPL", "d": {…}}, {"ch": "news", "d": {…}} ], "ts": 1760000000000}
```
and `{"pong": 1, "ts": …}` for pings.

| Channel | Payload | Conflated? |
|---|---|---|
| `quote:<SYM>` | `{symbol, t, bid, ask, last, change_pct}` | yes (latest only) |
| `bar:<SYM>` | `Bar` (1-minute, may repeat the same `t` with updated values) | yes, per `t` |
| `oquote:<OCC>` | `{symbol, t, bid, ask, mid}` | yes |
| `account` | `Account` | yes |
| `positions` | `Position[]` (full list) | yes |
| `orders` | `Order` (each update) | no |
| `equity` | `{t, v}` live equity tick for the portfolio chart | yes |
| `news` | `NewsItem` | no |
| `flow` | `FlowEvent` | no |
| `signals` | `SignalRow` | no (rate-limited server-side) |
| `decisions` | `Decision` | no |
| `llm` | `LlmCallSummary` (each LLM exchange as it completes) | no |
| `status` | `Status` | yes |
| `scanner` | `ScanResult` (on each completed scan) | yes |
| `intel` | `IntelItem` (new post/statement, or re-sent after LLM enrichment) | no |
| `macro` | `MacroState` (on every VIX / prediction-market / statement change, ≤ 1 per 2 s) | yes |

The server sends `status` immediately after connect. Clients re-subscribe after reconnect
(`ws.ts` does this automatically).
