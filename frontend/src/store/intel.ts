// Intel state: macro regime (VIX + prediction markets + movers) and the live post/statement feed.
// The WS `intel` feed is buffered (same pattern as store/feeds.ts) so a burst of posts causes one store update.
import { create } from "zustand";
import { api, errorMessage } from "../lib/api";
import type { IntelItem, IntelSummary, MacroState } from "../lib/types";

const CAP = 400;
const FLUSH_MS = 250;

interface IntelState {
  summary: IntelSummary | null;
  macro: MacroState | null;
  /** Live items, newest first (statements + social), merged by id. */
  items: IntelItem[];
  loading: boolean;
  error: string | null;
  load: () => Promise<void>;
  setMacro: (m: MacroState) => void;
  pushItem: (i: IntelItem) => void;
}

function merge(incoming: IntelItem[], existing: IntelItem[]): IntelItem[] {
  const seen = new Set<string>();
  const out: IntelItem[] = [];
  for (const x of [...incoming, ...existing].sort((a, b) => b.t - a.t)) {
    if (seen.has(x.id)) continue;
    seen.add(x.id);
    out.push(x);
    if (out.length >= CAP) break;
  }
  return out;
}

export const useIntelStore = create<IntelState>((set, get) => ({
  summary: null,
  macro: null,
  items: [],
  loading: false,
  error: null,
  load: async () => {
    if (get().loading) return;
    set({ loading: true });
    try {
      const [summary, items] = await Promise.all([api.intelSummary(), api.intelItems({ limit: 300 })]);
      set((s) => ({ summary, macro: summary.macro, items: merge(items, s.items), loading: false, error: null }));
    } catch (e) {
      set({ loading: false, error: errorMessage(e) });
    }
  },
  setMacro: (m) => set((s) => ({ macro: m, summary: s.summary ? { ...s.summary, macro: m } : s.summary })),
  pushItem: (i) => {
    pending.push(i);
    if (timer === null) timer = window.setTimeout(flush, FLUSH_MS);
  },
}));

const pending: IntelItem[] = [];
let timer: number | null = null;

function flush() {
  timer = null;
  if (!pending.length) return;
  const batch = pending.splice(0).reverse(); // newest arrival first so an LLM-enriched re-send replaces the old copy
  useIntelStore.setState((s) => ({ items: merge(batch, s.items) }));
}
