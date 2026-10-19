// Generic Robinhood-style bars chart used by PriceChart (stocks/ETFs) and OptionChart (OCC).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { errorMessage } from "../../lib/api";
import type { Bar, BarsResponse, WsOptionQuote, WsQuote } from "../../lib/types";
import { fmtCompact, fmtPrice } from "../../lib/format";
import { palette } from "../../lib/theme";
import { useChannel } from "../../hooks/useChannel";
import { useOnReconnect } from "../../hooks/useOnReconnect";
import { useStatusStore } from "../../store/status";
import { inferStep, labelKindFor, regularSession, RhChart, type ChartStyle, type HoverInfo } from "./chartCore";
import { LiveTag, RangeButtons, RhHeader } from "./ChartParts";
import { Icon } from "../ui/Icon";

export interface BarsChartProps<R extends string> {
  /** Cache key for the instrument (symbol / OCC). Changing it refetches. */
  instrument: string;
  ranges: readonly R[];
  range: R;
  onRange: (r: R) => void;
  fetcher: (range: R, signal: AbortSignal) => Promise<BarsResponse>;
  live: { bar?: string; quote?: string; oquote?: string };
  title?: ReactNode;
  height?: number;
  allowCandles?: boolean;
  style?: ChartStyle;
  onStyle?: (s: ChartStyle) => void;
  volume?: boolean;
  smallHeader?: boolean;
  /** Notified when the live/last price changes (for parents that show it elsewhere). */
  onPrice?: (v: number | null) => void;
}

export function BarsChart<R extends string>({
  instrument,
  ranges,
  range,
  onRange,
  fetcher,
  live,
  title,
  height = 320,
  allowCandles = true,
  style = "line",
  onStyle,
  volume = true,
  smallHeader,
  onPrice,
}: BarsChartProps<R>) {
  const connOpen = useStatusStore((s) => s.conn.state === "open");
  const hostRef = useRef<HTMLDivElement>(null);
  const timeRef = useRef<HTMLDivElement>(null);
  const ctrlRef = useRef<RhChart | null>(null);
  const [resp, setResp] = useState<BarsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<number | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [nonce, setNonce] = useState(0);
  const fetchedAt = useRef(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const onPriceRef = useRef(onPrice);
  onPriceRef.current = onPrice;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const ctrl = new RhChart(host, timeRef.current, { volume, onHover: setHover, onLast: (v) => setLast(v) });
    ctrlRef.current = ctrl;
    return () => {
      ctrl.destroy();
      ctrlRef.current = null;
    };
  }, [volume]);

  useEffect(() => {
    ctrlRef.current?.setStyle(allowCandles ? style : "line");
  }, [style, allowCandles]);

  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    setHover(null);
    fetcherRef
      .current(range, ac.signal)
      .then((r) => {
        if (ac.signal.aborted) return;
        fetchedAt.current = Date.now();
        setResp(r);
        setLoading(false);
        const c = ctrlRef.current;
        if (!c) return;
        const ts = r.bars.map((b) => b.t);
        const step = inferStep(ts, r.timeframe);
        const is1D = range === "1D";
        const lastT = ts.length ? ts[ts.length - 1] : Date.now();
        c.setData({
          bars: r.bars,
          baseline: is1D ? r.prev_close ?? r.bars[0]?.o ?? null : r.bars[0]?.c ?? null,
          showBaseline: is1D && r.prev_close !== null,
          session: is1D && step < 86_400_000 ? regularSession(lastT) : null,
          padSession: is1D && step < 86_400_000,
          stepMs: step,
          labelKind: labelKindFor(range, step),
        });
        setLast(c.lastValue());
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return;
        setError(errorMessage(e));
        setLoading(false);
      });
    return () => ac.abort();
  }, [instrument, range, nonce]);

  useOnReconnect(
    useCallback(() => {
      if (error || Date.now() - fetchedAt.current > 3000) setNonce((n) => n + 1);
    }, [error]),
  );

  const ready = !!resp && !loading;
  useChannel<Bar>(ready ? live.bar : null, (b) => ctrlRef.current?.liveBar(b));
  useChannel<WsQuote>(ready ? live.quote : null, (q) => {
    if (q && q.last > 0) ctrlRef.current?.liveQuote(q.last, q.t);
  });
  useChannel<WsOptionQuote>(ready ? live.oquote : null, (q) => {
    if (q && q.mid > 0) ctrlRef.current?.liveQuote(q.mid, q.t);
  });

  useEffect(() => {
    onPriceRef.current?.(last);
  }, [last]);

  const bars = resp?.bars ?? [];
  const baseline = resp ? (range === "1D" ? resp.prev_close ?? bars[0]?.o ?? null : bars[0]?.c ?? null) : null;
  const up = last === null || baseline === null ? true : last >= baseline;
  useEffect(() => {
    ctrlRef.current?.setColor(up ? palette.up() : palette.down());
  }, [up, resp]);

  const shown = hover ? hover.v : last;
  const hb = hover?.bar;
  const extra =
    style === "candles" && hb ? (
      <div className="small muted num" style={{ marginTop: 4 }}>
        O {fmtPrice(hb.o)} · H {fmtPrice(hb.h)} · L {fmtPrice(hb.l)} · C {fmtPrice(hb.c)} · Vol {fmtCompact(hb.v)}
      </div>
    ) : null;

  return (
    <div className="rh">
      <RhHeader title={title} value={shown} baseline={baseline} range={range} extra={extra} small={smallHeader} />
      <div className="rh-canvas" style={{ height }}>
        <div ref={hostRef} className="chart-host" />
        <div ref={timeRef} className="rh-time" />
        {loading && !resp && (
          <div className="rh-overlay-msg">
            <span className="spinner" /> Loading chart
          </div>
        )}
        {error && (
          <div className="rh-overlay-msg interactive">
            <span>Chart unavailable — {error}</span>
            <button className="btn btn-sm" onClick={() => setNonce((n) => n + 1)}>
              Retry
            </button>
          </div>
        )}
        {!loading && !error && resp && bars.length === 0 && <div className="rh-overlay-msg">No bars for this range</div>}
      </div>
      <RangeButtons ranges={ranges} value={range} onChange={onRange} up={up}>
        {range === "1D" && <LiveTag on={connOpen} />}
        {loading && resp && <span className="spinner" aria-label="Loading" />}
        {allowCandles && onStyle && (
          <div className="seg" role="group" aria-label="Chart style">
            <button className={style === "line" ? "active" : ""} aria-pressed={style === "line"} onClick={() => onStyle("line")} title="Line">
              <Icon name="line" size={14} />
            </button>
            <button className={style === "candles" ? "active" : ""} aria-pressed={style === "candles"} onClick={() => onStyle("candles")} title="Candlesticks">
              <Icon name="candles" size={14} />
            </button>
          </div>
        )}
      </RangeButtons>
    </div>
  );
}
