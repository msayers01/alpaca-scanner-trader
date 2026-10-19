// Leaf components for ticking prices. Each subscribes to exactly one symbol so a tick only
// re-renders that leaf, never the table/page around it.
import { memo, useEffect, useRef } from "react";
import { fmtPct, fmtPrice, fmtUsd, signClass } from "../lib/format";
import { useOptionQuote, useQuote } from "../store/quotes";

/** Flash the element green/red when `value` moves. Imperative: no extra render. */
function useTickFlash(value: number | undefined) {
  const ref = useRef<HTMLSpanElement>(null);
  const prev = useRef<number | undefined>(value);
  useEffect(() => {
    const el = ref.current;
    const p = prev.current;
    prev.current = value;
    if (!el || value === undefined || p === undefined || value === p) return;
    el.classList.remove("tick-up", "tick-down");
    void el.offsetWidth; // restart animation
    el.classList.add(value > p ? "tick-up" : "tick-down");
  }, [value]);
  return ref;
}

export const LivePrice = memo(function LivePrice({ symbol, fallback, dollar = true, flash = true }: { symbol: string; fallback?: number | null; dollar?: boolean; flash?: boolean }) {
  const q = useQuote(symbol);
  const v = q?.last || fallback || undefined;
  const ref = useTickFlash(flash ? v : undefined);
  return (
    <span ref={ref} className="num">
      {dollar ? fmtUsd(v) : fmtPrice(v)}
    </span>
  );
});

export const LiveChangePct = memo(function LiveChangePct({ symbol, fallback, pill }: { symbol: string; fallback?: number | null; pill?: boolean }) {
  const q = useQuote(symbol);
  const v = q ? q.change_pct : fallback ?? undefined;
  const cls = signClass(v);
  return <span className={`num ${pill ? `pill-change ${cls}` : cls}`}>{fmtPct(v, { signed: true })}</span>;
});

export const LiveOptionMid = memo(function LiveOptionMid({ occ, fallback }: { occ: string; fallback?: number | null }) {
  const q = useOptionQuote(occ);
  const v = q?.mid ?? fallback ?? undefined;
  const ref = useTickFlash(v);
  return (
    <span ref={ref} className="num">
      {fmtUsd(v)}
    </span>
  );
});

/** Bid × Ask for a stock. */
export const LiveBidAsk = memo(function LiveBidAsk({ symbol }: { symbol: string }) {
  const q = useQuote(symbol);
  return (
    <span className="num muted">
      {fmtPrice(q?.bid)} × {fmtPrice(q?.ask)}
    </span>
  );
});
