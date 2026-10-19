import { memo, useEffect, useMemo } from "react";
import { api } from "../lib/api";
import type { NewsItem } from "../lib/types";
import { fmtNum, fmtWhen, signClass } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { useFeedsStore } from "../store/feeds";
import { Empty, ErrorBox, Loading } from "./ui/States";
import { Icon } from "./ui/Icon";

export function sentimentOf(n: NewsItem): number | null {
  if (n.llm) return n.llm.direction * (n.llm.magnitude || 1);
  return n.sentiment;
}

export const NewsCard = memo(function NewsCard({ n }: { n: NewsItem }) {
  const s = sentimentOf(n);
  const cls = signClass(s);
  return (
    <div className="decision">
      <div className="head">
        <span className={`badge ${cls === "flat" ? "" : cls}`}>{s === null ? "n/a" : fmtNum(s, 2)}</span>
        <a href={n.url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--text)", fontWeight: 600, flex: 1, minWidth: 0 }}>
          {n.headline} <Icon name="external" size={11} />
        </a>
      </div>
      <div className="reason">
        <span className="faint">
          {fmtWhen(n.t)} · {n.source}
          {n.symbols.length > 0 && ` · ${n.symbols.slice(0, 6).join(", ")}`}
        </span>
      </div>
      {n.summary && (
        <div className="reason" style={{ overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
          {n.summary}
        </div>
      )}
      {n.llm?.rationale && (
        <div className="llm">
          LLM: {n.llm.direction > 0 ? "bullish" : n.llm.direction < 0 ? "bearish" : "neutral"} · magnitude {fmtNum(n.llm.magnitude, 2)} · {n.llm.horizon} — {n.llm.rationale}
        </div>
      )}
    </div>
  );
});

/** News for one symbol: REST snapshot merged with live items from the feed store. */
export function SymbolNews({ symbol, limit = 30 }: { symbol: string; limit?: number }) {
  const sym = symbol.toUpperCase();
  const res = useAsync((signal) => api.news(sym, limit, { signal }), [sym, limit]);
  const liveNews = useFeedsStore((s) => s.news);
  useEffect(() => {
    void useFeedsStore.getState().ensureLoaded("news");
  }, []);
  const items = useMemo(() => {
    const m = new Map<string, NewsItem>();
    for (const n of liveNews) if (n.symbols.some((x) => x.toUpperCase() === sym)) m.set(n.id, n);
    for (const n of res.data ?? []) if (!m.has(n.id)) m.set(n.id, n);
    return [...m.values()].sort((a, b) => b.t - a.t).slice(0, limit);
  }, [liveNews, res.data, sym, limit]);
  if (res.error && !items.length) return <ErrorBox error={res.error} onRetry={res.reload} />;
  if (res.loading && !items.length) return <Loading />;
  if (!items.length) return <Empty>No recent news for {sym}.</Empty>;
  return (
    <div>
      {items.map((n) => (
        <NewsCard key={n.id} n={n} />
      ))}
    </div>
  );
}
