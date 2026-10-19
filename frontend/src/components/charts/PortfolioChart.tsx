// Robinhood-style portfolio value chart: black background, no grid/axes, 2px line coloured by
// value vs range baseline, scrubbing header, dotted previous-close baseline on 1D, session-wide
// x-axis that the line "grows" across, live equity ticks via series.update().
import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../lib/api";
import type { EquityPoint, HistoryRange, PortfolioHistory } from "../../lib/types";
import { channels } from "../../lib/ws";
import { palette } from "../../lib/theme";
import { useChannel } from "../../hooks/useChannel";
import { useOnReconnect } from "../../hooks/useOnReconnect";
import { useAccountStore } from "../../store/account";
import { useStatusStore } from "../../store/status";
import { useUiStore } from "../../store/ui";
import { inferStep, labelKindFor, RhChart, type HoverInfo } from "./chartCore";
import { LiveTag, RangeButtons, RhHeader } from "./ChartParts";

export const PORTFOLIO_RANGES = ["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"] as const satisfies readonly HistoryRange[];

export function PortfolioChart({ height = 340 }: { height?: number }) {
  const range = (useUiStore((s) => s.portfolioRange) as HistoryRange) || "1D";
  const setRange = useUiStore((s) => s.setPortfolioRange);
  const mode = useStatusStore((s) => s.status?.mode);
  const connOpen = useStatusStore((s) => s.conn.state === "open");
  const accountEquity = useAccountStore((s) => s.account?.equity ?? null);

  const hostRef = useRef<HTMLDivElement>(null);
  const timeRef = useRef<HTMLDivElement>(null);
  const ctrlRef = useRef<RhChart | null>(null);
  const [hist, setHist] = useState<PortfolioHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState<number | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const [nonce, setNonce] = useState(0);
  const fetchedAt = useRef(0);

  // chart lifecycle
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const ctrl = new RhChart(host, timeRef.current, {
      onHover: setHover,
      onLast: (v) => setLive(v),
    });
    ctrlRef.current = ctrl;
    return () => {
      ctrl.destroy();
      ctrlRef.current = null;
    };
  }, []);

  // data
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .portfolioHistory(range, { signal: ctrl.signal })
      .then((h) => {
        if (ctrl.signal.aborted) return;
        fetchedAt.current = Date.now();
        setHist(h);
        setLoading(false);
        const c = ctrlRef.current;
        if (!c) return;
        const ts = h.points.map((p) => p.t);
        const step = inferStep(ts, h.timeframe);
        c.setData({
          points: h.points,
          baseline: h.base_value,
          showBaseline: range === "1D",
          session: range === "1D" ? h.session_bounds ?? null : null,
          padSession: range === "1D",
          dayBounds: range === "1D" ? h.day_bounds ?? null : null,
          stepMs: step,
          labelKind: labelKindFor(range, step),
        });
        setLive(c.lastValue());
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(errorMessage(e));
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [range, mode, nonce]);

  useOnReconnect(
    useCallback(() => {
      if (error || Date.now() - fetchedAt.current > 3000) setNonce((n) => n + 1);
    }, [error]),
  );

  // live equity ticks (1D only) go straight to the series
  useChannel<EquityPoint>(range === "1D" && hist ? channels.equity : null, (d) => {
    if (d && Number.isFinite(d.v)) ctrlRef.current?.livePoint(d.t, d.v);
  });

  const baseline = hist ? (range === "1D" ? hist.base_value : hist.points[0]?.v ?? hist.base_value) : null;
  const current = live ?? (range === "1D" ? accountEquity : null) ?? hist?.points[hist.points.length - 1]?.v ?? accountEquity;
  const up = current === null || baseline === null ? true : current >= baseline;

  useEffect(() => {
    ctrlRef.current?.setColor(up ? palette.up() : palette.down());
  }, [up, hist]);

  const shown = hover ? hover.v : current;
  const empty = !loading && !error && hist && hist.points.length === 0;

  return (
    <div className="rh">
      <RhHeader title="Portfolio value" value={shown} baseline={baseline} range={range} />
      <div className="rh-canvas" style={{ height }}>
        <div ref={hostRef} className="chart-host" />
        <div ref={timeRef} className="rh-time" />
        {loading && !hist && (
          <div className="rh-overlay-msg">
            <span className="spinner" /> Loading portfolio history
          </div>
        )}
        {error && (
          <div className="rh-overlay-msg interactive">
            <span>Portfolio history unavailable — {error}</span>
            <button className="btn btn-sm" onClick={() => setNonce((n) => n + 1)}>
              Retry
            </button>
          </div>
        )}
        {empty && <div className="rh-overlay-msg">No portfolio history for this range yet</div>}
      </div>
      <RangeButtons ranges={PORTFOLIO_RANGES} value={range} onChange={(r) => setRange(r)} up={up}>
        {range === "1D" && <LiveTag on={connOpen} />}
        {loading && hist && <span className="spinner" aria-label="Loading" />}
      </RangeButtons>
    </div>
  );
}
