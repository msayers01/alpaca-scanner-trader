// Order ticket for equities (shares or $ notional) and option contracts (whole contracts).
// Always confirms with an estimate and the current trading mode before sending.
import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../lib/api";
import type { OptionRow, OrderRequest } from "../lib/types";
import { describeOcc, fmtNum, fmtUsd } from "../lib/format";
import { useAccountStore } from "../store/account";
import { useOptionQuote, useQuote } from "../store/quotes";
import { useStatusStore } from "../store/status";
import { ConfirmDialog } from "./ui/Modal";
import { NumberInput } from "./ui/ParamForm";
import { toast } from "./ui/Toast";
import { ModeBadge } from "./StatusBits";

type Side = "buy" | "sell";

interface Props {
  symbol: string;
  /** If set, the ticket trades this option contract (OCC); `symbol` is then the underlying. */
  option?: OptionRow | null;
  defaultSide?: Side;
}

const round2 = (v: number) => Math.round(v * 100) / 100;

export function OrderTicket({ symbol, option, defaultSide = "buy" }: Props) {
  const isOpt = !!option;
  const mode = useStatusStore((s) => s.status?.mode);
  const session = useStatusStore((s) => s.status?.market.session);
  const account = useAccountStore((s) => s.account);
  const position = useAccountStore((s) => s.positions.find((p) => p.symbol === (option?.symbol ?? symbol.toUpperCase())));
  const q = useQuote(isOpt ? null : symbol);
  const oq = useOptionQuote(isOpt ? option.symbol : null);

  const [side, setSide] = useState<Side>(defaultSide);
  const [amountMode, setAmountMode] = useState<"qty" | "notional">("qty");
  const [qty, setQty] = useState<number>(1);
  const [notional, setNotional] = useState<number>(100);
  const [type, setType] = useState<"market" | "limit">(isOpt ? "limit" : "market");
  const [limit, setLimit] = useState<number | null>(null);
  const [tif, setTif] = useState<"day" | "gtc">("day");
  const [ext, setExt] = useState(false);
  const [confirming, setConfirming] = useState(false);

  // reset when instrument changes
  const instrument = option?.symbol ?? symbol;
  useEffect(() => {
    setLimit(null);
    setQty(1);
    setType(isOpt ? "limit" : "market");
    setAmountMode("qty");
    setExt(false);
  }, [instrument, isOpt]);

  const bid = isOpt ? oq?.bid ?? option.bid : q?.bid ?? 0;
  const ask = isOpt ? oq?.ask ?? option.ask : q?.ask ?? 0;
  const mid = isOpt ? oq?.mid ?? option.mid : bid > 0 && ask > 0 ? (bid + ask) / 2 : q?.last ?? 0;
  const last = isOpt ? option.last : q?.last ?? 0;
  const refPrice = side === "buy" ? ask || mid || last : bid || mid || last;

  // default limit = mid, set once per instrument when data arrives
  useEffect(() => {
    if (limit === null && mid > 0) setLimit(round2(mid));
  }, [mid, limit]);

  const mult = isOpt ? 100 : 1;
  const px = type === "limit" ? limit ?? 0 : refPrice;
  const useNotional = !isOpt && amountMode === "notional";
  const estShares = useNotional ? (px > 0 ? notional / px : 0) : qty;
  const estCost = useNotional ? notional : qty * px * mult;
  const bp = isOpt ? account?.options_buying_power : account?.buying_power;

  const problems = useMemo(() => {
    const out: string[] = [];
    if (useNotional ? !(notional >= 1) : !(qty > 0)) out.push(useNotional ? "Enter a dollar amount of at least $1." : "Enter a quantity.");
    if (isOpt && !Number.isInteger(qty)) out.push("Options trade in whole contracts.");
    if (type === "limit" && !(limit && limit > 0)) out.push("Enter a limit price.");
    if (useNotional && tif !== "day") out.push("Dollar (fractional) orders must be DAY.");
    if (ext && (type !== "limit" || tif !== "day")) out.push("Extended hours requires a DAY limit order.");
    if (!mode) out.push("Backend offline.");
    return out;
  }, [useNotional, notional, qty, isOpt, type, limit, tif, ext, mode]);

  const warnings: string[] = [];
  if (side === "buy" && bp !== undefined && estCost > bp) warnings.push(`Estimated cost exceeds ${isOpt ? "options " : ""}buying power (${fmtUsd(bp)}).`);
  if (isOpt && type === "market") warnings.push("Market orders on options can fill far from mid on wide spreads.");
  if (session && session !== "regular" && (isOpt || !ext)) warnings.push(isOpt ? "Options only trade during regular hours." : "Market is not in regular session; order queues until open unless extended hours is set.");
  if (side === "sell" && !position) warnings.push(isOpt ? "No open position: this would open a short option (likely rejected)." : "No open position: this would open a short sale.");

  const request = (): OrderRequest => {
    const r: OrderRequest = { symbol: (option?.symbol ?? symbol).toUpperCase(), side, type };
    if (useNotional) r.notional = round2(notional);
    else r.qty = qty;
    if (type === "limit" && limit) r.limit_price = round2(limit);
    r.time_in_force = isOpt ? "day" : tif;
    if (!isOpt && ext) r.extended_hours = true;
    return r;
  };

  const title = isOpt ? describeOcc(option.symbol) : symbol.toUpperCase();

  return (
    <div className="ticket">
      <div className="seg" role="group" aria-label="Side" style={{ alignSelf: "stretch" }}>
        <button className={`${side === "buy" ? "active buy" : ""}`} style={{ flex: 1 }} onClick={() => setSide("buy")} aria-pressed={side === "buy"}>
          Buy
        </button>
        <button className={`${side === "sell" ? "active sell" : ""}`} style={{ flex: 1 }} onClick={() => setSide("sell")} aria-pressed={side === "sell"}>
          Sell
        </button>
      </div>

      <div className="line">
        <span className="muted">Order type</span>
        <div className="seg" role="group" aria-label="Order type">
          <button className={type === "market" ? "active" : ""} onClick={() => setType("market")}>
            Market
          </button>
          <button className={type === "limit" ? "active" : ""} onClick={() => setType("limit")}>
            Limit
          </button>
        </div>
      </div>

      {!isOpt && (
        <div className="line">
          <span className="muted">Buy in</span>
          <div className="seg" role="group" aria-label="Amount type">
            <button className={amountMode === "qty" ? "active" : ""} onClick={() => setAmountMode("qty")}>
              Shares
            </button>
            <button
              className={amountMode === "notional" ? "active" : ""}
              onClick={() => {
                setAmountMode("notional");
                setTif("day");
              }}
            >
              Dollars
            </button>
          </div>
        </div>
      )}

      <div className="line">
        <label className="muted" htmlFor="ticket-amount">
          {isOpt ? "Contracts" : useNotional ? "Amount" : "Shares"}
        </label>
        {useNotional ? (
          <div className="input-group" style={{ width: 130 }}>
            <span className="addon">$</span>
            <NumberInput value={notional} onChange={setNotional} min={0} className="input input-num" ariaLabel="Dollar amount" />
          </div>
        ) : (
          <span id="ticket-amount">
            <NumberInput value={qty} onChange={setQty} min={0} integer={isOpt} className="input input-num" ariaLabel={isOpt ? "Contracts" : "Shares"} />
          </span>
        )}
      </div>

      {type === "limit" && (
        <div className="line">
          <span className="muted">Limit price</span>
          <div className="input-group" style={{ width: 130 }}>
            <span className="addon">$</span>
            <NumberInput value={limit} onChange={setLimit} min={0} className="input input-num" ariaLabel="Limit price" />
          </div>
        </div>
      )}
      {type === "limit" && (
        <div className="row-wrap small" style={{ justifyContent: "flex-end" }}>
          <button className="btn btn-xs" onClick={() => setLimit(round2(bid))} disabled={!bid}>
            Bid {fmtNum(bid)}
          </button>
          <button className="btn btn-xs" onClick={() => setLimit(round2(mid))} disabled={!mid}>
            Mid {fmtNum(mid)}
          </button>
          <button className="btn btn-xs" onClick={() => setLimit(round2(ask))} disabled={!ask}>
            Ask {fmtNum(ask)}
          </button>
        </div>
      )}

      {!isOpt && (
        <div className="line">
          <span className="muted">Time in force</span>
          <select className="select input-sm" style={{ width: 130 }} value={tif} onChange={(e) => setTif(e.target.value as "day" | "gtc")} disabled={useNotional}>
            <option value="day">Day</option>
            <option value="gtc">Good till cancelled</option>
          </select>
        </div>
      )}
      {!isOpt && type === "limit" && tif === "day" && (
        <label className="checkbox small">
          <input type="checkbox" checked={ext} onChange={(e) => setExt(e.target.checked)} />
          Allow extended hours (pre/post-market)
        </label>
      )}

      <div className="line">
        <span className="muted">{type === "market" ? `Est. price (${side === "buy" ? "ask" : "bid"})` : "Limit"}</span>
        <span className="num">{fmtUsd(px || null)}</span>
      </div>
      <div className="line total">
        <span>{side === "buy" ? "Estimated cost" : "Estimated credit"}</span>
        <span className="num">{fmtUsd(estCost || null)}</span>
      </div>
      {useNotional && px > 0 && <div className="small muted right num">≈ {fmtNum(estShares, 4)} shares</div>}
      <div className="small faint">
        {isOpt ? "Options buying power" : "Buying power"}: <span className="num">{fmtUsd(bp)}</span>
        {position && (
          <>
            {" "}
            · Holding <span className="num">{fmtNum(Math.abs(position.qty), isOpt ? 0 : 4)}</span>
          </>
        )}
      </div>

      {problems.length > 0 && <div className="small warn-text">{problems[0]}</div>}

      <button className={`btn btn-lg btn-block ${side === "buy" ? "btn-up" : "btn-down"}`} disabled={problems.length > 0} onClick={() => setConfirming(true)}>
        Review order
      </button>
      <div className="row small faint" style={{ justifyContent: "center" }}>
        Trading in <ModeBadge mode={mode} />
      </div>

      {confirming && (
        <ConfirmDialog
          title={
            <span className="row">
              {side === "buy" ? "Buy" : "Sell"} {title} <ModeBadge mode={mode} />
            </span>
          }
          confirmLabel={mode === "live" ? `Submit ${side} (real money)` : `Submit ${side}`}
          tone={side === "buy" ? "up" : "down"}
          onClose={() => setConfirming(false)}
          onConfirm={async () => {
            try {
              const o = await api.placeOrder(request());
              toast.success(`Order ${o.status}: ${o.side} ${o.qty ?? fmtUsd(o.notional)} ${o.symbol}`);
              void useAccountStore.getState().loadOrders();
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
        >
          <dl className="confirm-grid">
            <dt>Instrument</dt>
            <dd>{title}</dd>
            <dt>Side</dt>
            <dd className={side === "buy" ? "up" : "down"}>{side.toUpperCase()}</dd>
            <dt>{useNotional ? "Amount" : isOpt ? "Contracts" : "Shares"}</dt>
            <dd>{useNotional ? fmtUsd(notional) : fmtNum(qty, isOpt || Number.isInteger(qty) ? 0 : 4)}</dd>
            <dt>Order</dt>
            <dd>
              {type === "limit" ? `Limit ${fmtUsd(limit)}` : "Market"} · {isOpt ? "DAY" : tif.toUpperCase()}
              {ext ? " · ext. hours" : ""}
            </dd>
            <dt>Reference</dt>
            <dd>
              bid {fmtNum(bid)} / ask {fmtNum(ask)}
            </dd>
            <dt>{side === "buy" ? "Estimated cost" : "Estimated credit"}</dt>
            <dd>{fmtUsd(estCost)}</dd>
          </dl>
          {warnings.map((w) => (
            <div key={w} className="notice warn small">
              {w}
            </div>
          ))}
          {mode === "live" && (
            <div className="notice danger">
              <strong>LIVE mode.</strong> This order uses real money and is sent to Alpaca immediately.
            </div>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}
