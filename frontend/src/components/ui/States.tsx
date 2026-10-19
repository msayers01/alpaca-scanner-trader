import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
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

const TIP_GAP = 6;
const TIP_MARGIN = 8;

/**
 * Small info icon with a hover/focus tooltip. The bubble is portalled to <body> with fixed positioning so
 * no `overflow: hidden` ancestor (stat strips, cards, tables) or chart canvas can clip it. It prefers to sit
 * above the icon, flips below when there is no room, and is clamped horizontally to the viewport.
 */
export function Tip({ text, label = "?" }: { text: string; label?: ReactNode }) {
  const anchor = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number; below: boolean } | null>(null);
  const id = useId();

  useLayoutEffect(() => {
    if (!open || !anchor.current || !bubble.current) return;
    const place = () => {
      const a = anchor.current!.getBoundingClientRect();
      const b = bubble.current!.getBoundingClientRect();
      const vw = document.documentElement.clientWidth;
      const vh = document.documentElement.clientHeight;
      const roomAbove = a.top - TIP_GAP - TIP_MARGIN;
      const below = roomAbove < b.height && vh - a.bottom - TIP_GAP - TIP_MARGIN >= b.height;
      const top = below ? a.bottom + TIP_GAP : Math.max(TIP_MARGIN, a.top - TIP_GAP - b.height);
      const centered = a.left + a.width / 2 - b.width / 2;
      const left = Math.min(Math.max(TIP_MARGIN, centered), vw - b.width - TIP_MARGIN);
      setPos({ left, top, below });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, text]);

  const show = () => setOpen(true);
  const hide = () => {
    setOpen(false);
    setPos(null);
  };

  return (
    <>
      <span
        ref={anchor}
        className="tip"
        tabIndex={0}
        aria-label={text}
        aria-describedby={open ? id : undefined}
        role="note"
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onKeyDown={(e) => e.key === "Escape" && hide()}
      >
        {label === "?" ? <Icon name="info" size={12} /> : label}
      </span>
      {open &&
        createPortal(
          <div
            ref={bubble}
            id={id}
            role="tooltip"
            className={`tip-pop${pos?.below ? " below" : ""}`}
            style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: -9999 }}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
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
