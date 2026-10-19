// Live quotes. Components retain a symbol (ref-counted) and read it with a per-symbol selector
// so only the leaf that shows that symbol re-renders on a tick.
import { useEffect } from "react";
import { create } from "zustand";
import { api } from "../lib/api";
import type { WsOptionQuote, WsQuote } from "../lib/types";
import { channels, ws } from "../lib/ws";

export interface LiveQuote {
  symbol: string;
  t: number;
  bid: number;
  ask: number;
  last: number;
  change_pct: number;
  prev_close: number | null;
  change: number | null;
  volume: number | null;
}

interface QuotesState {
  quotes: Record<string, LiveQuote>;
  oquotes: Record<string, WsOptionQuote>;
}

export const useQuotesStore = create<QuotesState>(() => ({ quotes: {}, oquotes: {} }));

// Coalesce WS ticks per animation frame: many symbols ticking -> one store update per frame.
let qPending: Record<string, Partial<LiveQuote> & { symbol: string }> = {};
let oPending: Record<string, WsOptionQuote> = {};
let raf: number | null = null;

function flushTicks() {
  raf = null;
  const q = qPending;
  const o = oPending;
  qPending = {};
  oPending = {};
  useQuotesStore.setState((s) => {
    let quotes = s.quotes;
    const qk = Object.keys(q);
    if (qk.length) {
      quotes = { ...s.quotes };
      for (const sym of qk) {
        const prev = quotes[sym];
        const patch = q[sym];
        const merged: LiveQuote = {
          symbol: sym,
          t: patch.t ?? prev?.t ?? Date.now(),
          bid: patch.bid ?? prev?.bid ?? 0,
          ask: patch.ask ?? prev?.ask ?? 0,
          last: patch.last ?? prev?.last ?? 0,
          change_pct: patch.change_pct ?? prev?.change_pct ?? 0,
          prev_close: patch.prev_close ?? prev?.prev_close ?? null,
          change: patch.change ?? null,
          volume: patch.volume ?? prev?.volume ?? null,
        };
        if (patch.change === undefined) {
          merged.change = merged.prev_close ? merged.last - merged.prev_close : prev?.change ?? null;
        }
        quotes[sym] = merged;
      }
    }
    let oquotes = s.oquotes;
    const ok = Object.keys(o);
    if (ok.length) oquotes = { ...s.oquotes, ...o };
    return { quotes, oquotes };
  });
}

function schedule() {
  if (raf === null) raf = requestAnimationFrame(flushTicks);
}

const refs = new Map<string, { n: number; unsub: () => void }>();

/** Start streaming a stock quote (ref-counted). Seeds from REST /api/quote once. Returns release(). */
export function retainQuote(symbol: string): () => void {
  const sym = symbol.toUpperCase();
  const key = `q:${sym}`;
  const r = refs.get(key);
  if (r) {
    r.n++;
  } else {
    const unsub = ws.subscribe<WsQuote>(channels.quote(sym), (d) => {
      qPending[sym] = { ...(qPending[sym] ?? {}), ...d, symbol: sym };
      schedule();
    });
    refs.set(key, { n: 1, unsub });
    if (!useQuotesStore.getState().quotes[sym]) {
      api
        .quote(sym)
        .then((snap) => {
          const cur = useQuotesStore.getState().quotes[sym];
          if (cur && cur.t > snap.t) {
            qPending[sym] = { ...(qPending[sym] ?? {}), symbol: sym, prev_close: snap.prev_close, volume: snap.volume };
          } else {
            qPending[sym] = { ...snap, ...(qPending[sym] ?? {}), symbol: sym };
          }
          schedule();
        })
        .catch(() => {
          /* quote unavailable; leaf shows a dash */
        });
    }
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const cur = refs.get(key);
    if (!cur) return;
    cur.n--;
    if (cur.n <= 0) {
      cur.unsub();
      refs.delete(key);
    }
  };
}

/** Stream an option quote (`oquote:<OCC>`). Returns release(). */
export function retainOptionQuote(occ: string): () => void {
  const sym = occ.toUpperCase();
  const key = `o:${sym}`;
  const r = refs.get(key);
  if (r) r.n++;
  else {
    const unsub = ws.subscribe<WsOptionQuote>(channels.oquote(sym), (d) => {
      oPending[sym] = { ...d, symbol: sym };
      schedule();
    });
    refs.set(key, { n: 1, unsub });
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const cur = refs.get(key);
    if (!cur) return;
    cur.n--;
    if (cur.n <= 0) {
      cur.unsub();
      refs.delete(key);
    }
  };
}

/** Subscribe + read one symbol's quote. Only the calling component re-renders on its ticks. */
export function useQuote(symbol: string | null | undefined): LiveQuote | undefined {
  const sym = symbol?.toUpperCase() ?? "";
  useEffect(() => (sym ? retainQuote(sym) : undefined), [sym]);
  return useQuotesStore((s) => (sym ? s.quotes[sym] : undefined));
}

export function useOptionQuote(occ: string | null | undefined): WsOptionQuote | undefined {
  const sym = occ?.toUpperCase() ?? "";
  useEffect(() => (sym ? retainOptionQuote(sym) : undefined), [sym]);
  return useQuotesStore((s) => (sym ? s.oquotes[sym] : undefined));
}

/** Seed a quote (e.g. from a REST snapshot already fetched elsewhere). */
export function seedQuote(q: Partial<LiveQuote> & { symbol: string }) {
  const sym = q.symbol.toUpperCase();
  qPending[sym] = { ...(qPending[sym] ?? {}), ...q, symbol: sym };
  schedule();
}
