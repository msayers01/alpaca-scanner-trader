// Fixed-row-height windowing: renders only the rows in (or near) the viewport.
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

interface Props<T> {
  items: readonly T[];
  rowHeight: number;
  /** Fixed list height in px, or "fill" to use the parent's height (parent must have a height). */
  height: number | "fill";
  overscan?: number;
  getKey: (item: T, index: number) => string;
  renderRow: (item: T, index: number, style: CSSProperties) => ReactNode;
  empty?: ReactNode;
  /** Keep the view pinned to the top when new items are prepended while the user is at the top. */
  stickToTop?: boolean;
  className?: string;
  ariaLabel?: string;
}

function VirtualListInner<T>({ items, rowHeight, height, overscan = 8, getKey, renderRow, empty, stickToTop = true, className, ariaLabel }: Props<T>) {
  const ref = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(typeof height === "number" ? height : 400);
  const firstKey = items.length ? getKey(items[0], 0) : "";
  const prevFirst = useRef(firstKey);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewH(el.clientHeight));
    ro.observe(el);
    setViewH(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  // When items are prepended and the user has scrolled down, keep their rows in place.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (prevFirst.current !== firstKey && el.scrollTop > rowHeight && stickToTop) {
      let added = 0;
      const lim = Math.min(items.length, 1000);
      for (let i = 0; i < lim; i++) {
        if (getKey(items[i], i) === prevFirst.current) {
          added = i;
          break;
        }
      }
      if (added > 0) {
        el.scrollTop += added * rowHeight;
        setScrollTop(el.scrollTop);
      }
    }
    prevFirst.current = firstKey;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [firstKey, rowHeight, stickToTop]);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (el) setScrollTop(el.scrollTop);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [onScroll]);

  const total = items.length * rowHeight;
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const end = Math.min(items.length, Math.ceil((scrollTop + viewH) / rowHeight) + overscan);
  const rows: ReactNode[] = [];
  for (let i = start; i < end; i++) {
    rows.push(renderRow(items[i], i, { top: i * rowHeight, height: rowHeight }));
  }

  return (
    <div
      ref={ref}
      className={`vlist${className ? ` ${className}` : ""}`}
      style={{ height: height === "fill" ? "100%" : height }}
      role="list"
      aria-label={ariaLabel}
      tabIndex={0}
    >
      {items.length === 0 ? (
        empty
      ) : (
        <div className="vlist-inner" style={{ height: total }}>
          {rows}
        </div>
      )}
    </div>
  );
}

export const VirtualList = memo(VirtualListInner) as typeof VirtualListInner;
