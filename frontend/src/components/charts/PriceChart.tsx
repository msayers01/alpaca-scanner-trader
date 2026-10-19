import { useCallback, type ReactNode } from "react";
import { api } from "../../lib/api";
import type { BarsRange } from "../../lib/types";
import { channels } from "../../lib/ws";
import { useUiStore } from "../../store/ui";
import { BarsChart } from "./BarsChart";

export const PRICE_RANGES = ["1D", "1W", "1M", "3M", "YTD", "1Y", "5Y"] as const satisfies readonly BarsRange[];

/** Stock/ETF price chart: Robinhood line by default, candlestick toggle, volume pane, live bars + quotes. */
export function PriceChart({ symbol, title, height = 340, onPrice }: { symbol: string; title?: ReactNode; height?: number; onPrice?: (v: number | null) => void }) {
  const stored = useUiStore((s) => s.priceRange) as BarsRange;
  const range: BarsRange = (PRICE_RANGES as readonly string[]).includes(stored) ? stored : "1D";
  const setRange = useUiStore((s) => s.setPriceRange);
  const style = useUiStore((s) => s.priceStyle);
  const setStyle = useUiStore((s) => s.setPriceStyle);
  const sym = symbol.toUpperCase();
  const fetcher = useCallback((r: BarsRange, signal: AbortSignal) => api.bars(sym, r, { signal }), [sym]);
  return (
    <BarsChart
      instrument={sym}
      ranges={PRICE_RANGES}
      range={range}
      onRange={setRange}
      fetcher={fetcher}
      live={{ bar: channels.bar(sym), quote: channels.quote(sym) }}
      title={title}
      height={height}
      style={style}
      onStyle={setStyle}
      onPrice={onPrice}
    />
  );
}
