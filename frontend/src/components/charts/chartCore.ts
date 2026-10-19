// Shared, imperative chart controller for the Robinhood-style charts (portfolio, stock, option).
// React owns only the header text; every data point / tick goes straight to lightweight-charts.
//
// Lightweight Charts™ — Copyright (c) 2025 TradingView, Inc. — Apache License 2.0.
// https://www.tradingview.com/ (attribution is shown on the Help page).
import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  TickMarkType,
  type AutoscaleInfo,
  type CandlestickData,
  type DeepPartial,
  type ChartOptions,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import type { Bar, EquityPoint } from "../../lib/types";
import { etMidnight, etWallTime, ET } from "../../lib/format";
import { palette, withAlpha } from "../../lib/theme";

export const toTime = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;
export const fromTime = (t: Time): number => (typeof t === "number" ? t * 1000 : typeof t === "string" ? Date.parse(t) : Date.UTC(t.year, t.month - 1, t.day));

// ---------------------------------------------------------------- ranges & labels
export const RANGE_LABEL: Record<string, string> = {
  "1D": "Today",
  "1W": "Past week",
  "1M": "Past month",
  "3M": "Past 3 months",
  YTD: "Year to date",
  "1Y": "Past year",
  "5Y": "Past 5 years",
  ALL: "All time",
};

const MIN = 60_000;
const DAY = 86_400_000;

/** "1Min" | "5Min" | "15Min" | "1H" | "1Hour" | "1D" | "1Day" | "1W" → ms (null if unknown). */
export function parseTimeframe(tf: string | null | undefined): number | null {
  if (!tf) return null;
  const m = /^(\d+)\s*(min|m|t|h|hour|d|day|w|week)$/i.exec(tf.trim());
  if (!m) return null;
  const n = Number(m[1]);
  const u = m[2].toLowerCase();
  if (u === "min" || u === "m" || u === "t") return n * MIN;
  if (u === "h" || u === "hour") return n * 60 * MIN;
  if (u === "d" || u === "day") return n * DAY;
  return n * 7 * DAY;
}

export function inferStep(ts: number[], timeframe?: string | null): number {
  const tf = parseTimeframe(timeframe);
  if (tf) return tf;
  if (ts.length < 2) return 5 * MIN;
  const diffs: number[] = [];
  for (let i = 1; i < ts.length && diffs.length < 200; i++) {
    const d = ts[i] - ts[i - 1];
    if (d > 0) diffs.push(d);
  }
  diffs.sort((a, b) => a - b);
  return diffs[Math.floor(diffs.length / 2)] ?? 5 * MIN;
}

export type TimeLabelKind = "time" | "datetime" | "date";

const fmtCache = {
  time: new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit" }),
  datetime: new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
  date: new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short", day: "numeric", year: "numeric" }),
  tickTime: new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit" }),
  tickDay: new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short", day: "numeric" }),
  tickMonth: new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short" }),
  tickYear: new Intl.DateTimeFormat("en-US", { timeZone: ET, year: "numeric" }),
};

export function formatHoverTime(ms: number, kind: TimeLabelKind): string {
  if (kind === "time") return `${fmtCache.time.format(ms)} ET`;
  if (kind === "datetime") return `${fmtCache.datetime.format(ms)} ET`;
  return fmtCache.date.format(ms);
}

export function labelKindFor(range: string, stepMs: number): TimeLabelKind {
  if (stepMs >= DAY) return "date";
  return range === "1D" ? "time" : "datetime";
}

function tickMarkFormatter(time: Time, type: TickMarkType): string {
  const ms = fromTime(time);
  switch (type) {
    case TickMarkType.Year:
      return fmtCache.tickYear.format(ms);
    case TickMarkType.Month:
      return fmtCache.tickMonth.format(ms);
    case TickMarkType.DayOfMonth:
      return fmtCache.tickDay.format(ms);
    default:
      return fmtCache.tickTime.format(ms);
  }
}

/**
 * Whitespace timestamps that make the 1D x-axis span the whole session even before it ends.
 * Spans at least 09:30–16:00 ET; extends to 04:00/20:00 when the data has extended-hours points.
 */
export function sessionPadTimes(
  ts: number[],
  stepMs: number,
  bounds?: { open: number; close: number } | null,
  dayBounds?: { start: number; end: number } | null,
): number[] {
  const ref = ts.length ? ts[ts.length - 1] : Date.now();
  const step0 = Math.max(MIN, Math.min(stepMs, 30 * MIN));
  if (dayBounds && dayBounds.end > dayBounds.start) {
    // backend-provided full session window (e.g. 04:00–20:00 ET live)
    const first = ts.length ? ts[0] : dayBounds.start;
    const last = ts.length ? ts[ts.length - 1] : dayBounds.start - step0;
    const out: number[] = [];
    for (let t = Math.ceil(dayBounds.start / step0) * step0; t < first; t += step0) out.push(t);
    for (let t = Math.floor(last / step0) * step0 + step0; t <= dayBounds.end; t += step0) out.push(t);
    return out;
  }
  const open = bounds?.open ?? etWallTime(ref, 9, 30);
  const close = bounds?.close ?? etWallTime(ref, 16, 0);
  const first = ts.length ? ts[0] : open;
  const last = ts.length ? ts[ts.length - 1] : open - stepMs;
  const hasPre = first < open - stepMs / 2;
  const hasPost = last > close;
  const extended = hasPre || hasPost;
  const start = Math.min(first, open);
  const end = extended ? Math.max(etWallTime(ref, 20, 0), last) : Math.max(close, last);
  const step = Math.max(MIN, Math.min(stepMs, 30 * MIN));
  const out: number[] = [];
  // before the first point (e.g. backend started late)
  for (let t = Math.ceil(start / step) * step; t < first; t += step) out.push(t);
  // after the last point up to the session end
  for (let t = Math.floor(last / step) * step + step; t <= end; t += step) out.push(t);
  return out;
}

/** Default 1D session bounds (regular hours) for the ET date of `ref`. */
export function regularSession(ref: number): { open: number; close: number } {
  return { open: etWallTime(ref, 9, 30), close: etWallTime(ref, 16, 0) };
}

export function baseChartOptions(axes: boolean): DeepPartial<ChartOptions> {
  const bg = palette.bg();
  return {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: bg },
      textColor: palette.text3(),
      fontFamily: palette.font(),
      fontSize: 11,
      attributionLogo: false,
      panes: { separatorColor: palette.border(), separatorHoverColor: palette.border(), enableResize: false },
    },
    grid: { vertLines: { visible: false }, horzLines: { visible: false } },
    rightPriceScale: { visible: axes, borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
    leftPriceScale: { visible: false },
    timeScale: {
      visible: axes,
      borderVisible: false,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 0,
      fixLeftEdge: true,
      // not fixRightEdge: it clamps to the last *data* bar and would hide the 1D session padding
      fixRightEdge: false,
      lockVisibleTimeRangeOnResize: true,
      tickMarkFormatter,
    },
    localization: {
      timeFormatter: (t: Time) => fmtCache.datetime.format(fromTime(t)),
    },
    crosshair: {
      mode: CrosshairMode.Magnet,
      vertLine: { visible: true, labelVisible: false, style: LineStyle.Solid, width: 1, color: palette.text3() },
      horzLine: { visible: false, labelVisible: false },
    },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: { time: true, price: true }, axisDoubleClickReset: true },
    kineticScroll: { mouse: false, touch: true },
  };
}

// ---------------------------------------------------------------- controller
export type ChartStyle = "line" | "candles";

export interface HoverInfo {
  t: number;
  v: number;
  bar?: Bar;
}

export interface RhData {
  /** Line points (portfolio). Ignored if `bars` given. */
  points?: EquityPoint[];
  bars?: Bar[];
  baseline: number | null;
  showBaseline: boolean;
  /** Regular-session bounds: points outside are drawn dimmer. */
  session?: { open: number; close: number } | null;
  /** Pad the x-axis to the whole session (1D). */
  padSession?: boolean;
  /** Full-day window for padding (PortfolioHistory.day_bounds). */
  dayBounds?: { start: number; end: number } | null;
  stepMs: number;
  labelKind: TimeLabelKind;
}

interface Opts {
  axes?: boolean;
  volume?: boolean;
  onHover?: (h: HoverInfo | null) => void;
  /** Called (rAF-throttled) when a live update changes the last value. */
  onLast?: (v: number, t: number) => void;
}

export class RhChart {
  readonly chart: IChartApi;
  private line: ISeriesApi<"Line">;
  private pad: ISeriesApi<"Line">;
  private candles: ISeriesApi<"Candlestick"> | null = null;
  private volume: ISeriesApi<"Histogram"> | null = null;
  private baseLine: IPriceLine | null = null;
  private color: string;
  private style: ChartStyle = "line";
  private data: RhData | null = null;
  private pts: EquityPoint[] = [];
  private bars: Bar[] = [];
  /** live aggregation state for the last bucket */
  private agg: { bucket: number; base: Bar | null; minutes: Map<number, Bar> } | null = null;
  private lastRaf: number | null = null;
  private lastPending: { v: number; t: number } | null = null;
  private axes: boolean;
  private destroyed = false;
  private crosshairHandler: (p: MouseEventParams) => void;
  private dblHandler: () => void;

  constructor(
    private readonly host: HTMLElement,
    private readonly timeLabel: HTMLElement | null,
    private readonly opts: Opts = {},
  ) {
    this.axes = !!opts.axes;
    this.color = palette.up();
    this.chart = createChart(host, baseChartOptions(this.axes));
    const autoscale = (orig: () => AutoscaleInfo | null): AutoscaleInfo | null => {
      const r = orig();
      const b = this.data?.showBaseline ? this.data.baseline : null;
      if (!r || !r.priceRange || b === null || !Number.isFinite(b)) return r;
      return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, b), maxValue: Math.max(r.priceRange.maxValue, b) } };
    };
    this.pad = this.chart.addSeries(LineSeries, {
      lineVisible: false,
      pointMarkersVisible: false,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    this.line = this.chart.addSeries(LineSeries, {
      color: this.color,
      lineWidth: 2,
      lineType: LineType.Simple,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4.5,
      crosshairMarkerBorderWidth: 2,
      crosshairMarkerBorderColor: palette.bg(),
      lastValueVisible: false,
      priceLineVisible: false,
      autoscaleInfoProvider: autoscale,
    });
    if (opts.volume) {
      this.volume = this.chart.addSeries(
        HistogramSeries,
        { priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false, priceScaleId: "vol" },
        1,
      );
      this.volume.priceScale().applyOptions({ visible: false, scaleMargins: { top: 0.1, bottom: 0 } });
      const panes = this.chart.panes();
      panes[0]?.setStretchFactor(1);
      panes[1]?.setStretchFactor(0.18);
    }

    this.crosshairHandler = (p) => this.onCrosshair(p);
    this.chart.subscribeCrosshairMove(this.crosshairHandler);
    this.dblHandler = () => this.reset();
    host.addEventListener("dblclick", this.dblHandler);
    this.autoscaleFn = autoscale;
  }

  private autoscaleFn: (orig: () => AutoscaleInfo | null) => AutoscaleInfo | null;

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.lastRaf !== null) cancelAnimationFrame(this.lastRaf);
    this.chart.unsubscribeCrosshairMove(this.crosshairHandler);
    this.host.removeEventListener("dblclick", this.dblHandler);
    this.chart.remove();
  }

  /** Fit all data (incl. session padding) and re-enable price autoscale. */
  reset(): void {
    if (this.destroyed) return;
    this.chart.timeScale().fitContent();
    this.chart.priceScale("right").applyOptions({ autoScale: true });
  }

  setColor(color: string): void {
    if (this.destroyed || color === this.color) return;
    this.color = color;
    this.line.applyOptions({ color });
    if (this.data?.session) this.applyLine();
    if (this.baseLine) this.applyBaseline();
  }

  setStyle(style: ChartStyle): void {
    if (this.destroyed || style === this.style) return;
    this.style = style;
    if (style === "candles" && !this.candles) {
      this.candles = this.chart.addSeries(CandlestickSeries, {
        upColor: palette.up(),
        downColor: palette.down(),
        borderVisible: false,
        wickUpColor: palette.up(),
        wickDownColor: palette.down(),
        lastValueVisible: false,
        priceLineVisible: false,
        autoscaleInfoProvider: this.autoscaleFn,
      });
    }
    this.line.applyOptions({ visible: style === "line" });
    this.candles?.applyOptions({ visible: style === "candles" });
    this.chart.applyOptions({
      rightPriceScale: { visible: this.axes || style === "candles" },
      timeScale: { visible: this.axes || style === "candles" },
    });
    if (style === "candles") this.applyCandles();
    this.applyBaseline();
  }

  setData(d: RhData): void {
    if (this.destroyed) return;
    this.data = d;
    this.agg = null;
    if (d.bars) {
      this.bars = dedupeBars(d.bars);
      this.pts = this.bars.map((b) => ({ t: b.t, v: b.c }));
    } else {
      this.bars = [];
      this.pts = dedupePoints(d.points ?? []);
    }
    this.applyLine();
    if (this.style === "candles") this.applyCandles();
    this.applyVolume();
    const ts = this.pts.map((p) => p.t);
    const pad = d.padSession ? sessionPadTimes(ts, d.stepMs, d.session, d.dayBounds) : [];
    this.pad.setData(pad.map((t) => ({ time: toTime(t) }) as WhitespaceData<Time>));
    this.applyBaseline();
    this.reset();
  }

  /** Current last value (live). */
  lastValue(): number | null {
    return this.pts.length ? this.pts[this.pts.length - 1].v : null;
  }

  firstValue(): number | null {
    return this.pts.length ? this.pts[0].v : null;
  }

  /** Live line tick (portfolio equity): update the current bucket or append a new one. */
  livePoint(t: number, v: number): void {
    if (this.destroyed || !this.data || !Number.isFinite(v)) return;
    const step = this.data.stepMs;
    const bucket = Math.floor(t / step) * step;
    const last = this.pts[this.pts.length - 1];
    if (last && t < last.t) return; // stale tick
    let p: EquityPoint;
    if (last && bucket <= last.t) {
      p = { t: last.t, v };
      this.pts[this.pts.length - 1] = p;
    } else {
      p = { t: bucket, v };
      this.pts.push(p);
    }
    this.line.update(this.lineDatum(p));
    this.emitLast(v, t);
  }

  /** Live 1-minute bar (`bar:<SYM>`): merged into the current bucket of the chart timeframe. */
  liveBar(b: Bar): void {
    if (this.destroyed || !this.data || !this.data.bars) return;
    const step = this.data.stepMs;
    const last = this.bars[this.bars.length - 1];
    if (step <= MIN) {
      const t = Math.floor(b.t / MIN) * MIN;
      if (last && t < last.t) return;
      const bar = { ...b, t };
      if (last && last.t === t) this.bars[this.bars.length - 1] = bar;
      else this.bars.push(bar);
      this.pushBar(bar);
      return;
    }
    const inLast = !!last && sameBucket(b.t, last.t, step);
    if (last && !inLast && b.t < last.t) return; // stale
    let merged: Bar;
    if (inLast && last) {
      if (!this.agg || this.agg.bucket !== last.t) this.agg = { bucket: last.t, base: { ...last }, minutes: new Map() };
      // repeated minutes (same t, updated values) replace rather than add
      this.agg.minutes.set(b.t, b);
      merged = combine(last.t, this.agg.base, this.agg.minutes);
      this.bars[this.bars.length - 1] = merged;
    } else {
      const t = step >= DAY ? etMidnight(b.t) : Math.floor(b.t / step) * step;
      this.agg = { bucket: t, base: null, minutes: new Map([[b.t, b]]) };
      merged = combine(t, null, this.agg.minutes);
      this.bars.push(merged);
    }
    this.pushBar(merged);
  }

  /** Live quote: moves the close of the newest bar (no new bars are created from quotes). */
  liveQuote(price: number, t: number): void {
    if (this.destroyed || !this.data || !Number.isFinite(price) || price <= 0) return;
    const step = this.data.stepMs;
    if (this.data.bars) {
      const last = this.bars[this.bars.length - 1];
      if (!last || t < last.t || !sameBucket(t, last.t, Math.max(step, MIN))) return;
      const merged: Bar = { ...last, c: price, h: Math.max(last.h, price), l: Math.min(last.l, price) };
      this.bars[this.bars.length - 1] = merged;
      if (this.agg && this.agg.bucket === last.t && this.agg.base) {
        this.agg.base = { ...this.agg.base, h: Math.max(this.agg.base.h, price), l: Math.min(this.agg.base.l, price) };
      }
      this.pushBar(merged, false);
    } else {
      this.livePoint(t, price);
    }
  }

  // ------------------------------------------------------------ internals
  private pushBar(b: Bar, withVolume = true): void {
    const p = { t: b.t, v: b.c };
    const lastPt = this.pts[this.pts.length - 1];
    if (lastPt && lastPt.t === b.t) this.pts[this.pts.length - 1] = p;
    else this.pts.push(p);
    this.line.update(this.lineDatum(p));
    if (this.candles && this.style === "candles") this.candles.update(candleDatum(b));
    if (withVolume && this.volume) this.volume.update(this.volDatum(b));
    this.emitLast(b.c, b.t);
  }

  private emitLast(v: number, t: number): void {
    if (!this.opts.onLast) return;
    this.lastPending = { v, t };
    if (this.lastRaf !== null) return;
    this.lastRaf = requestAnimationFrame(() => {
      this.lastRaf = null;
      const lp = this.lastPending;
      if (lp && !this.destroyed) this.opts.onLast?.(lp.v, lp.t);
    });
  }

  private lineDatum(p: EquityPoint): LineData<Time> {
    const s = this.data?.session;
    const out: LineData<Time> = { time: toTime(p.t), value: p.v };
    if (s && (p.t < s.open || p.t >= s.close)) out.color = withAlpha(this.color, 0.4);
    return out;
  }

  private volDatum(b: Bar): HistogramData<Time> {
    return { time: toTime(b.t), value: b.v, color: withAlpha(b.c >= b.o ? palette.up() : palette.down(), 0.28) };
  }

  private applyLine(): void {
    this.line.setData(this.pts.map((p) => this.lineDatum(p)));
  }

  private applyCandles(): void {
    if (!this.candles) return;
    this.candles.setData(this.bars.map(candleDatum));
  }

  private applyVolume(): void {
    if (!this.volume) return;
    this.volume.setData(this.bars.map((b) => this.volDatum(b)));
  }

  private applyBaseline(): void {
    if (this.baseLine) {
      try {
        this.line.removePriceLine(this.baseLine);
      } catch {
        /* series replaced */
      }
      try {
        this.candles?.removePriceLine(this.baseLine);
      } catch {
        /* not on candles */
      }
      this.baseLine = null;
    }
    const d = this.data;
    if (!d || !d.showBaseline || d.baseline === null || !Number.isFinite(d.baseline)) return;
    const target = this.style === "candles" && this.candles ? this.candles : this.line;
    this.baseLine = target.createPriceLine({
      price: d.baseline,
      color: palette.text3(),
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      axisLabelVisible: false,
      title: "",
    });
  }

  private onCrosshair(p: MouseEventParams): void {
    const lbl = this.timeLabel;
    const series = this.style === "candles" && this.candles ? this.candles : this.line;
    const d = p.time !== undefined && p.point ? p.seriesData.get(series) : undefined;
    if (!d || !p.point || p.point.x < 0 || !this.data) {
      if (lbl) lbl.style.display = "none";
      this.opts.onHover?.(null);
      return;
    }
    const t = fromTime(d.time);
    let v: number;
    let bar: Bar | undefined;
    if ("value" in d && typeof d.value === "number") v = d.value;
    else if ("close" in d && typeof d.close === "number") v = d.close;
    else {
      this.opts.onHover?.(null);
      return;
    }
    if (this.bars.length) {
      // binary search the bar for the OHLC readout
      bar = findBar(this.bars, t);
    }
    if (lbl) {
      lbl.textContent = formatHoverTime(t, this.data.labelKind);
      const w = lbl.offsetWidth || 80;
      const hostW = this.host.clientWidth;
      const x = Math.max(w / 2, Math.min(hostW - w / 2, p.point.x));
      lbl.style.left = `${x}px`;
      lbl.style.display = "block";
    }
    this.opts.onHover?.({ t, v, bar });
  }
}

/** Is `t` inside the bar that starts at `barT` for this timeframe? */
function sameBucket(t: number, barT: number, step: number): boolean {
  if (step > DAY) return t >= barT && t < barT + step;
  if (step === DAY) return etMidnight(t) === etMidnight(barT);
  return Math.floor(t / step) === Math.floor(barT / step) || (t >= barT && t < barT + step);
}

function combine(t: number, base: Bar | null, minutes: Map<number, Bar>): Bar {
  const ms = [...minutes.values()].sort((a, b) => a.t - b.t);
  // only minutes newer than the base bar's start add volume beyond what the base already contained
  let o = base?.o ?? ms[0]?.o ?? 0;
  let h = base?.h ?? -Infinity;
  let l = base?.l ?? Infinity;
  let c = base?.c ?? 0;
  let v = base?.v ?? 0;
  for (const m of ms) {
    if (!base && m === ms[0]) o = m.o;
    h = Math.max(h, m.h);
    l = Math.min(l, m.l);
    c = m.c;
    v += m.v;
  }
  if (!Number.isFinite(h)) h = c;
  if (!Number.isFinite(l)) l = c;
  return { t, o, h, l, c, v };
}

function findBar(bars: Bar[], t: number): Bar | undefined {
  let lo = 0;
  let hi = bars.length - 1;
  const ts = Math.floor(t / 1000);
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const mt = Math.floor(bars[mid].t / 1000);
    if (mt === ts) return bars[mid];
    if (mt < ts) lo = mid + 1;
    else hi = mid - 1;
  }
  return undefined;
}

function candleDatum(b: Bar): CandlestickData<Time> {
  return { time: toTime(b.t), open: b.o, high: b.h, low: b.l, close: b.c };
}

function dedupePoints(pts: EquityPoint[]): EquityPoint[] {
  const sorted = pts.filter((p) => Number.isFinite(p.v) && Number.isFinite(p.t)).sort((a, b) => a.t - b.t);
  const out: EquityPoint[] = [];
  for (const p of sorted) {
    const last = out[out.length - 1];
    if (last && Math.floor(last.t / 1000) === Math.floor(p.t / 1000)) out[out.length - 1] = p;
    else out.push(p);
  }
  return out;
}

function dedupeBars(bars: Bar[]): Bar[] {
  const sorted = bars.filter((b) => Number.isFinite(b.c)).sort((a, b) => a.t - b.t);
  const out: Bar[] = [];
  for (const b of sorted) {
    const last = out[out.length - 1];
    if (last && Math.floor(last.t / 1000) === Math.floor(b.t / 1000)) out[out.length - 1] = b;
    else out.push(b);
  }
  return out;
}
