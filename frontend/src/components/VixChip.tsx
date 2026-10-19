import { Link } from "react-router-dom";
import { fmtPct } from "../lib/format";
import { useStatusStore } from "../store/status";
import type { VixLabel } from "../lib/types";

const TONE: Record<VixLabel, string> = { calm: "up", normal: "", elevated: "warn", stressed: "warn", panic: "down" };

/** Top-bar volatility regime chip (VIX level, day change, regime). Hidden when intel is off or has no VIX yet. */
export function VixChip() {
  const intel = useStatusStore((s) => s.status?.intel);
  if (!intel?.enabled || intel.vix === null || !intel.regime) return null;
  const chg = intel.vix_chg ?? 0;
  const tone = TONE[intel.regime];
  const label = `VIX ${intel.vix.toFixed(1)} (${fmtPct(chg, { signed: true, decimals: 1 })} today) — ${intel.regime} volatility regime. Click for the Intel page.`;
  return (
    <Link to="/intel" className="vix-chip" title={label} aria-label={label}>
      <span className="xsmall faint">VIX</span>
      <span className="num">{intel.vix.toFixed(1)}</span>
      <span className={`xsmall num ${chg > 0 ? "down" : chg < 0 ? "up" : "faint"}`}>{fmtPct(chg, { signed: true, decimals: 1 })}</span>
      <span className={`badge ${tone}`}>{intel.regime}</span>
    </Link>
  );
}
