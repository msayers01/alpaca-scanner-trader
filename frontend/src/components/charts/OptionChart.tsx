import { useCallback, useState, type ReactNode } from "react";
import { api } from "../../lib/api";
import type { OptionBarsRange } from "../../lib/types";
import { channels } from "../../lib/ws";
import type { ChartStyle } from "./chartCore";
import { BarsChart } from "./BarsChart";

export const OPTION_RANGES = ["1D", "1W", "1M", "3M"] as const satisfies readonly OptionBarsRange[];

/** Option contract chart (OCC symbol) with live mid from `oquote:<OCC>`. */
export function OptionChart({ occ, title, height = 260 }: { occ: string; title?: ReactNode; height?: number }) {
  const [range, setRange] = useState<OptionBarsRange>("1D");
  const [style, setStyle] = useState<ChartStyle>("line");
  const sym = occ.toUpperCase();
  const fetcher = useCallback((r: OptionBarsRange, signal: AbortSignal) => api.optionBars(sym, r, { signal }), [sym]);
  return (
    <BarsChart
      instrument={sym}
      ranges={OPTION_RANGES}
      range={range}
      onRange={setRange}
      fetcher={fetcher}
      live={{ oquote: channels.oquote(sym) }}
      title={title}
      height={height}
      style={style}
      onStyle={setStyle}
      smallHeader
    />
  );
}
