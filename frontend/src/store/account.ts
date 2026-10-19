import { create } from "zustand";
import { api, errorMessage } from "../lib/api";
import type { Account, Order, Position } from "../lib/types";

/** Alpaca order statuses that mean "still working". */
const OPEN_STATUSES = new Set([
  "new",
  "accepted",
  "pending_new",
  "partially_filled",
  "pending_replace",
  "pending_cancel",
  "held",
  "accepted_for_bidding",
  "calculated",
  "stopped",
  "suspended",
  "open",
  "submitted",
  "working",
]);

export const isOpenOrder = (o: Order) => OPEN_STATUSES.has(o.status.toLowerCase());

interface AccountState {
  account: Account | null;
  positions: Position[];
  openOrders: Order[];
  /** Most recent order updates received over WS (any status), newest first. */
  recentOrders: Order[];
  loaded: boolean;
  error: string | null;
  loadAll: () => Promise<void>;
  loadAccount: () => Promise<void>;
  loadPositions: () => Promise<void>;
  loadOrders: () => Promise<void>;
  setAccount: (a: Account) => void;
  setPositions: (p: Position[]) => void;
  applyOrder: (o: Order) => void;
}

export const useAccountStore = create<AccountState>((set, get) => ({
  account: null,
  positions: [],
  openOrders: [],
  recentOrders: [],
  loaded: false,
  error: null,
  loadAll: async () => {
    await Promise.all([get().loadAccount(), get().loadPositions(), get().loadOrders()]);
    set({ loaded: true });
  },
  loadAccount: async () => {
    try {
      set({ account: await api.account(), error: null });
    } catch (e) {
      set({ error: errorMessage(e) });
    }
  },
  loadPositions: async () => {
    try {
      set({ positions: await api.positions() });
    } catch (e) {
      set({ error: errorMessage(e) });
    }
  },
  loadOrders: async () => {
    try {
      const orders = await api.orders("open", 200);
      set({ openOrders: orders.filter(isOpenOrder).sort((a, b) => b.submitted_at - a.submitted_at) });
    } catch (e) {
      set({ error: errorMessage(e) });
    }
  },
  setAccount: (a) => set({ account: a, error: null }),
  setPositions: (p) => set({ positions: p }),
  applyOrder: (o) =>
    set((s) => {
      const rest = s.openOrders.filter((x) => x.id !== o.id);
      const openOrders = isOpenOrder(o) ? [o, ...rest].sort((a, b) => b.submitted_at - a.submitted_at) : rest;
      const recentOrders = [o, ...s.recentOrders.filter((x) => x.id !== o.id)].slice(0, 100);
      return { openOrders, recentOrders };
    }),
}));
