import type { Mode, Status } from "../lib/types";
import { fmtDuration } from "../lib/format";
import { useNow } from "../hooks/useNow";
import { useStatusStore } from "../store/status";
import { ws } from "../lib/ws";

export function ModeBadge({ mode, large }: { mode: Mode | null | undefined; large?: boolean }) {
  if (!mode) return <span className={`badge outline${large ? " lg" : ""}`}>offline</span>;
  const label = mode === "sim" ? "SIM" : mode === "paper" ? "PAPER" : "LIVE";
  const title =
    mode === "sim" ? "Simulated market — no broker, synthetic data" : mode === "paper" ? "Alpaca paper trading — fake money, real data" : "LIVE trading — real money";
  return (
    <span className={`badge mode-${mode}${large ? " lg" : ""}`} title={title} aria-label={`Mode: ${label}`}>
      {label}
    </span>
  );
}

const SESSION_LABEL: Record<Status["market"]["session"], string> = {
  pre: "Pre-market",
  regular: "Market open",
  post: "After hours",
  closed: "Market closed",
};

export function SessionIndicator({ compact }: { compact?: boolean }) {
  const market = useStatusStore((s) => s.status?.market);
  const now = useNow(compact ? 30_000 : 10_000);
  if (!market) return <span className="session">—</span>;
  const cls = market.session === "regular" ? "ok" : market.session === "closed" ? "" : "warn";
  // Use the server clock offset so countdowns are right even if the local clock drifts.
  const skew = market.now ? market.now - Date.now() : 0;
  const t = now + skew;
  const target = market.is_open ? market.next_close : market.next_open;
  const countdown = target && target > t ? fmtDuration(target - t) : null;
  return (
    <span className="session" title={countdown ? `${market.is_open ? "Closes" : "Opens"} in ${countdown}` : undefined}>
      <span className={`dot ${cls}`} />
      {SESSION_LABEL[market.session]}
      {!compact && countdown && (
        <span className="faint">
          · {market.is_open ? "closes" : "opens"} in {countdown}
        </span>
      )}
    </span>
  );
}

export function ConnectionDot() {
  const conn = useStatusStore((s) => s.conn);
  const cls = conn.state === "open" ? "ok" : conn.state === "connecting" ? "warn pulse" : "bad";
  const label =
    conn.state === "open" ? `Live stream connected${conn.rttMs !== null ? ` · ${conn.rttMs} ms` : ""}` : conn.state === "connecting" ? "Connecting to stream…" : "Stream disconnected";
  return (
    <button className="icon-btn" title={label} aria-label={label} onClick={() => ws.retryNow()} style={{ width: "auto", padding: "0 6px", gap: 6 }}>
      <span className={`dot ${cls}`} />
      {conn.state === "open" && conn.rttMs !== null && <span className="xsmall faint num hide-sm">{conn.rttMs}ms</span>}
    </button>
  );
}
