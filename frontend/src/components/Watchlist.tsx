import { memo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { errorMessage } from "../lib/api";
import { useStatusStore } from "../store/status";
import { useUiStore } from "../store/ui";
import { LiveChangePct, LivePrice } from "./LivePrice";
import { Icon } from "./ui/Icon";
import { Empty } from "./ui/States";
import { toast } from "./ui/Toast";

const WatchRow = memo(function WatchRow({ sym, onRemove }: { sym: string; onRemove: (s: string) => void }) {
  const navigate = useNavigate();
  return (
    <div
      className="watch-row"
      role="link"
      tabIndex={0}
      onClick={() => navigate(`/symbol/${encodeURIComponent(sym)}`)}
      onKeyDown={(e) => {
        if (e.key === "Enter") navigate(`/symbol/${encodeURIComponent(sym)}`);
      }}
    >
      <span className="sym">{sym}</span>
      <LivePrice symbol={sym} />
      <LiveChangePct symbol={sym} pill />
      <button
        className="icon-btn"
        aria-label={`Remove ${sym} from watchlist`}
        title="Remove"
        onClick={(e) => {
          e.stopPropagation();
          onRemove(sym);
        }}
      >
        <Icon name="close" size={12} />
      </button>
    </div>
  );
});

export async function saveWatchlist(next: string[]): Promise<void> {
  useUiStore.getState().setWatchlist(next);
  try {
    await useStatusStore.getState().saveSettings({ watchlist: next });
  } catch (e) {
    toast.error(`Watchlist saved locally only: ${errorMessage(e)}`);
  }
}

export function Watchlist() {
  const list = useUiStore((s) => s.watchlist);
  const [add, setAdd] = useState("");
  const remove = (s: string) => void saveWatchlist(list.filter((x) => x !== s));
  return (
    <div>
      {list.length === 0 ? (
        <Empty>Your watchlist is empty.</Empty>
      ) : (
        list.map((s) => <WatchRow key={s} sym={s} onRemove={remove} />)
      )}
      <form
        className="row"
        style={{ padding: "8px 16px" }}
        onSubmit={(e) => {
          e.preventDefault();
          const s = add.trim().toUpperCase();
          if (!s || list.includes(s)) return;
          void saveWatchlist([...list, s]);
          setAdd("");
        }}
      >
        <input className="input input-sm" placeholder="Add symbol" value={add} onChange={(e) => setAdd(e.target.value)} aria-label="Add symbol to watchlist" />
        <button className="btn btn-sm" type="submit" disabled={!add.trim()}>
          <Icon name="plus" size={12} /> Add
        </button>
      </form>
    </div>
  );
}
