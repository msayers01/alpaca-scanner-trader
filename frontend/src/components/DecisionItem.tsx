import { memo } from "react";
import { Link } from "react-router-dom";
import type { Decision } from "../lib/types";
import { fmtNum, fmtPct, fmtWhen, isOcc, parseOcc } from "../lib/format";

export const ActionBadge = ({ action }: { action: Decision["action"] }) => <span className={`badge action-${action}`}>{action}</span>;

export function symbolHref(sym: string): string {
  if (isOcc(sym)) {
    const p = parseOcc(sym);
    if (p) return `/symbol/${encodeURIComponent(p.underlying)}?contract=${encodeURIComponent(sym.toUpperCase())}`;
  }
  return `/symbol/${encodeURIComponent(sym.toUpperCase())}`;
}

export const DecisionItem = memo(function DecisionItem({ d, showLlm = true }: { d: Decision; showLlm?: boolean }) {
  return (
    <div className="decision">
      <div className="head">
        <ActionBadge action={d.action} />
        <Link to={symbolHref(d.symbol)} className="sym" style={{ color: "var(--text)", fontWeight: 600 }}>
          {d.symbol}
        </Link>
        <span className="faint small">{d.strategy_id}</span>
        {d.edge_score !== null && <span className="small muted num">edge {fmtNum(d.edge_score, 2)}</span>}
        {d.order_id && <span className="badge outline">order</span>}
        <span className="spacer" />
        <span className="faint small num">{fmtWhen(d.t)}</span>
      </div>
      <div className="reason">{d.reason}</div>
      {showLlm && d.llm && (
        <div className="llm">
          <div className="small">
            <strong style={{ color: "var(--text)" }}>LLM {d.llm.decision}</strong> · confidence {fmtPct(d.llm.confidence, { decimals: 0 })} · size ×{fmtNum(d.llm.size_multiplier, 2)} ·{" "}
            {d.llm.latency_ms} ms
          </div>
          {d.llm.rationale && <div>{d.llm.rationale}</div>}
        </div>
      )}
    </div>
  );
});
