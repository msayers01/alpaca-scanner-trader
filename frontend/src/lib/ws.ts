// Single multiplexed WebSocket client for /ws (see docs/API.md §WebSocket).
// - ref-counted channel subscriptions: subscribe(ch, cb) => unsubscribe
// - batch frames {b:[{ch,d}], ts} are decoded and dispatched per channel
// - auto-reconnect with exponential backoff + jitter, automatic re-subscribe
// - ping every 15 s, round-trip latency measured from pong
// - connection state observable (onState / getState) for the UI

import type { WsBatchFrame, WsPongFrame } from "./types";

export type ConnState = "connecting" | "open" | "closed";

export interface ConnInfo {
  state: ConnState;
  /** Last measured ping round-trip in ms. */
  rttMs: number | null;
  /** Epoch ms of the last frame received. */
  lastFrameAt: number | null;
  /** Server ts of the last frame minus local receive time (rough clock offset). */
  serverLagMs: number | null;
  reconnectAttempt: number;
  /** When the next reconnect will be attempted (epoch ms), if closed. */
  nextRetryAt: number | null;
}

type Listener = (data: unknown, ch: string) => void;

const PING_MS = 15_000;
const BACKOFF_MIN = 500;
const BACKOFF_MAX = 15_000;

class WsClient {
  private sock: WebSocket | null = null;
  private subs = new Map<string, Set<Listener>>();
  private pendingSub = new Set<string>();
  private pendingUnsub = new Set<string>();
  private flushQueued = false;
  private pingTimer: number | null = null;
  private retryTimer: number | null = null;
  private pingId = 0;
  private pingSent = new Map<number, number>();
  private stateListeners = new Set<(c: ConnInfo) => void>();
  private started = false;
  private info: ConnInfo = {
    state: "closed",
    rttMs: null,
    lastFrameAt: null,
    serverLagMs: null,
    reconnectAttempt: 0,
    nextRetryAt: null,
  };

  constructor(private readonly url: () => string) {}

  /** Start the connection (idempotent). */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.connect();
    window.addEventListener("online", this.retryNow);
    document.addEventListener("visibilitychange", this.onVisible);
  }

  /** Subscribe to a channel. Returns an unsubscribe function. Multiple listeners share one server subscription. */
  subscribe<T = unknown>(ch: string, cb: (data: T, ch: string) => void): () => void {
    let set = this.subs.get(ch);
    if (!set) {
      set = new Set();
      this.subs.set(ch, set);
      this.pendingUnsub.delete(ch);
      this.pendingSub.add(ch);
      this.queueFlush();
    }
    const l = cb as Listener;
    set.add(l);
    let done = false;
    return () => {
      if (done) return;
      done = true;
      const s = this.subs.get(ch);
      if (!s) return;
      s.delete(l);
      if (s.size === 0) {
        this.subs.delete(ch);
        if (this.pendingSub.has(ch)) this.pendingSub.delete(ch);
        else this.pendingUnsub.add(ch);
        this.queueFlush();
      }
    };
  }

  getState(): ConnInfo {
    return this.info;
  }

  onState(cb: (c: ConnInfo) => void): () => void {
    this.stateListeners.add(cb);
    cb(this.info);
    return () => {
      this.stateListeners.delete(cb);
    };
  }

  /** Force an immediate reconnect attempt (e.g. user clicked "Retry"). */
  retryNow = (): void => {
    if (this.info.state === "open" || this.info.state === "connecting") return;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.info = { ...this.info, reconnectAttempt: 0 };
    this.connect();
  };

  // ------------------------------------------------------------------ internals

  private onVisible = (): void => {
    if (document.visibilityState === "visible" && this.info.state === "closed") this.retryNow();
  };

  private setInfo(patch: Partial<ConnInfo>): void {
    this.info = { ...this.info, ...patch };
    for (const l of this.stateListeners) l(this.info);
  }

  private connect(): void {
    this.retryTimer = null;
    this.setInfo({ state: "connecting", nextRetryAt: null });
    let sock: WebSocket;
    try {
      sock = new WebSocket(this.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.sock = sock;
    sock.onopen = () => {
      if (this.sock !== sock) return;
      this.setInfo({ state: "open", reconnectAttempt: 0 });
      // re-subscribe everything we currently hold
      this.pendingSub.clear();
      this.pendingUnsub.clear();
      const all = [...this.subs.keys()];
      if (all.length) this.send({ op: "sub", ch: all });
      this.startPing();
    };
    sock.onmessage = (ev) => this.onMessage(ev);
    sock.onerror = () => {
      /* onclose follows */
    };
    sock.onclose = () => {
      if (this.sock !== sock) return;
      this.sock = null;
      this.stopPing();
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    const attempt = this.info.reconnectAttempt + 1;
    const base = Math.min(BACKOFF_MAX, BACKOFF_MIN * 2 ** Math.min(attempt - 1, 8));
    const delay = Math.round(base * (0.75 + Math.random() * 0.5));
    this.setInfo({ state: "closed", reconnectAttempt: attempt, nextRetryAt: Date.now() + delay, rttMs: null });
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = window.setTimeout(() => this.connect(), delay);
  }

  private startPing(): void {
    this.stopPing();
    const ping = () => {
      const id = ++this.pingId;
      this.pingSent.set(id, performance.now());
      if (this.pingSent.size > 8) {
        const first = this.pingSent.keys().next().value;
        if (first !== undefined) this.pingSent.delete(first);
      }
      this.send({ op: "ping", id });
    };
    ping();
    this.pingTimer = window.setInterval(ping, PING_MS);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) window.clearInterval(this.pingTimer);
    this.pingTimer = null;
    this.pingSent.clear();
  }

  private send(msg: unknown): void {
    const s = this.sock;
    if (s && s.readyState === WebSocket.OPEN) s.send(JSON.stringify(msg));
  }

  private queueFlush(): void {
    if (this.flushQueued) return;
    this.flushQueued = true;
    queueMicrotask(() => {
      this.flushQueued = false;
      if (!this.sock || this.sock.readyState !== WebSocket.OPEN) return; // onopen re-subscribes everything
      if (this.pendingUnsub.size) this.send({ op: "unsub", ch: [...this.pendingUnsub] });
      if (this.pendingSub.size) this.send({ op: "sub", ch: [...this.pendingSub] });
      this.pendingSub.clear();
      this.pendingUnsub.clear();
    });
  }

  private onMessage(ev: MessageEvent): void {
    if (typeof ev.data !== "string") return;
    let msg: unknown;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    const now = Date.now();
    const frame = msg as Partial<WsBatchFrame & WsPongFrame>;
    if (typeof frame.pong === "number") {
      const sent = this.pingSent.get(frame.pong);
      this.pingSent.delete(frame.pong);
      this.setInfo({
        rttMs: sent !== undefined ? Math.round(performance.now() - sent) : this.info.rttMs,
        lastFrameAt: now,
        serverLagMs: typeof frame.ts === "number" ? now - frame.ts : this.info.serverLagMs,
      });
      return;
    }
    if (!Array.isArray(frame.b)) return;
    // Update the frame clock without notifying listeners on every frame (cheap field write).
    this.info = { ...this.info, lastFrameAt: now, serverLagMs: typeof frame.ts === "number" ? now - frame.ts : this.info.serverLagMs };
    for (const item of frame.b) {
      if (!item || typeof item.ch !== "string") continue;
      const set = this.subs.get(item.ch);
      if (!set) continue;
      for (const l of set) {
        try {
          l(item.d, item.ch);
        } catch (err) {
          console.error(`[ws] listener for ${item.ch} threw`, err);
        }
      }
    }
  }
}

function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

export const ws = new WsClient(wsUrl);

export const channels = {
  quote: (sym: string) => `quote:${sym.toUpperCase()}`,
  bar: (sym: string) => `bar:${sym.toUpperCase()}`,
  oquote: (occ: string) => `oquote:${occ.toUpperCase()}`,
  account: "account",
  positions: "positions",
  orders: "orders",
  equity: "equity",
  news: "news",
  flow: "flow",
  signals: "signals",
  decisions: "decisions",
  status: "status",
  scanner: "scanner",
} as const;
