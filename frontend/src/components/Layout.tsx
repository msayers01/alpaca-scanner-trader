import { Suspense } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { fmtPct, fmtUsd, signClass } from "../lib/format";
import { useGlobalHotkeys, GO_SHORTCUTS } from "../hooks/useHotkeys";
import { useNow } from "../hooks/useNow";
import { useAccountStore } from "../store/account";
import { useStatusStore } from "../store/status";
import { ws } from "../lib/ws";
import { ConnectionDot, ModeBadge, SessionIndicator } from "./StatusBits";
import { SymbolSearch } from "./SymbolSearch";
import { Icon, type IconName } from "./ui/Icon";
import { Loading } from "./ui/States";
import { Toaster } from "./ui/Toast";

const NAV: { to: string; label: string; icon: IconName; key?: string; end?: boolean }[] = [
  { to: "/", label: "Dashboard", icon: "dashboard", key: "d", end: true },
  { to: "/symbol", label: "Symbol", icon: "symbol" },
  { to: "/scanner", label: "Scanner", icon: "scanner", key: "s" },
  { to: "/feeds", label: "Feeds", icon: "feeds", key: "f" },
  { to: "/autotrader", label: "Auto-trader", icon: "bot", key: "a" },
  { to: "/calendar", label: "Calendar", icon: "calendar", key: "c" },
  { to: "/backtest", label: "Backtest", icon: "backtest", key: "b" },
  { to: "/llm", label: "LLM Log", icon: "chat", key: "l" },
];
const NAV2: { to: string; label: string; icon: IconName; key?: string }[] = [
  { to: "/settings", label: "Settings", icon: "settings", key: "t" },
  { to: "/help", label: "Help", icon: "help", key: "h" },
];

function TopEquity() {
  const a = useAccountStore((s) => s.account);
  if (!a) return null;
  const cls = signClass(a.day_pl);
  return (
    <div className="equity" title="Account equity and today's P&L vs previous close">
      <span className="value">{fmtUsd(a.equity)}</span>
      <span className={`small ${cls}`}>
        {fmtUsd(a.day_pl, { signed: true })} ({fmtPct(a.day_pl_pct, { signed: true })})
      </span>
      <span className="xsmall faint hide-md">today</span>
    </div>
  );
}

function OfflineBanner() {
  const conn = useStatusStore((s) => s.conn);
  const reachable = useStatusStore((s) => s.reachable);
  const statusError = useStatusStore((s) => s.statusError);
  const now = useNow(1000);
  if (conn.state === "open") return null;
  const wait = conn.nextRetryAt ? Math.max(0, Math.ceil((conn.nextRetryAt - now) / 1000)) : null;
  const restDown = !reachable || !!statusError;
  return (
    <div className={`banner ${restDown ? "offline" : "warn"}`} role="alert">
      <Icon name="alert" />
      <span style={{ flex: 1 }}>
        {restDown ? (
          <>
            <strong>Backend offline.</strong> Start it with <code>uv run vantage</code> (127.0.0.1:8765).
          </>
        ) : (
          <>
            <strong>Live stream disconnected.</strong> Prices and feeds are paused.
          </>
        )}{" "}
        {conn.state === "connecting" ? "Connecting…" : wait !== null ? `Retrying in ${wait}s` : ""}
      </span>
      <button
        className="btn btn-sm"
        onClick={() => {
          ws.retryNow();
          void useStatusStore.getState().loadStatus();
        }}
      >
        Retry now
      </button>
    </div>
  );
}

function LiveStrip() {
  const mode = useStatusStore((s) => s.status?.mode);
  const modeError = useStatusStore((s) => s.status?.mode_error);
  const accErr = useStatusStore((s) => s.status?.error);
  if (modeError || (accErr && mode !== "sim")) {
    return (
      <div className="banner warn" role="alert">
        <Icon name="alert" />
        <span>{modeError ? `Mode switch failed: ${modeError} (fell back to SIM).` : `Broker account error: ${accErr}`}</span>
      </div>
    );
  }
  if (mode !== "live") return null;
  return (
    <div className="banner live" role="note">
      <ModeBadge mode="live" />
      <span>Live trading — orders use real money.</span>
    </div>
  );
}

export function Layout() {
  useGlobalHotkeys();
  const status = useStatusStore((s) => s.status);
  const lastSymbol = "/symbol";
  return (
    <div className="app">
      <nav className="nav" aria-label="Main">
        <div className="brand">
          <Icon name="logo" size={20} strokeWidth={2.4} />
          <span className="brand-name">Vantage</span>
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to === "/symbol" ? lastSymbol : n.to} end={n.end} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`} title={n.label}>
            <Icon name={n.icon} />
            <span className="nav-label">{n.label}</span>
            {n.key && <span className="kbd-hint">g {n.key}</span>}
          </NavLink>
        ))}
        <div className="nav-section">System</div>
        {NAV2.map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`} title={n.label}>
            <Icon name={n.icon} />
            <span className="nav-label">{n.label}</span>
            {n.key && <span className="kbd-hint">g {n.key}</span>}
          </NavLink>
        ))}
        <div className="nav-footer">
          {status ? `v${status.version}` : "offline"} · <kbd>/</kbd> search
          <div className="sr-only">Shortcuts: {GO_SHORTCUTS.map((s) => `g ${s.key} ${s.label}`).join(", ")}</div>
        </div>
      </nav>
      <header className="topbar">
        <SymbolSearch />
        <div className="spacer" />
        <div className="topbar-group hide-sm">
          <SessionIndicator />
        </div>
        <div className="topbar-group">
          <TopEquity />
        </div>
        <div className="topbar-group">
          <ModeBadge mode={status?.mode} large />
          <ConnectionDot />
        </div>
      </header>
      <main className="main" id="main">
        <OfflineBanner />
        <LiveStrip />
        <Suspense fallback={<Loading label="Loading page" />}>
          <Outlet />
        </Suspense>
      </main>
      <Toaster />
    </div>
  );
}
