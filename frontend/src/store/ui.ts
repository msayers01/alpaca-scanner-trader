import { create } from "zustand";

const LS_KEY = "vantage.ui.v1";

interface Persisted {
  selectedSymbol: string;
  portfolioRange: string;
  priceRange: string;
  priceStyle: "line" | "candles";
  feedTab: string;
  watchlistLocal: string[];
}

function load(): Partial<Persisted> {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? (JSON.parse(raw) as Partial<Persisted>) : {};
  } catch {
    return {};
  }
}

const initial = load();

interface UiState extends Persisted {
  /** Watchlist as known by the backend (Settings.watchlist); falls back to local copy offline. */
  watchlist: string[];
  setSelectedSymbol: (s: string) => void;
  setPortfolioRange: (r: string) => void;
  setPriceRange: (r: string) => void;
  setPriceStyle: (s: "line" | "candles") => void;
  setFeedTab: (t: string) => void;
  setWatchlist: (w: string[]) => void;
}

export const useUiStore = create<UiState>((set) => ({
  selectedSymbol: initial.selectedSymbol ?? "SPY",
  portfolioRange: initial.portfolioRange ?? "1D",
  priceRange: initial.priceRange ?? "1D",
  priceStyle: initial.priceStyle ?? "line",
  feedTab: initial.feedTab ?? "news",
  watchlistLocal: initial.watchlistLocal ?? ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"],
  watchlist: initial.watchlistLocal ?? ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"],
  setSelectedSymbol: (s) => set({ selectedSymbol: s.toUpperCase() }),
  setPortfolioRange: (r) => set({ portfolioRange: r }),
  setPriceRange: (r) => set({ priceRange: r }),
  setPriceStyle: (s) => set({ priceStyle: s }),
  setFeedTab: (t) => set({ feedTab: t }),
  setWatchlist: (w) => set({ watchlist: w, watchlistLocal: w }),
}));

useUiStore.subscribe((s) => {
  const p: Persisted = {
    selectedSymbol: s.selectedSymbol,
    portfolioRange: s.portfolioRange,
    priceRange: s.priceRange,
    priceStyle: s.priceStyle,
    feedTab: s.feedTab,
    watchlistLocal: s.watchlistLocal,
  };
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(p));
  } catch {
    /* storage unavailable */
  }
});
