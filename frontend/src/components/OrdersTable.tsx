import { memo, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../lib/api";
import type { Order } from "../lib/types";
import { describeOcc, fmtNum, fmtUsd, fmtWhen } from "../lib/format";
import { useAccountStore } from "../store/account";
import { symbolHref } from "./DecisionItem";
import { Empty } from "./ui/States";
import { toast } from "./ui/Toast";

const Row = memo(function Row({ o, cancellable }: { o: Order; cancellable: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <tr>
      <td className="faint num">{fmtWhen(o.submitted_at)}</td>
      <td className="sym">
        <Link to={symbolHref(o.symbol)} style={{ color: "var(--text)" }}>
          {o.asset_class === "option" ? describeOcc(o.symbol) : o.symbol}
        </Link>
      </td>
      <td className={o.side === "buy" ? "up" : "down"}>{o.side.toUpperCase()}</td>
      <td className="num">{o.qty !== null ? fmtNum(o.qty, Number.isInteger(o.qty) ? 0 : 4) : o.notional !== null ? fmtUsd(o.notional) : "—"}</td>
      <td>{o.type}</td>
      <td className="num">{o.limit_price !== null ? fmtUsd(o.limit_price) : "—"}</td>
      <td className="num">
        {fmtNum(o.filled_qty, Number.isInteger(o.filled_qty) ? 0 : 4)}
        {o.filled_avg_price !== null && <span className="faint"> @ {fmtUsd(o.filled_avg_price)}</span>}
      </td>
      <td>
        <span className="badge outline">{o.status}</span>
      </td>
      <td className="small muted">{o.strategy_id ?? "manual"}</td>
      <td className="right">
        {cancellable && (
          <button
            className="btn btn-sm"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await api.cancelOrder(o.id);
                toast.info(`Cancel requested for ${o.symbol}`);
                void useAccountStore.getState().loadOrders();
              } catch (e) {
                toast.error(`Cancel failed: ${errorMessage(e)}`);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy && <span className="spinner" />}
            Cancel
          </button>
        )}
      </td>
    </tr>
  );
});

export function OrdersTable({ orders, cancellable = true, emptyText = "No open orders" }: { orders: Order[]; cancellable?: boolean; emptyText?: string }) {
  if (!orders.length) return <Empty>{emptyText}</Empty>;
  return (
    <div className="table-wrap">
      <table className="tbl compact">
        <thead>
          <tr>
            <th>Submitted</th>
            <th>Symbol</th>
            <th>Side</th>
            <th className="num">Qty / $</th>
            <th>Type</th>
            <th className="num">Limit</th>
            <th className="num">Filled</th>
            <th>Status</th>
            <th>Strategy</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <Row key={o.id} o={o} cancellable={cancellable} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
