// Live feed ring buffers (newest first, capped). WS pushes are buffered and flushed at most
// ~8x/s so a burst of flow prints causes one store update, not hundreds.
import { create } from "zustand";
import { api } from "../lib/api";
import type { Decision, FlowEvent, NewsItem, SignalRow } from "../lib/types";

export const FEED_CAP = 500;
const FLUSH_MS = 125;

export type FeedKind = "news" | "flow" | "signals" | "decisions";

type ItemOf<K extends FeedKind> = K extends "news"
  ? NewsItem
  : K extends "flow"
    ? FlowEvent
    : K extends "signals"
      ? SignalRow
      : Decision;

interface FeedsState {
  news: NewsItem[];
  flow: FlowEvent[];
  signals: SignalRow[];
  decisions: Decision[];
  loaded: Record<FeedKind, boolean>;
  loading: Record<FeedKind, boolean>;
  errors: Record<FeedKind, string | null>;
  ensureLoaded: (k: FeedKind, force?: boolean) => Promise<void>;
}

export const flowKey = (f: FlowEvent) => `${f.contract}|${f.t}|${f.size}|${f.price}`;
export const signalKey = (s: SignalRow) => `${s.symbol}|${s.model}|${s.t}`;

const keyOf: { [K in FeedKind]: (x: ItemOf<K>) => string } = {
  news: (n) => n.id,
  flow: flowKey,
  signals: signalKey,
  decisions: (d) => d.id,
};

function merge<K extends FeedKind>(k: K, incoming: ItemOf<K>[], existing: ItemOf<K>[]): ItemOf<K>[] {
  const key = keyOf[k] as (x: ItemOf<K>) => string;
  const seen = new Set<string>();
  const out: ItemOf<K>[] = [];
  const all = [...incoming, ...existing].sort((a, b) => b.t - a.t);
  for (const x of all) {
    const id = key(x);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(x);
    if (out.length >= FEED_CAP) break;
  }
  return out;
}

const loaders: { [K in FeedKind]: () => Promise<ItemOf<K>[]> } = {
  news: () => api.news(undefined, 200),
  flow: () => api.flow(undefined, FEED_CAP),
  signals: () => api.signals(),
  decisions: () => api.decisions(FEED_CAP),
};

export const useFeedsStore = create<FeedsState>((set, get) => ({
  news: [],
  flow: [],
  signals: [],
  decisions: [],
  loaded: { news: false, flow: false, signals: false, decisions: false },
  loading: { news: false, flow: false, signals: false, decisions: false },
  errors: { news: null, flow: null, signals: null, decisions: null },
  ensureLoaded: async (k, force = false) => {
    const s = get();
    if (s.loading[k] || (s.loaded[k] && !force)) return;
    set((st) => ({ loading: { ...st.loading, [k]: true } }));
    try {
      const items = await loaders[k]();
      set((st) => ({
        [k]: merge(k, items as never[], st[k] as never[]),
        loaded: { ...st.loaded, [k]: true },
        loading: { ...st.loading, [k]: false },
        errors: { ...st.errors, [k]: null },
      }));
    } catch (e) {
      set((st) => ({
        loading: { ...st.loading, [k]: false },
        errors: { ...st.errors, [k]: e instanceof Error ? e.message : String(e) },
      }));
    }
  },
}));

// ---------------------------------------------------------------- buffered push
const pending: { [K in FeedKind]: ItemOf<K>[] } = { news: [], flow: [], signals: [], decisions: [] };
let timer: number | null = null;

function flush() {
  timer = null;
  const patch: Partial<Record<FeedKind, unknown[]>> = {};
  const st = useFeedsStore.getState();
  (Object.keys(pending) as FeedKind[]).forEach((k) => {
    const items = pending[k];
    if (!items.length) return;
    pending[k] = [];
    if (k === "news" || k === "decisions") {
      // newest arrival first so a re-sent item (e.g. news after LLM enrichment) replaces the older copy by id
      patch[k] = merge(k, (items as never[]).slice().reverse(), st[k] as never[]);
    } else {
      // flow/signals are high-rate and have no ids worth de-duping against the whole buffer
      const next = [...(items as unknown[]).slice().reverse(), ...(st[k] as unknown[])];
      patch[k] = next.length > FEED_CAP ? next.slice(0, FEED_CAP) : next;
    }
  });
  if (Object.keys(patch).length) useFeedsStore.setState(patch as Partial<FeedsState>);
}

export function pushFeed<K extends FeedKind>(k: K, item: ItemOf<K>): void {
  (pending[k] as ItemOf<K>[]).push(item);
  if (timer === null) timer = window.setTimeout(flush, FLUSH_MS);
}
