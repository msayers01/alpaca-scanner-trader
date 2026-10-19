import { memo, type CSSProperties, type ReactNode } from "react";
import { arrow, fmtPct, fmtUsd } from "../../lib/format";
import { RANGE_LABEL } from "./chartCore";

export const upDownVar = (up: boolean) => (up ? "var(--up)" : "var(--down)");

interface HeaderProps {
  title?: ReactNode;
  value: number | null;
  baseline: number | null;
  range: string;
  /** Extra line under the change (e.g. OHLC on hover). */
  extra?: ReactNode;
  small?: boolean;
  /** Override the label after the change (default: range label). */
  label?: string;
}

/** Big value + "▲ $12.34 (0.56%) Today" line. Colour follows value vs baseline. */
export const RhHeader = memo(function RhHeader({ title, value, baseline, range, extra, small, label }: HeaderProps) {
  const change = value !== null && baseline !== null ? value - baseline : null;
  const pct = change !== null && baseline ? change / baseline : null;
  const cls = change === null ? "flat" : change >= 0 ? "up" : "down";
  return (
    <div className="rh-header">
      {title && <div className="rh-title">{title}</div>}
      <div className={`rh-value${small ? " sm" : ""}`} aria-live="off">
        {fmtUsd(value)}
      </div>
      <div className={`rh-change ${cls}`}>
        {change !== null && <span aria-hidden="true">{arrow(change === 0 ? 1 : change)}</span>}
        <span>{fmtUsd(change !== null ? Math.abs(change) : null, { decimals: Math.abs(baseline ?? 0) < 1 ? undefined : 2 })}</span>
        <span>({fmtPct(pct !== null ? Math.abs(pct) : null)})</span>
        <span className="label">{label ?? RANGE_LABEL[range] ?? range}</span>
      </div>
      {extra}
    </div>
  );
});

interface RangeProps<R extends string> {
  ranges: readonly R[];
  value: R;
  onChange: (r: R) => void;
  up: boolean;
  children?: ReactNode;
}

export function RangeButtons<R extends string>({ ranges, value, onChange, up, children }: RangeProps<R>) {
  return (
    <div className="rh-footer" style={{ "--rh-color": upDownVar(up) } as CSSProperties}>
      <div className="rh-ranges" role="tablist" aria-label="Chart range">
        {ranges.map((r) => (
          <button key={r} role="tab" aria-selected={r === value} className={`rh-range${r === value ? " active" : ""}`} onClick={() => onChange(r)}>
            {r}
          </button>
        ))}
      </div>
      <div className="spacer" />
      {children}
    </div>
  );
}

export function LiveTag({ on }: { on: boolean }) {
  return (
    <span className="rh-live" title={on ? "Streaming live updates" : "Live stream disconnected"}>
      <span className={`dot ${on ? "ok pulse" : ""}`} />
      LIVE
    </span>
  );
}
