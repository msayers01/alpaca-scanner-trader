import type { ReactNode } from "react";
import { Icon } from "./Icon";

export function Empty({ title, children }: { title?: ReactNode; children?: ReactNode }) {
  return (
    <div className="empty">
      {title && <div className="empty-title">{title}</div>}
      {children}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loading-block" role="status">
      <span className="spinner" />
      {label}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <Icon name="alert" />
      <span style={{ flex: 1 }}>{error}</span>
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Small "?" with a hover/focus tooltip. */
export function Tip({ text, label = "?" }: { text: string; label?: ReactNode }) {
  return (
    <span className="tip" data-tip={text} tabIndex={0} aria-label={text} role="note">
      {label === "?" ? <Icon name="info" size={12} /> : label}
    </span>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
  title,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: ReactNode;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <label className="switch" title={title}>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="track" />
      {label && <span>{label}</span>}
    </label>
  );
}

/** Horizontal bar for a value in [-1, 1] (diverging from centre) or [0, 1] (from left). */
export function ScoreBar({ value, diverging = true, width = 80, tone }: { value: number; diverging?: boolean; width?: number; tone?: "up" | "down" | "neutral" }) {
  const v = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
  const cls = tone ?? (v >= 0 ? "up" : "down");
  const style = diverging
    ? v >= 0
      ? { left: "50%", width: `${v * 50}%` }
      : { left: `${50 + v * 50}%`, width: `${-v * 50}%` }
    : { left: 0, width: `${Math.abs(v) * 100}%` };
  return (
    <div className="bar-track" style={{ width }} aria-hidden="true">
      {diverging && <div className="bar-mid" />}
      <div className={`bar-fill ${cls}`} style={style} />
    </div>
  );
}
