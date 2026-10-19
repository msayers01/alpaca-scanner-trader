// Wire types — mirror docs/API.md exactly. Change API.md, the backend and this file together.
// Timestamps are epoch milliseconds. Percentages are fractions (0.034 = 3.4 %).

// ---------------------------------------------------------------- System
export type Mode = "sim" | "paper" | "live";
export type MarketSession = "pre" | "regular" | "post" | "closed";

export interface Status {
  mode: Mode;
  live_allowed: boolean;
  paper_keys: boolean;
  live_keys: boolean;
  market: {
    is_open: boolean;
    session: MarketSession;
    next_open: number;
    next_close: number;
    now: number;
  };
  feeds: {
    stocks: string;
    options: string;
    news: boolean;
    flow_realtime: boolean;
    connected: Record<string, boolean>;
    /** Addition: per-stream last error. */
    errors?: Record<string, string | null>;
  };
  autotrader: {
    armed: boolean;
    running: boolean;
    last_cycle: number | null;
    killed: boolean;
    kill_reason: string | null;
  };
  llm: { enabled: boolean; available: boolean; model: string; base_url: string };
  version: string;
  /** Addition: last account-poll error or null. */
  error?: string | null;
  /** Addition: why a requested mode failed to start (the backend falls back to SIM). */
  mode_error?: string | null;
}

export interface Settings {
  stock_feed: "iex" | "sip";
  options_feed: "indicative" | "opra";
  universe: string[];
  watchlist: string[];
  ws_flush_ms: number;
}

export interface ModeRequest {
  mode: Mode;
  confirm?: "LIVE";
}

// ---------------------------------------------------------------- Account & portfolio
export type Tier = "micro" | "small" | "growth" | "pro";

export interface Account {
  equity: number;
  last_equity: number;
  cash: number;
  buying_power: number;
  options_buying_power: number;
  daytrade_count: number;
  pattern_day_trader: boolean;
  daytrading_buying_power: number;
  day_pl: number;
  day_pl_pct: number;
  tier: Tier;
  status: string;
  currency: string;
  options_level: number;
}

export interface Position {
  symbol: string;
  asset_class: "equity" | "option";
  underlying: string;
  qty: number;
  side: "long" | "short";
  avg_entry_price: number;
  current_price: number;
  market_value: number;
  cost_basis: number;
  unrealized_pl: number;
  unrealized_plpc: number;
  change_today_pct: number;
  strategy_id: string | null;
  option?: {
    type: "call" | "put";
    strike: number;
    expiration: string;
    dte: number;
    delta?: number;
    iv?: number;
  };
  exit?: { stop_price?: number; target_price?: number; trail_pct?: number; time_stop?: number };
}

export interface OrderRequest {
  symbol: string;
  side: "buy" | "sell";
  qty?: number;
  notional?: number;
  type: "market" | "limit";
  limit_price?: number;
  time_in_force?: "day" | "gtc";
  extended_hours?: boolean;
}

export interface Order {
  id: string;
  client_order_id: string;
  symbol: string;
  asset_class: "equity" | "option";
  side: "buy" | "sell";
  qty: number | null;
  notional: number | null;
  filled_qty: number;
  filled_avg_price: number | null;
  type: string;
  limit_price: number | null;
  status: string;
  submitted_at: number;
  filled_at: number | null;
  strategy_id: string | null;
  // Additions on `orders` channel payloads:
  event?: string;
  fill_price?: number | null;
  fill_qty?: number | null;
  event_t?: number | null;
}

export type HistoryRange = "1D" | "1W" | "1M" | "3M" | "YTD" | "1Y" | "ALL";

export interface EquityPoint {
  t: number;
  v: number;
}

export interface PortfolioHistory {
  range: string;
  timeframe: string;
  base_value: number;
  points: EquityPoint[];
  session_bounds?: { open: number; close: number };
  /** Addition (1D only): full session window used to pad the x-axis (04:00–20:00 ET live). */
  day_bounds?: { start: number; end: number };
}

export interface JournalEntry {
  id: string;
  symbol: string;
  strategy_id: string;
  asset_class: string;
  direction: number;
  entry_t: number;
  exit_t: number | null;
  entry_price: number;
  exit_price: number | null;
  qty: number;
  pnl: number | null;
  pnl_pct: number | null;
  reason_exit: string | null;
}

// ---------------------------------------------------------------- Market data
export type BarsRange = "1D" | "1W" | "1M" | "3M" | "YTD" | "1Y" | "5Y";
export type OptionBarsRange = "1D" | "1W" | "1M" | "3M";

export interface QuoteSnap {
  symbol: string;
  t: number;
  bid: number;
  ask: number;
  last: number;
  prev_close: number;
  change: number;
  change_pct: number;
  volume: number;
}

export interface Bar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface BarsResponse {
  symbol: string;
  range: string;
  timeframe: string;
  prev_close: number | null;
  bars: Bar[];
}

export interface SearchResult {
  symbol: string;
  name: string;
  exchange: string;
}

export interface OptionRow {
  symbol: string;
  type: "call" | "put";
  strike: number;
  expiration: string;
  bid: number;
  ask: number;
  mid: number;
  last: number;
  volume: number;
  open_interest: number;
  iv: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
}

export interface Chain {
  underlying: string;
  spot: number;
  expiration: string;
  dte: number;
  calls: OptionRow[];
  puts: OptionRow[];
  atm_iv: number | null;
  expected_move: number | null;
}

// ---------------------------------------------------------------- Scanner
export interface ScanCandidate {
  contract: string;
  underlying: string;
  type: "call" | "put";
  strike: number;
  expiration: string;
  dte: number;
  spot: number;
  bid: number;
  ask: number;
  mid: number;
  spread_pct: number;
  volume: number;
  open_interest: number;
  iv: number;
  delta: number;
  theta: number;
  iv_rank: number | null;
  iv_rv_ratio: number | null;
  forecast_return: number;
  forecast_vol: number;
  ev_pct: number;
  pop: number;
  kelly: number;
  breakeven: number;
  edge_score: number;
  reasons: string[];
  /** Addition: modelled entry fill (mid + aggression × half-spread). */
  entry_price?: number;
}

export interface ScanResult {
  t: number;
  duration_ms: number;
  params: Record<string, unknown>;
  candidates: ScanCandidate[];
  errors: string[];
}

/** Scanner params are described at runtime by ParamSpec[]; the body is a partial name→value map. */
export type ScanParams = Record<string, unknown>;

// ---------------------------------------------------------------- Signals, news, flow
export interface SignalRow {
  model: string;
  symbol: string;
  score: number;
  confidence: number;
  horizon_days: number;
  t: number;
  meta: Record<string, unknown>;
  ic?: number | null;
}

export interface Forecast {
  symbol: string;
  t: number;
  horizon_days: number;
  exp_return: number;
  vol: number;
  prob_up: number;
  z: number;
  contributions: { model: string; weight: number; score: number }[];
}

export interface NewsItem {
  id: string;
  t: number;
  headline: string;
  summary: string;
  source: string;
  url: string;
  symbols: string[];
  sentiment: number | null;
  relevance: number | null;
  llm?: { direction: number; magnitude: number; horizon: string; rationale: string } | null;
}

export interface FlowEvent {
  t: number;
  contract: string;
  underlying: string;
  type: "call" | "put";
  strike: number;
  expiration: string;
  price: number;
  size: number;
  premium: number;
  side: "ask" | "bid" | "mid";
  is_sweep: boolean;
  unusual_score: number;
  direction: number;
  source: "stream" | "snapshot";
}

// ---------------------------------------------------------------- Calendar
export type CalKind = "earnings" | "fomc" | "macro" | "opex" | "holiday" | "early_close" | "user";

export interface CalEvent {
  id: string;
  date: string;
  time: string | null;
  kind: CalKind;
  title: string;
  symbol: string | null;
  importance: 1 | 2 | 3;
  meta: Record<string, unknown>;
  source: string;
}

export interface CalEventCreate {
  date: string;
  time?: string;
  title: string;
  kind: CalKind;
  symbol?: string;
  importance: 1 | 2 | 3;
}

// ---------------------------------------------------------------- Strategies & auto-trader
export interface ParamSpec {
  name: string;
  type: "float" | "int" | "bool" | "enum" | "str";
  default: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  label: string;
  help: string;
  group: string;
}

export interface StrategyInfo {
  id: string;
  name: string;
  description: string;
  asset_class: "option" | "equity";
  style: "intraday" | "swing";
  enabled: boolean;
  use_llm_gate: boolean;
  params: Record<string, unknown>;
  schema: ParamSpec[];
  tiers: string[];
  stats: { trades: number; win_rate: number | null; pnl: number };
  /** full = replayable on daily bars; synthetic = options priced synthetically; none = can't be backtested. */
  backtest?: { support: BacktestSupport; note: string };
  /** Persisted runtime state (start equity when switched on, halt reason). */
  runtime?: { start_equity?: number | null; started_t?: number; halted?: string | null; halted_t?: number | null; halted_equity?: number };
  /** Strategy-specific UI data (Rags to Riches: effective settings, goal, floor). */
  extra?: { effective?: RagsEffective; goal_equity?: number; floor_equity?: number; start_equity?: number | null; halted?: string | null };
}

export type BacktestSupport = "full" | "synthetic" | "none";
export type BacktestSizing = "equal_weight" | "live";

export interface StrategyUpdate {
  enabled?: boolean;
  params?: Record<string, unknown>;
  use_llm_gate?: boolean;
}

export interface RiskConfig {
  kelly_multiplier: number;
  max_risk_per_trade: number;
  max_position_pct: number;
  max_open_positions: number;
  max_per_underlying: number;
  max_options_allocation: number;
  max_daily_loss: number;
  max_drawdown: number;
  max_trades_per_day: number;
  min_option_volume: number;
  min_open_interest: number;
  max_spread_pct: number;
  allow_daytrades: "auto" | "never" | "always";
  entry_start_min: number;
  entry_end_min: number;
  earnings_blackout_days: number;
  cycle_seconds: number;
  llm_enabled: boolean;
  on_llm_failure: "skip" | "approve";
  aggression: number;
  order_timeout_s: number;
}

export interface TierPreset {
  tier: string;
  label: string;
  description: string;
  min_equity: number;
  max_equity: number | null;
  /** Manual-only preset (never chosen automatically by equity), e.g. Rags to Riches. */
  manual?: boolean;
}

/** POST /api/tools/goal-odds — Monte Carlo odds of compounding from start to goal before floor. */
export interface GoalOddsRequest {
  start: number;
  goal: number;
  floor: number;
  position_frac: number;
  stop_frac: number;
  reward_r: number;
  edge_pp?: number;
  edge_sd?: number;
  win_rate?: number;
  cost_frac?: number;
  stop_slippage?: number;
  trades_per_day?: number;
  max_days?: number;
}

export interface GoalOdds {
  p_goal: number;
  p_floor: number;
  p_unresolved: number;
  median_trades_to_goal: number | null;
  p10_trades_to_goal: number | null;
  median_days_to_goal?: number;
  win_rate: number;
  baseline_win_rate: number;
  breakeven_win_rate: number;
  edge_sd: number;
  p_negative_edge: number;
  win_multiplier: number;
  loss_multiplier: number;
  expectancy_pct: number;
  log_growth_per_trade: number;
  kelly_fraction: number;
  kelly_ratio: number | null;
  max_trades: number;
}

export interface RagsEffective {
  position_pct: number;
  option_pct: number;
  max_trades_per_day: number;
  min_minutes_between: number;
  min_score: number;
  stop_pct: number;
  reward_r: number;
  daily_stop_pct: number;
  max_consecutive_losses: number;
}

export interface AutoTraderState {
  armed: boolean;
  running: boolean;
  killed: boolean;
  kill_reason: string | null;
  tier: string;
  risk: RiskConfig;
  risk_schema: ParamSpec[];
  last_cycle: number | null;
  cycle_ms: number | null;
  today: { trades: number; pnl: number };
  presets: TierPreset[];
  /** Addition: when disarmed, would-be entries are logged as `skip` decisions prefixed "preview (disarmed):". */
  preview?: boolean;
  /** Addition: false = tier follows equity automatically. */
  tier_locked?: boolean;
  /** Addition: keep managing exits of open positions while disarmed. */
  manage_exits_when_disarmed?: boolean;
}

/** PUT /api/autotrader/risk body: RiskConfig fields plus the two addition toggles. */
export type RiskUpdate = Partial<RiskConfig> & { preview?: boolean; manage_exits_when_disarmed?: boolean };

/** Addition: GET /api/signals/models */
export interface SignalModelInfo {
  id: string;
  name: string;
  horizon_days: number;
  kind: string;
  directional: boolean;
  description: string;
  enabled: boolean;
  prior_weight: number;
  weight: number;
  ic: number | null;
  ic_sd: number | null;
  ic_days: number;
  ic_shrunk: number | null;
}

export type DecisionAction = "enter" | "exit" | "skip" | "reject" | "error";

export interface Decision {
  id: string;
  t: number;
  strategy_id: string;
  symbol: string;
  action: DecisionAction;
  reason: string;
  edge_score: number | null;
  llm: {
    decision: string;
    confidence: number;
    size_multiplier: number;
    rationale: string;
    latency_ms: number;
  } | null;
  order_id: string | null;
}

// ---------------------------------------------------------------- LLM
export interface LlmInfo {
  enabled: boolean;
  available: boolean;
  base_url: string;
  model: string;
  models: string[];
  /** Shape not specified by API.md; rendered as key/value pairs. */
  stats: Record<string, unknown> | null;
}

/** One logged exchange with the local LLM (list view; also the payload of the `llm` WS channel). */
export interface LlmCallSummary {
  id: string;
  t: number;
  purpose: string; // "gate" | "news" | "analyst" | "test" | …
  model: string;
  latency_ms: number;
  ok: boolean;
  cached: boolean;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  error: string | null;
  preview: string;
}

export interface LlmCallDetail extends Omit<LlmCallSummary, "preview"> {
  system: string | null;
  user: string;
  response: string;
  parsed: Record<string, unknown> | null;
}

export interface LlmCallsResponse {
  calls: LlmCallSummary[];
  by_purpose: { purpose: string; n: number; fails: number; avg_ms: number | null }[];
}

export interface LlmCallsQuery {
  limit?: number;
  purpose?: string;
  status?: "ok" | "fail" | "cached";
  q?: string;
  before?: number;
}

export interface LlmUpdate {
  enabled?: boolean;
  model?: string;
  base_url?: string;
  temperature?: number;
}

export interface LlmTestResult {
  ok: boolean;
  latency_ms: number;
  text: string;
}

// ---------------------------------------------------------------- Backtest
export interface BacktestRequest {
  strategy_id: string;
  symbols: string[];
  start: string;
  end: string;
  params?: Record<string, unknown>;
  initial_equity?: number;
  /** Assumed implied/realised vol ratio for synthetic option pricing (default 1.10). */
  iv_rv?: number;
  /** equal_weight (default): each symbol gets an equal slice of equity; live: the auto-trader's risk settings. */
  sizing?: BacktestSizing;
}

export interface BacktestMetrics {
  cagr: number;
  sharpe: number;
  sortino: number;
  max_drawdown: number;
  calmar: number;
  win_rate: number;
  profit_factor: number;
  trades: number;
  exposure: number;
  psr: number;
  dsr: number;
}

export interface BacktestResult {
  strategy_id: string;
  equity: EquityPoint[];
  benchmark: EquityPoint[];
  metrics: BacktestMetrics;
  trades: JournalEntry[];
  notes: string[];
  sizing?: BacktestSizing;
  funnel?: { ideas: number; entered: number; top_rejections: Record<string, number> };
}

// ---------------------------------------------------------------- WebSocket payloads
export interface WsQuote {
  symbol: string;
  t: number;
  bid: number;
  ask: number;
  last: number;
  change_pct: number;
}

export interface WsOptionQuote {
  symbol: string;
  t: number;
  bid: number;
  ask: number;
  mid: number;
}

export interface WsBatchFrame {
  b: { ch: string; d: unknown }[];
  ts: number;
}

export interface WsPongFrame {
  pong: number;
  ts: number;
}

export interface ApiErrorBody {
  error: string;
  detail: string;
}
