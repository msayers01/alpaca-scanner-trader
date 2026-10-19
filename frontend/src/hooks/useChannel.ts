import { useEffect, useRef } from "react";
import { ws } from "../lib/ws";

/** Subscribe to a WS channel for the component's lifetime. The callback may change freely. */
export function useChannel<T = unknown>(ch: string | null | undefined, cb: (data: T) => void): void {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    if (!ch) return;
    return ws.subscribe<T>(ch, (d) => ref.current(d));
  }, [ch]);
}
