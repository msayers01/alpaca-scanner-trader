import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import type { Chain, OptionRow } from "../lib/types";
import { describeOcc, fmtCompact, fmtNum, fmtPct, fmtUsd, parseOcc, signClass } from "../lib/format";
import { PriceChart } from "../components/charts/PriceChart";
import { OptionChart } from "../components/charts/OptionChart";
import { OrderTicket } from "../components/OrderTicket";
import { OptionsChain } from "../components/OptionsChain";
import { SignalPanel } from "../components/SignalPanel";
import { SymbolNews } from "../components/NewsList";
import { LiveBidAsk, LiveOptionMid } from "../components/LivePrice";
import { saveWatchlist } from "../components/Watchlist";
import { Icon } from "../components/ui/Icon";
import { useAsync } from "../hooks/useAsync";
import { useAccountStore } from "../store/account";
import { useQuote } from "../store/quotes";
import { useUiStore } from "../store/ui";

function QuoteStrip({ symbol }: { symbol: string }) {
  const q = useQuote(symbol);
  const snap = useAsync((signal) => api.quote(symbol, { signal }), [symbol]);
  const s = snap.data;
  const prev = q?.prev_close ?? s?.prev_close ?? null;
  const last = q?.last ?? s?.last ?? null;
  const dayChg = last !== null && prev ? (last - prev) / prev : q?.change_pct ?? s?.change_pct ?? null;
  return (
    <div className="stats">
      <div className="stat">
        <div className="label">Bid × Ask</div>
        <div className="value small">
          <LiveBidAsk symbol={symbol} />
        </div>
      </div>
      <div className="stat">
        <div className="label">Day change</div>
        <div className={`value ${signClass(dayChg)}`}>{fmtPct(dayChg, { signed: true })}</div>
      </div>
      <div className="stat">
        <div className="label">Prev. close</div>
        <div className="value">{fmtUsd(prev)}</div>
      </div>
      <div className="stat">
        <div className="label">Volume</div>
        <div className="value">{fmtCompact(q?.volume ?? s?.volume ?? null)}</div>
      </div>
    </div>
  );
}

function PositionStrip({ symbol }: { symbol: string }) {
  const positions = useAccountStore((s) => s.positions);
  const mine = positions.filter((p) => p.symbol === symbol || p.underlying === symbol);
  if (!mine.length) return null;
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Your positions</span>
      </div>
      <table className="tbl compact">
        <tbody>
          {mine.map((p) => (
            <tr key={p.symbol}>
              <td className="sym">{p.asset_class === "option" ? describeOcc(p.symbol) : p.symbol}</td>
              <td className="num">{fmtNum(p.qty, Number.isInteger(p.qty) ? 0 : 4)}</td>
              <td className="num">{fmtUsd(p.market_value)}</td>
              <td className={`num ${signClass(p.unrealized_pl)}`}>
                {fmtUsd(p.unrealized_pl, { signed: true })} ({fmtPct(p.unrealized_plpc, { signed: true })})
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function SymbolPage() {
  const params = useParams();
  const sym = (params.sym ?? "SPY").toUpperCase();
  const [search, setSearch] = useSearchParams();
  const contractParam = search.get("contract")?.toUpperCase() ?? null;
  const setSelectedSymbol = useUiStore((s) => s.setSelectedSymbol);
  const watchlist = useUiStore((s) => s.watchlist);
  const [contract, setContract] = useState<OptionRow | null>(null);
  const [ticketTab, setTicketTab] = useState<"stock" | "option">(contractParam ? "option" : "stock");

  useEffect(() => setSelectedSymbol(sym), [sym, setSelectedSymbol]);
  useEffect(() => {
    if (!contractParam) setContract(null);
  }, [sym, contractParam]);

  const info = useAsync(
    (signal) => api.search(sym, { signal }).then((r) => r.find((x) => x.symbol.toUpperCase() === sym) ?? null),
    [sym],
  );

  const initialExp = useMemo(() => (contractParam ? parseOcc(contractParam)?.expiration ?? null : null), [contractParam]);

  const onSelect = useCallback(
    (r: OptionRow) => {
      setContract(r);
      setTicketTab("option");
      const next = new URLSearchParams(search);
      next.set("contract", r.symbol);
      setSearch(next, { replace: true });
    },
    [search, setSearch],
  );

  // Resolve a preselected contract (?contract=OCC, e.g. from the scanner) once its chain loads.
  const onChain = useCallback(
    (c: Chain) => {
      if (!contractParam) return;
      const hit = [...c.calls, ...c.puts].find((r) => r.symbol.toUpperCase() === contractParam);
      if (hit) setContract((cur) => (cur?.symbol === hit.symbol ? { ...cur, ...hit } : hit));
    },
    [contractParam],
  );

  const clearContract = () => {
    setContract(null);
    setTicketTab("stock");
    const next = new URLSearchParams(search);
    next.delete("contract");
    setSearch(next, { replace: true });
  };

  const inWatch = watchlist.includes(sym);
  const title = (
    <span className="row">
      <span style={{ color: "var(--text)", fontWeight: 700, fontSize: "var(--fs-lg)" }}>{sym}</span>
      {info.data?.name && <span className="muted">{info.data.name}</span>}
      {info.data?.exchange && <span className="badge outline">{info.data.exchange}</span>}
    </span>
  );

  return (
    <div className="page">
      <div className="grid-main">
        <div className="stack-lg">
          <PriceChart symbol={sym} title={title} height={340} />
          <QuoteStrip symbol={sym} />
          <section>
            <div className="section-title">Signals</div>
            <div className="card">
              <SignalPanel symbol={sym} />
            </div>
          </section>
          {contract && (
            <section>
              <div className="row" style={{ marginBottom: 12 }}>
                <div className="section-title" style={{ margin: 0 }}>
                  {describeOcc(contract.symbol)}
                </div>
                <span className="small muted num">
                  mid <LiveOptionMid occ={contract.symbol} fallback={contract.mid} /> · IV {fmtPct(contract.iv, { decimals: 1 })} · Δ {fmtNum(contract.delta, 2)} · Θ{" "}
                  {fmtNum(contract.theta, 3)}/day · OI {fmtCompact(contract.open_interest)}
                </span>
                <span className="spacer" />
                <button className="btn btn-sm btn-ghost" onClick={clearContract}>
                  <Icon name="close" size={12} /> Close
                </button>
              </div>
              <OptionChart occ={contract.symbol} height={240} />
            </section>
          )}
          <section>
            <div className="section-title">Options chain</div>
            <div className="card">
              <OptionsChain underlying={sym} initialExpiration={initialExp} selected={contract?.symbol ?? contractParam} onSelect={onSelect} onChain={onChain} />
            </div>
          </section>
          <section>
            <div className="section-title">News</div>
            <div className="card">
              <SymbolNews symbol={sym} />
            </div>
          </section>
        </div>
        <aside className="stack" style={{ position: "sticky", top: 16 }}>
          <div className="card">
            <div className="card-header">
              <div className="seg" role="tablist" aria-label="Ticket">
                <button role="tab" aria-selected={ticketTab === "stock"} className={ticketTab === "stock" ? "active" : ""} onClick={() => setTicketTab("stock")}>
                  {sym}
                </button>
                <button
                  role="tab"
                  aria-selected={ticketTab === "option"}
                  className={ticketTab === "option" ? "active" : ""}
                  onClick={() => setTicketTab("option")}
                  disabled={!contract}
                  title={contract ? describeOcc(contract.symbol) : "Pick a contract in the chain"}
                >
                  Option
                </button>
              </div>
              <span className="spacer" />
              <button
                className="btn btn-xs btn-ghost"
                onClick={() => void saveWatchlist(inWatch ? watchlist.filter((x) => x !== sym) : [...watchlist, sym])}
                title={inWatch ? "Remove from watchlist" : "Add to watchlist"}
              >
                {inWatch ? "Watching" : "+ Watch"}
              </button>
            </div>
            <div className="card-body">
              {ticketTab === "option" && contract ? (
                <>
                  <div className="small muted" style={{ marginBottom: 8 }}>
                    {describeOcc(contract.symbol)}
                  </div>
                  <OrderTicket symbol={sym} option={contract} />
                </>
              ) : (
                <OrderTicket symbol={sym} />
              )}
            </div>
          </div>
          <PositionStrip symbol={sym} />
        </aside>
      </div>
    </div>
  );
}
