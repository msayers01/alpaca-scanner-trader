// Typed REST client for every route in docs/API.md. All URLs are relative (Vite proxies /api).
import type {
  Account,
  AutoTraderState,
  BacktestRequest,
  BacktestResult,
  BarsRange,
  BarsResponse,
  CalEvent,
  CalEventCreate,
  Chain,
  Decision,
  LlmCallDetail,
  LlmCallsQuery,
  LlmCallsResponse,
  FlowEvent,
  Forecast,
  HistoryRange,
  JournalEntry,
  LlmInfo,
  LlmTestResult,
  LlmUpdate,
  ModeRequest,
  NewsItem,
  OptionBarsRange,
  Order,
  OrderRequest,
  ParamSpec,
  PortfolioHistory,
  Position,
  QuoteSnap,
  RiskUpdate,
  ScanParams,
  ScanResult,
  SearchResult,
  Settings,
  SignalModelInfo,
  SignalRow,
  Status,
  StrategyInfo,
  StrategyUpdate,
  Tier,
} from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly detail: string;
  readonly status: number;
  constructor(code: string, detail: string, status: number) {
    super(detail || code);
    this.name = "ApiError";
    this.code = code;
    this.detail = detail;
    this.status = status;
  }
  /** True when the backend could not be reached at all (offline / proxy error). */
  get offline(): boolean {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function qs(query?: Query): string {
  if (!query) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

const enc = encodeURIComponent;

async function request<T>(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  opts: { query?: Query; body?: unknown; signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<T> {
  const url = `/api${path}${qs(opts.query)}`;
  const ctrl = new AbortController();
  const timeout = window.setTimeout(() => ctrl.abort(new DOMException("timeout", "TimeoutError")), opts.timeoutMs ?? 30_000);
  const onAbort = () => ctrl.abort(opts.signal?.reason);
  opts.signal?.addEventListener("abort", onAbort, { once: true });
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: opts.body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: ctrl.signal,
      cache: "no-store",
    });
  } catch (e) {
    if (opts.signal?.aborted) throw e;
    const timedOut = e instanceof DOMException && (e.name === "TimeoutError" || e.name === "AbortError");
    throw new ApiError(timedOut ? "timeout" : "network", timedOut ? "Request timed out" : "Backend unreachable", 0);
  } finally {
    window.clearTimeout(timeout);
    opts.signal?.removeEventListener("abort", onAbort);
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    if (data === null && (res.status === 502 || res.status === 503 || res.status === 504)) {
      // dev/preview proxy answers 502 when the backend process is not running
      throw new ApiError("offline", "Backend unreachable — is it running on 127.0.0.1:8765?", res.status);
    }
    const body = (data ?? {}) as Partial<{ error: string; detail: unknown }>;
    const detail =
      typeof body.detail === "string"
        ? body.detail
        : body.detail !== undefined
          ? JSON.stringify(body.detail)
          : res.statusText || `HTTP ${res.status}`;
    throw new ApiError(body.error ?? `http_${res.status}`, detail, res.status);
  }
  if (data && typeof data === "object" && !Array.isArray(data) && "error" in data && "detail" in data && Object.keys(data).length === 2) {
    const body = data as { error: string; detail: string };
    throw new ApiError(body.error, body.detail, res.status);
  }
  return data as T;
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail || e.code;
  if (e instanceof Error) return e.message;
  return String(e);
}

type Opt = { signal?: AbortSignal };

export const api = {
  // System
  status: (o?: Opt) => request<Status>("GET", "/status", o),
  setMode: (body: ModeRequest) => request<Status>("POST", "/mode", { body, timeoutMs: 60_000 }),
  settings: (o?: Opt) => request<Settings>("GET", "/settings", o),
  updateSettings: (body: Partial<Settings>) => request<Settings>("PUT", "/settings", { body }),

  // Account & portfolio
  account: (o?: Opt) => request<Account>("GET", "/account", o),
  positions: (o?: Opt) => request<Position[]>("GET", "/positions", o),
  closePosition: (symbol: string, pct?: number) =>
    request<Order>("POST", `/positions/${enc(symbol)}/close`, { body: pct === undefined ? {} : { pct } }),
  orders: (status: "open" | "closed" | "all" = "open", limit?: number, o?: Opt) =>
    request<Order[]>("GET", "/orders", { query: { status, limit }, ...o }),
  placeOrder: (body: OrderRequest) => request<Order>("POST", "/orders", { body }),
  cancelOrder: (id: string) => request<{ ok: true }>("DELETE", `/orders/${enc(id)}`),
  portfolioHistory: (range: HistoryRange, o?: Opt) =>
    request<PortfolioHistory>("GET", "/portfolio/history", { query: { range }, ...o }),
  journal: (limit?: number, o?: Opt) => request<JournalEntry[]>("GET", "/journal", { query: { limit }, ...o }),

  // Market data
  quote: (symbol: string, o?: Opt) => request<QuoteSnap>("GET", `/quote/${enc(symbol)}`, o),
  bars: (symbol: string, range: BarsRange, o?: Opt) =>
    request<BarsResponse>("GET", "/bars", { query: { symbol, range }, ...o }),
  search: (q: string, o?: Opt) => request<SearchResult[]>("GET", "/search", { query: { q }, ...o }),
  expirations: (underlying: string, o?: Opt) =>
    request<string[]>("GET", "/options/expirations", { query: { underlying }, ...o }),
  chain: (underlying: string, expiration: string, o?: Opt) =>
    request<Chain>("GET", "/options/chain", { query: { underlying, expiration }, ...o }),
  optionBars: (symbol: string, range: OptionBarsRange, o?: Opt) =>
    request<BarsResponse>("GET", "/options/bars", { query: { symbol, range }, ...o }),

  // Scanner
  scanner: (o?: Opt) => request<ScanResult>("GET", "/scanner", o),
  runScanner: (params: ScanParams) => request<ScanResult>("POST", "/scanner/run", { body: params, timeoutMs: 180_000 }),
  scannerParams: (o?: Opt) => request<ParamSpec[]>("GET", "/scanner/params", o),

  // Signals, news, flow
  signals: (symbol?: string, o?: Opt) => request<SignalRow[]>("GET", "/signals", { query: { symbol }, ...o }),
  signalModels: (o?: Opt) => request<SignalModelInfo[]>("GET", "/signals/models", o),
  forecast: (symbol: string, o?: Opt) => request<Forecast>("GET", `/signals/forecast/${enc(symbol)}`, o),
  news: (symbol?: string, limit = 100, o?: Opt) =>
    request<NewsItem[]>("GET", "/news", { query: { symbol, limit: Math.min(limit, 200) }, ...o }),
  flow: (underlying?: string, limit = 200, o?: Opt) =>
    request<FlowEvent[]>("GET", "/flow", { query: { underlying, limit }, ...o }),

  // Calendar
  calendar: (start: string, end: string, o?: Opt) => request<CalEvent[]>("GET", "/calendar", { query: { start, end }, ...o }),
  addCalendarEvent: (body: CalEventCreate) => request<CalEvent>("POST", "/calendar", { body }),
  deleteCalendarEvent: (id: string) => request<{ ok: true }>("DELETE", `/calendar/${enc(id)}`),

  // Strategies & auto-trader
  strategies: (o?: Opt) => request<StrategyInfo[]>("GET", "/strategies", o),
  updateStrategy: (id: string, body: StrategyUpdate) => request<StrategyInfo>("PUT", `/strategies/${enc(id)}`, { body }),
  autotrader: (o?: Opt) => request<AutoTraderState>("GET", "/autotrader", o),
  updateRisk: (body: RiskUpdate) => request<AutoTraderState>("PUT", "/autotrader/risk", { body }),
  /** `tier: "auto"` unlocks automatic tier selection (addition). */
  applyPreset: (tier: Tier | "auto" | string) => request<AutoTraderState>("POST", "/autotrader/preset", { body: { tier } }),
  arm: (armed: boolean, confirm?: "LIVE") =>
    request<AutoTraderState>("POST", "/autotrader/arm", { body: confirm ? { armed, confirm } : { armed } }),
  kill: (flatten: boolean) => request<AutoTraderState>("POST", "/autotrader/kill", { body: { flatten }, timeoutMs: 60_000 }),
  resetKill: () => request<AutoTraderState>("POST", "/autotrader/reset-kill"),
  runCycle: () => request<AutoTraderState>("POST", "/autotrader/cycle", { timeoutMs: 120_000 }),
  decisions: (limit = 200, o?: Opt) => request<Decision[]>("GET", "/autotrader/decisions", { query: { limit }, ...o }),

  // LLM
  llm: (o?: Opt) => request<LlmInfo>("GET", "/llm", o),
  updateLlm: (body: LlmUpdate) => request<LlmInfo>("PUT", "/llm", { body }),
  testLlm: (prompt?: string) =>
    request<LlmTestResult>("POST", "/llm/test", { body: prompt ? { prompt } : {}, timeoutMs: 120_000 }),
  llmCalls: (query: LlmCallsQuery = {}, o?: Opt) =>
    request<LlmCallsResponse>("GET", "/llm/calls", { query: query as Query, ...o }),
  llmCall: (id: string, o?: Opt) => request<LlmCallDetail>("GET", `/llm/calls/${enc(id)}`, o),

  // SIM only (addition): reset the simulated account to a starting equity ($100–$10M)
  simReset: (equity: number) => request<Account>("POST", "/sim/reset", { body: { equity } }),

  // Backtest
  backtest: (body: BacktestRequest) => request<BacktestResult>("POST", "/backtest", { body, timeoutMs: 600_000 }),
};

export type Api = typeof api;
