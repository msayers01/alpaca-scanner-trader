import { useEffect } from "react";
import { Link } from "react-router-dom";
import { PortfolioChart } from "../components/charts/PortfolioChart";
import { ArmButton, AutoTraderStatusLine, KillButton } from "../components/AutoTraderControls";
import { DecisionItem } from "../components/DecisionItem";
import { OrdersTable } from "../components/OrdersTable";
import { PositionsTable } from "../components/PositionsTable";
import { Watchlist } from "../components/Watchlist";
import { Empty, ErrorBox, Loading, Tip } from "../components/ui/States";
import { fmtAgo, fmtUsd } from "../lib/format";
import { useNow } from "../hooks/useNow";
import { useOnReconnect } from "../hooks/useOnReconnect";
import { useAccountStore } from "../store/account";
import { useAutoTraderStore } from "../store/autotrader";
import { useFeedsStore } from "../store/feeds";
import { useStatusStore } from "../store/status";

const TIER_LABEL: Record<string, string> = { micro: "Micro (< $2k)", small: "Small ($2k–10k)", growth: "Growth ($10k–25k)", pro: "Pro (≥ $25k)" };

function AccountStats() {
  const a = useAccountStore((s) => s.account);
  const err = useAccountStore((s) => s.error);
  if (!a) return err ? <ErrorBox error={err} onRetry={() => void useAccountStore.getState().loadAll()} /> : <div className="stats skeleton" style={{ height: 64 }} />;
  const pdtLimited = !a.pattern_day_trader && a.equity < 25_000;
  return (
    <div className="stats">
      <div className="stat">
        <div className="label">Buying power</div>
        <div className="value">{fmtUsd(a.buying_power)}</div>
      </div>
      <div className="stat">
        <div className="label">Cash</div>
        <div className="value">{fmtUsd(a.cash)}</div>
      </div>
      <div className="stat">
        <div className="label">Options BP</div>
        <div className="value">{fmtUsd(a.options_buying_power)}</div>
        <div className="hint">Options level {a.options_level}</div>
      </div>
      <div className="stat">
        <div className="label">
          Day trades
          <Tip text="Round-trip day trades in the last 5 business days. Under $25k equity, a 4th day trade flags the account as a pattern day trader. The risk manager reads this from the broker." />
        </div>
        <div className={`value ${pdtLimited && a.daytrade_count >= 3 ? "warn-text" : ""}`}>
          {a.daytrade_count}
          {pdtLimited ? " / 3" : ""}
        </div>
        <div className="hint">{a.pattern_day_trader ? "PDT flagged" : pdtLimited ? "5-day window" : "Unrestricted"}</div>
      </div>
      <div className="stat">
        <div className="label">
          Tier <Tip text="Account tier by equity. Selects default strategies and risk presets (see Help → Account tiers)." />
        </div>
        <div className="value" style={{ textTransform: "capitalize" }}>
          {a.tier}
        </div>
        <div className="hint">{TIER_LABEL[a.tier] ?? ""}</div>
      </div>
      <div className="stat">
        <div className="label">Account</div>
        <div className="value" style={{ textTransform: "capitalize" }}>
          {a.status.toLowerCase()}
        </div>
        <div className="hint">{a.currency}</div>
      </div>
    </div>
  );
}

function AutoTraderCard() {
  const at = useAutoTraderStore((s) => s.state);
  const err = useAutoTraderStore((s) => s.error);
  const lastCycle = useStatusStore((s) => s.status?.autotrader.last_cycle ?? null);
  const now = useNow(5000);
  useEffect(() => {
    void useAutoTraderStore.getState().load();
  }, []);
  useOnReconnect(() => void useAutoTraderStore.getState().load());
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Auto-trader</span>
        <span className="spacer" />
        <Link to="/autotrader" className="small">
          Open
        </Link>
      </div>
      <div className="card-body stack">
        {err && !at ? <ErrorBox error={err} onRetry={() => void useAutoTraderStore.getState().load()} /> : <AutoTraderStatusLine />}
        <dl className="kv">
          <dt>Last cycle</dt>
          <dd>{lastCycle ? fmtAgo(lastCycle, now) : "never"}</dd>
          <dt>Tier preset</dt>
          <dd style={{ textTransform: "capitalize" }}>{at?.tier ?? "—"}</dd>
          <dt>Trades today</dt>
          <dd>{at?.today.trades ?? "—"}</dd>
          <dt>P&amp;L today</dt>
          <dd className={at ? (at.today.pnl >= 0 ? "up" : "down") : ""}>{at ? fmtUsd(at.today.pnl, { signed: true }) : "—"}</dd>
        </dl>
        <div className="row-wrap">
          <ArmButton size="btn-sm" />
          <KillButton size="btn-sm" />
        </div>
      </div>
    </div>
  );
}

function LatestDecisions() {
  const decisions = useFeedsStore((s) => s.decisions);
  const loaded = useFeedsStore((s) => s.loaded.decisions);
  useEffect(() => {
    void useFeedsStore.getState().ensureLoaded("decisions");
  }, []);
  const top = decisions.slice(0, 5);
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Latest decisions</span>
        <span className="spacer" />
        <Link to="/feeds/decisions" className="small">
          All
        </Link>
      </div>
      {!loaded && !top.length ? <Loading /> : top.length ? top.map((d) => <DecisionItem key={d.id} d={d} showLlm={false} />) : <Empty>No decisions yet. The auto-trader logs every enter/skip/reject here.</Empty>}
    </div>
  );
}

export default function Dashboard() {
  const positions = useAccountStore((s) => s.positions);
  const orders = useAccountStore((s) => s.openOrders);
  const loaded = useAccountStore((s) => s.loaded);
  return (
    <div className="page">
      <div className="grid-main">
        <div className="stack-lg">
          <PortfolioChart height={340} />
          <AccountStats />
          <section>
            <div className="section-title">Positions</div>
            <div className="card">{!loaded ? <Loading /> : <PositionsTable positions={positions} />}</div>
          </section>
          <section>
            <div className="section-title">Open orders</div>
            <div className="card">
              <OrdersTable orders={orders} />
            </div>
          </section>
        </div>
        <aside className="stack">
          <AutoTraderCard />
          <div className="card">
            <div className="card-header">
              <span className="card-title">Watchlist</span>
            </div>
            <Watchlist />
          </div>
          <LatestDecisions />
        </aside>
      </div>
    </div>
  );
}
