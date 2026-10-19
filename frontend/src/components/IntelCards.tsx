import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { fmtNum, fmtPct, signClass } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { useIntelStore } from "../store/intel";
import { useStatusStore } from "../store/status";
import { ScoreBar } from "./ui/States";

const REGIME_TONE: Record<string, string> = { calm: "up", normal: "", elevated: "warn", stressed: "warn", panic: "down" };

/** Dashboard card: volatility regime + risk-on + event risk at a glance. */
export function MarketRegimeCard() {
  const enabled = useStatusStore((s) => s.status?.intel?.enabled);
  const macro = useIntelStore((s) => s.macro);
  useEffect(() => {
    if (enabled) void useIntelStore.getState().load();
  }, [enabled]);
  if (!enabled) return null;
  const v = macro?.vix;
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Market regime</span>
        <span className="spacer" />
        <Link to="/intel" className="small">
          Intel
        </Link>
      </div>
      <div className="card-body stack">
        {!macro ? (
          <div className="skeleton" style={{ height: 70 }} />
        ) : (
          <dl className="kv">
            <dt>VIX</dt>
            <dd>
              {v ? (
                <>
                  {v.vix.toFixed(1)} <span className={`small ${v.chg_1d > 0 ? "down" : "up"}`}>{fmtPct(v.chg_1d, { signed: true, decimals: 1 })}</span>{" "}
                  <span className={`badge ${REGIME_TONE[v.label]}`}>{v.label}</span>
                </>
              ) : (
                "—"
              )}
            </dd>
            <dt>Risk-on</dt>
            <dd>
              <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <ScoreBar value={macro.risk_on} width={80} />
                <span className={`num ${signClass(macro.risk_on)}`}>{fmtNum(macro.risk_on, 2)}</span>
              </span>
            </dd>
            <dt>Event risk</dt>
            <dd>
              <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <ScoreBar value={macro.event_risk} diverging={false} width={80} tone={macro.event_risk > 0.6 ? "down" : "neutral"} />
                <span className="num">{fmtPct(macro.event_risk, { decimals: 0 })}</span>
              </span>
            </dd>
          </dl>
        )}
      </div>
    </div>
  );
}

/** Symbol-page card: retail sentiment/attention for this ticker plus the recent posts count. */
export function SymbolIntelCard({ symbol }: { symbol: string }) {
  const enabled = useStatusStore((s) => s.status?.intel?.enabled);
  const res = useAsync((signal) => api.intelSocial(symbol, { signal }), [symbol], { enabled: !!enabled });
  if (!enabled) return null;
  const s = res.data;
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Social sentiment</span>
        <span className="spacer" />
        <Link to="/intel/social" className="small">
          Intel
        </Link>
      </div>
      <div className="card-body">
        {!s ? (
          <div className="small muted">{res.loading ? "Loading…" : `No recent chatter for ${symbol}.`}</div>
        ) : (
          <dl className="kv">
            <dt>Polarity</dt>
            <dd>
              <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <ScoreBar value={s.polarity} width={80} />
                <span className={`num ${signClass(s.polarity)}`}>{fmtNum(s.polarity, 2)}</span>
              </span>
            </dd>
            <dt>Mentions 24 h</dt>
            <dd>{s.mentions_24h === null ? `${s.n} posts` : `${fmtNum(s.mentions_24h, 0)}${s.rank ? ` · rank #${s.rank}` : ""}`}</dd>
            <dt>Attention</dt>
            <dd className={s.attention > 0.4 ? "up" : s.attention < -0.3 ? "down" : ""}>
              {s.attention >= 0 ? "×" : "÷"}
              {Math.exp(Math.abs(s.attention)).toFixed(1)} vs normal {s.crowded && <span className="badge warn">crowded</span>}
            </dd>
          </dl>
        )}
      </div>
    </div>
  );
}
