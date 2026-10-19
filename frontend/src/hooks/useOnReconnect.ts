import { useEffect, useRef } from "react";
import { useStatusStore } from "../store/status";

/**
 * Call `cb` whenever the WebSocket (re)opens after this component mounted — i.e. the backend came
 * back. Use it to refetch REST snapshots that live updates build on.
 */
export function useOnReconnect(cb: () => void): void {
  const openCount = useStatusStore((s) => s.openCount);
  const seen = useRef(openCount);
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    if (openCount !== seen.current) {
      seen.current = openCount;
      ref.current();
    }
  }, [openCount]);
}
