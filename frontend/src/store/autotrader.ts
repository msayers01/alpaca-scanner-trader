import { create } from "zustand";
import { api, errorMessage } from "../lib/api";
import type { AutoTraderState } from "../lib/types";
import { useStatusStore } from "./status";

interface AtState {
  state: AutoTraderState | null;
  error: string | null;
  loading: boolean;
  load: () => Promise<void>;
  set: (s: AutoTraderState) => void;
}

export const useAutoTraderStore = create<AtState>((set) => ({
  state: null,
  error: null,
  loading: false,
  load: async () => {
    set({ loading: true });
    try {
      set({ state: await api.autotrader(), error: null, loading: false });
    } catch (e) {
      set({ error: errorMessage(e), loading: false });
    }
  },
  set: (s) => set({ state: s, error: null }),
}));

// Keep the armed/killed flags in sync with the streamed Status (no extra polling).
useStatusStore.subscribe((s, prev) => {
  const a = s.status?.autotrader;
  if (!a || a === prev.status?.autotrader) return;
  const cur = useAutoTraderStore.getState().state;
  if (!cur) return;
  if (cur.armed !== a.armed || cur.killed !== a.killed || cur.running !== a.running || cur.last_cycle !== a.last_cycle || cur.kill_reason !== a.kill_reason) {
    useAutoTraderStore.setState({
      state: { ...cur, armed: a.armed, killed: a.killed, running: a.running, last_cycle: a.last_cycle, kill_reason: a.kill_reason },
    });
  }
});
