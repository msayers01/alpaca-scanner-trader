import { memo, useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../lib/api";
import type { Position } from "../lib/types";
import { describeOcc, fmtInt, fmtNum, fmtPct, fmtUsd, signClass } from "../lib/format";
import { useAccountStore } from "../store/account";
import { useStatusStore } from "../store/status";
import { symbolHref } from "./DecisionItem";
import { ConfirmDialog } from "./ui/Modal";
import { Empty } from "./ui/States";
import { toast } from "./ui/Toast";
import { ModeBadge } from "./StatusBits";

const same = (a: Position, b: Position) =>
  a.symbol === b.symbol &&
  a.qty === b.qty &&
  a.current_price === b.current_price &&
  a.market_value === b.market_value &&
  a.unrealized_pl === b.unrealized_pl &&
  a.change_today_pct === b.change_today_pct &&
  a.avg_entry_price === b.avg_entry_price &&
  a.strategy_id === b.strategy_id &&
  JSON.stringify(a.exit) === JSON.stringify(b.exit);

const Row = memo(
  function Row({ p, onOpen, onClose }: { p: Position; onOpen: (p: Position) => void; onClose: (p: Position) => void }) {
    const isOpt = p.asset_class === "option";
    return (
      <tr className="clickable" onClick={() => onOpen(p)}>
        <td className="sym">
          <div>{isOpt ? describeOcc(p.symbol) : p.symbol}</div>
          {isOpt && p.option && (
            <div className="xsmall faint">
              {p.option.dte}d · Δ {fmtNum(p.option.delta ?? null, 2)} · IV {fmtPct(p.option.iv ?? null, { decimals: 0 })}
            </div>
          )}
        </td>
        <td className="num">
          {p.side === "short" ? "−" : ""}
          {fmtNum(Math.abs(p.qty), isOpt || Number.isInteger(p.qty) ? 0 : 4)}
        </td>
        <td className="num">{fmtUsd(p.avg_entry_price)}</td>
        <td className="num">{fmtUsd(p.current_price)}</td>
        <td className="num">{fmtUsd(p.market_value)}</td>
        <td className={`num ${signClass(p.change_today_pct)}`}>{fmtPct(p.change_today_pct, { signed: true })}</td>
        <td className={`num ${signClass(p.unrealized_pl)}`}>
          {fmtUsd(p.unrealized_pl, { signed: true })}
          <div className="xsmall">{fmtPct(p.unrealized_plpc, { signed: true })}</div>
        </td>
        <td className="small muted">{p.strategy_id ?? "manual"}</td>
        <td className="small muted num">
          {p.exit ? (
            <>
              {p.exit.stop_price !== undefined && <div>stop {fmtUsd(p.exit.stop_price)}</div>}
              {p.exit.target_price !== undefined && <div>tgt {fmtUsd(p.exit.target_price)}</div>}
              {p.exit.trail_pct !== undefined && <div>trail {fmtPct(p.exit.trail_pct, { decimals: 0 })}</div>}
            </>
          ) : (
            "—"
          )}
        </td>
        <td className="right">
          <button
            className="btn btn-sm"
            onClick={(e) => {
              e.stopPropagation();
              onClose(p);
            }}
          >
            Close
          </button>
        </td>
      </tr>
    );
  },
  (a, b) => same(a.p, b.p) && a.onOpen === b.onOpen && a.onClose === b.onClose,
);

export function PositionsTable({ positions }: { positions: Position[] }) {
  const navigate = useNavigate();
  const mode = useStatusStore((s) => s.status?.mode);
  const [closing, setClosing] = useState<Position | null>(null);
  const [pct, setPct] = useState(1);
  const onOpen = useCallback((p: Position) => navigate(symbolHref(p.symbol)), [navigate]);
  const onClose = useCallback((p: Position) => {
    setPct(1);
    setClosing(p);
  }, []);

  if (!positions.length) return <Empty title="No open positions">Positions opened manually or by the auto-trader appear here.</Empty>;

  const totalMv = positions.reduce((s, p) => s + p.market_value, 0);
  const totalPl = positions.reduce((s, p) => s + p.unrealized_pl, 0);
  const isOpt = closing?.asset_class === "option";
  const closeQty = closing ? Math.abs(closing.qty) * pct : 0;

  return (
    <>
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="num">Qty</th>
              <th className="num">Avg cost</th>
              <th className="num">Price</th>
              <th className="num">Market value</th>
              <th className="num">Today</th>
              <th className="num">Total P&amp;L</th>
              <th>Strategy</th>
              <th>Exit plan</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <Row key={p.symbol} p={p} onOpen={onOpen} onClose={onClose} />
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td className="muted small">{fmtInt(positions.length)} positions</td>
              <td colSpan={3} />
              <td className="num">{fmtUsd(totalMv)}</td>
              <td />
              <td className={`num ${signClass(totalPl)}`}>{fmtUsd(totalPl, { signed: true })}</td>
              <td colSpan={3} />
            </tr>
          </tfoot>
        </table>
      </div>
      {closing && (
        <ConfirmDialog
          title={
            <span className="row">
              Close {isOpt ? describeOcc(closing.symbol) : closing.symbol} <ModeBadge mode={mode} />
            </span>
          }
          confirmLabel={mode === "live" ? "Close with real money" : "Close position"}
          tone={closing.side === "long" ? "down" : "up"}
          onClose={() => setClosing(null)}
          onConfirm={async () => {
            try {
              const o = await api.closePosition(closing.symbol, pct === 1 ? undefined : pct);
              toast.success(`Close order ${o.status} — ${o.symbol}`);
              void useAccountStore.getState().loadOrders();
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <div className="seg" role="group" aria-label="Portion to close">
            {[1, 0.5, 0.25].map((v) => (
              <button key={v} className={pct === v ? "active" : ""} onClick={() => setPct(v)} type="button">
                {v * 100}%
              </button>
            ))}
          </div>
          <dl className="confirm-grid">
            <dt>Action</dt>
            <dd>{closing.side === "long" ? "Sell" : "Buy to cover"} at market</dd>
            <dt>Quantity</dt>
            <dd>
              ~{fmtNum(closeQty, isOpt ? 0 : 4)} {isOpt ? "contracts" : "shares"}
            </dd>
            <dt>Est. proceeds</dt>
            <dd>{fmtUsd(Math.abs(closing.market_value) * pct)}</dd>
            <dt>Unrealised P&amp;L</dt>
            <dd className={signClass(closing.unrealized_pl)}>{fmtUsd(closing.unrealized_pl * pct, { signed: true })}</dd>
          </dl>
          {mode === "live" && <div className="notice danger">LIVE mode — this sends a real order.</div>}
        </ConfirmDialog>
      )}
    </>
  );
}
