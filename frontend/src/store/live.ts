// Wires the global WS channels into the stores. Called once from main.tsx.
import type { Account, Decision, FlowEvent, IntelItem, MacroState, NewsItem, Order, Position, SignalRow, Status } from "../lib/types";
import { channels, ws } from "../lib/ws";
import { useAccountStore } from "./account";
import { pushFeed } from "./feeds";
import { useIntelStore } from "./intel";
import { useStatusStore } from "./status";
import { useUiStore } from "./ui";
import { toast } from "../components/ui/Toast";

let started = false;

const fmtQty = (n: number | null | undefined) => (n === null || n === undefined ? "" : Number.isInteger(n) ? String(n) : n.toFixed(4));

/** Toast on fills / rejections (the `event` field is an API.md addition; fall back to status). */
function notifyOrder(o: Order): void {
  const ev = (o.event ?? o.status ?? "").toLowerCase();
  const who = o.strategy_id ? ` (${o.strategy_id})` : "";
  if (ev === "fill" || ev === "filled") {
    const px = o.fill_price ?? o.filled_avg_price;
    toast.success(`Filled: ${o.side} ${fmtQty(o.fill_qty ?? o.filled_qty)} ${o.symbol}${px ? ` @ ${px.toFixed(2)}` : ""}${who}`);
  } else if (ev === "partial_fill" || ev === "partially_filled") {
    toast.info(`Partial fill: ${o.side} ${fmtQty(o.fill_qty ?? o.filled_qty)} ${o.symbol}${who}`);
  } else if (ev === "rejected") {
    toast.error(`Order rejected: ${o.side} ${o.symbol}${who}`);
  }
}

export function startLive(): void {
  if (started) return;
  started = true;

  const acc = useAccountStore.getState();
  const st = useStatusStore.getState();

  ws.subscribe<Status>(channels.status, (s) => {
    const prev = useStatusStore.getState().status;
    useStatusStore.getState().setStatus(s);
    // Mode switch: everything account-related belongs to a different broker now.
    if (prev && prev.mode !== s.mode) void useAccountStore.getState().loadAll();
  });
  ws.subscribe<Account>(channels.account, (a) => useAccountStore.getState().setAccount(a));
  ws.subscribe<Position[]>(channels.positions, (p) => useAccountStore.getState().setPositions(p));
  ws.subscribe<Order>(channels.orders, (o) => {
    useAccountStore.getState().applyOrder(o);
    notifyOrder(o);
  });
  ws.subscribe<NewsItem>(channels.news, (n) => pushFeed("news", n));
  ws.subscribe<FlowEvent>(channels.flow, (f) => pushFeed("flow", f));
  ws.subscribe<SignalRow>(channels.signals, (s) => pushFeed("signals", s));
  ws.subscribe<Decision>(channels.decisions, (d) => pushFeed("decisions", d));
  ws.subscribe<MacroState>(channels.macro, (m) => useIntelStore.getState().setMacro(m));
  ws.subscribe<IntelItem>(channels.intel, (i) => useIntelStore.getState().pushItem(i));

  // Initial REST snapshot, then resync whenever the socket reconnects.
  const loadSettings = () =>
    st.loadSettings().then(() => {
      const s = useStatusStore.getState().settings;
      if (s) useUiStore.getState().setWatchlist(s.watchlist);
    });
  const resync = () => {
    void useStatusStore.getState().loadStatus();
    void acc.loadAll();
    if (!useStatusStore.getState().settings) void loadSettings();
  };
  resync();

  let wasOpen = false;
  let everOpened = false;
  ws.onState((c) => {
    if (c.state === "open") {
      if (!wasOpen && everOpened) resync();
      wasOpen = true;
      everOpened = true;
    } else if (c.state === "closed") {
      wasOpen = false;
    }
  });

  // Poll status slowly as a fallback when the socket is down (market session, etc.).
  window.setInterval(() => {
    if (ws.getState().state !== "open") void useStatusStore.getState().loadStatus();
  }, 10_000);

  ws.start();
}
