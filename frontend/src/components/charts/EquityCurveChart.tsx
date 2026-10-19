// Backtest equity curve vs benchmark: two line series on a subtle-axes chart.
import { useEffect, useRef } from "react";
import { createChart, LineSeries, type IChartApi, type Time } from "lightweight-charts";
import type { EquityPoint } from "../../lib/types";
import { palette } from "../../lib/theme";
import { baseChartOptions, toTime } from "./chartCore";

function clean(pts: EquityPoint[]): { time: Time; value: number }[] {
  const sorted = pts.filter((p) => Number.isFinite(p.v)).sort((a, b) => a.t - b.t);
  const out: { time: Time; value: number }[] = [];
  let lastSec = -1;
  for (const p of sorted) {
    const s = toTime(p.t);
    if (s === lastSec) out[out.length - 1] = { time: s, value: p.v };
    else out.push({ time: s, value: p.v });
    lastSec = s;
  }
  return out;
}

export function EquityCurveChart({ equity, benchmark, height = 320 }: { equity: EquityPoint[]; benchmark: EquityPoint[]; height?: number }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const opts = baseChartOptions(true);
    const chart = createChart(host, {
      ...opts,
      crosshair: { ...opts.crosshair, horzLine: { visible: true, labelVisible: true, color: palette.border() } },
      timeScale: { ...opts.timeScale, fixLeftEdge: true, fixRightEdge: true },
    });
    chartRef.current = chart;
    const onDbl = () => chart.timeScale().fitContent();
    host.addEventListener("dblclick", onDbl);
    return () => {
      host.removeEventListener("dblclick", onDbl);
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const bench = chart.addSeries(LineSeries, { color: palette.text3(), lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: "Benchmark" });
    const eq = chart.addSeries(LineSeries, { color: palette.accent(), lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: "Strategy" });
    bench.setData(clean(benchmark));
    eq.setData(clean(equity));
    chart.timeScale().fitContent();
    return () => {
      chart.removeSeries(bench);
      chart.removeSeries(eq);
    };
  }, [equity, benchmark]);

  return (
    <div className="rh-canvas" style={{ height }}>
      <div ref={hostRef} className="chart-host" />
    </div>
  );
}
