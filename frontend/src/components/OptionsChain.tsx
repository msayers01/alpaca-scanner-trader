import { memo, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { Chain, OptionRow } from "../lib/types";
import { fmtCompact, fmtExpiry, fmtNum, fmtPct, fmtPrice, fmtUsd, daysUntil } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, Loading } from "./ui/States";
import { Icon } from "./ui/Icon";

interface Props {
  underlying: string;
  /** Preselected expiration (e.g. from a scanner contract). */
  initialExpiration?: string | null;
  selected: string | null;
  onSelect: (row: OptionRow) => void;
  /** Called whenever the chain loads, so the parent can resolve a preselected contract. */
  onChain?: (c: Chain) => void;
}

interface StrikeRow {
  strike: number;
  call?: OptionRow;
  put?: OptionRow;
}

const NEAR = 12;

const Side = ({ r, itm, sel, side, onSelect }: { r?: OptionRow; itm: boolean; sel: boolean; side: "call" | "put"; onSelect: (r: OptionRow) => void }) => {
  const cls = `${itm ? "itm" : ""} ${sel ? "sel" : ""}`;
  const click = r ? () => onSelect(r) : undefined;
  const title = r ? `${r.symbol} — click for chart & ticket` : undefined;
  const cells = r
    ? side === "call"
      ? [fmtCompact(r.open_interest), fmtCompact(r.volume), fmtPct(r.iv, { decimals: 1 }), fmtNum(r.delta, 2), fmtPrice(r.bid), fmtPrice(r.mid), fmtPrice(r.ask)]
      : [fmtPrice(r.bid), fmtPrice(r.mid), fmtPrice(r.ask), fmtNum(r.delta, 2), fmtPct(r.iv, { decimals: 1 }), fmtCompact(r.volume), fmtCompact(r.open_interest)]
    : Array(7).fill("");
  const midIdx = side === "call" ? 5 : 1;
  return (
    <>
      {cells.map((c, i) => (
        <td key={i} className={`num ${cls}`} onClick={click} title={title} style={i === midIdx ? { color: "var(--text)", fontWeight: 600 } : undefined}>
          {c}
        </td>
      ))}
    </>
  );
};

const ChainRow = memo(function ChainRow({ row, spot, atm, selected, onSelect }: { row: StrikeRow; spot: number; atm: boolean; selected: string | null; onSelect: (r: OptionRow) => void }) {
  return (
    <tr className={atm ? "atm" : ""}>
      <Side r={row.call} itm={row.strike < spot} sel={!!row.call && row.call.symbol === selected} side="call" onSelect={onSelect} />
      <td className="strike-col num">{fmtPrice(row.strike, row.strike % 1 === 0 ? 0 : 2)}</td>
      <Side r={row.put} itm={row.strike > spot} sel={!!row.put && row.put.symbol === selected} side="put" onSelect={onSelect} />
    </tr>
  );
});

export function OptionsChain({ underlying, initialExpiration, selected, onSelect, onChain }: Props) {
  const exps = useAsync((signal) => api.expirations(underlying, { signal }), [underlying]);
  const [exp, setExp] = useState<string | null>(initialExpiration ?? null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    setExp(initialExpiration ?? null);
  }, [underlying, initialExpiration]);

  useEffect(() => {
    if (!exps.data?.length) return;
    if (exp && exps.data.includes(exp)) return;
    if (initialExpiration && exps.data.includes(initialExpiration)) {
      setExp(initialExpiration);
      return;
    }
    // default: first expiration at least 7 days out, else the nearest
    setExp(exps.data.find((d) => daysUntil(d) >= 7) ?? exps.data[0]);
  }, [exps.data, exp, initialExpiration]);

  const chain = useAsync((signal) => api.chain(underlying, exp!, { signal }), [underlying, exp], { enabled: !!exp });
  const onChainRef = onChain;
  useEffect(() => {
    if (chain.data) onChainRef?.(chain.data);
  }, [chain.data, onChainRef]);

  // refresh the visible chain every 30 s
  const reload = chain.reload;
  useEffect(() => {
    if (!exp) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, 30_000);
    return () => window.clearInterval(id);
  }, [exp, reload]);

  const rows = useMemo<StrikeRow[]>(() => {
    const c = chain.data;
    if (!c) return [];
    const m = new Map<number, StrikeRow>();
    for (const r of c.calls) m.set(r.strike, { ...(m.get(r.strike) ?? { strike: r.strike }), call: r });
    for (const r of c.puts) m.set(r.strike, { ...(m.get(r.strike) ?? { strike: r.strike }), put: r });
    return [...m.values()].sort((a, b) => a.strike - b.strike);
  }, [chain.data]);

  const spot = chain.data?.spot ?? 0;
  const atmIdx = useMemo(() => {
    let best = -1;
    let bd = Infinity;
    rows.forEach((r, i) => {
      const d = Math.abs(r.strike - spot);
      if (d < bd) {
        bd = d;
        best = i;
      }
    });
    return best;
  }, [rows, spot]);

  const visible = useMemo(() => {
    if (showAll || atmIdx < 0) return rows;
    const selIdx = selected ? rows.findIndex((r) => r.call?.symbol === selected || r.put?.symbol === selected) : -1;
    let lo = Math.max(0, atmIdx - NEAR);
    let hi = Math.min(rows.length, atmIdx + NEAR + 1);
    if (selIdx >= 0) {
      lo = Math.min(lo, selIdx);
      hi = Math.max(hi, selIdx + 1);
    }
    return rows.slice(lo, hi);
  }, [rows, atmIdx, showAll, selected]);

  if (exps.error) return <ErrorBox error={`Expirations unavailable: ${exps.error}`} onRetry={exps.reload} />;
  if (exps.loading && !exps.data) return <Loading label="Loading expirations" />;
  if (!exps.data?.length) return <Empty title="No listed options">This symbol has no option expirations available.</Empty>;

  const c = chain.data;
  return (
    <div>
      <div className="row-wrap" style={{ padding: "10px 16px", borderBottom: "1px solid var(--border)" }}>
        <label className="row small muted">
          Expiration
          <select className="select input-sm" style={{ width: 170 }} value={exp ?? ""} onChange={(e) => setExp(e.target.value)}>
            {exps.data.map((d) => (
              <option key={d} value={d}>
                {fmtExpiry(d)} ({daysUntil(d)}d)
              </option>
            ))}
          </select>
        </label>
        {c && (
          <span className="small muted num">
            Spot <strong style={{ color: "var(--text)" }}>{fmtUsd(c.spot)}</strong> · {c.dte} DTE · ATM IV {fmtPct(c.atm_iv, { decimals: 1 })} · Exp. move{" "}
            {c.expected_move !== null ? `±${fmtUsd(c.expected_move)} (${fmtPct(c.spot ? c.expected_move / c.spot : null, { decimals: 1 })})` : "—"}
          </span>
        )}
        <span className="spacer" />
        <button className="btn btn-sm btn-ghost" onClick={() => setShowAll((v) => !v)}>
          {showAll ? `Near the money` : `All ${rows.length} strikes`}
        </button>
        <button className="icon-btn" onClick={chain.reload} aria-label="Refresh chain" title="Refresh chain">
          {chain.loading ? <span className="spinner" /> : <Icon name="refresh" size={14} />}
        </button>
      </div>
      {chain.error && <ErrorBox error={chain.error} onRetry={chain.reload} />}
      {!c && chain.loading && <Loading label="Loading chain" />}
      {c && rows.length === 0 && <Empty>No contracts for this expiration.</Empty>}
      {c && rows.length > 0 && (
        <div className="table-wrap" style={{ maxHeight: 560, overflowY: "auto" }}>
          <table className="tbl compact chain">
            <thead>
              <tr>
                <th colSpan={7} className="side-label">
                  CALLS
                </th>
                <th className="strike-col" />
                <th colSpan={7} className="side-label">
                  PUTS
                </th>
              </tr>
              <tr>
                <th>OI</th>
                <th>Vol</th>
                <th>IV</th>
                <th>Δ</th>
                <th>Bid</th>
                <th>Mid</th>
                <th>Ask</th>
                <th className="strike-col">Strike</th>
                <th>Bid</th>
                <th>Mid</th>
                <th>Ask</th>
                <th>Δ</th>
                <th>IV</th>
                <th>Vol</th>
                <th>OI</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <ChainRow key={r.strike} row={r} spot={spot} atm={rows[atmIdx]?.strike === r.strike} selected={selected} onSelect={onSelect} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
