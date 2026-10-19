import { create } from "zustand";
import { api, errorMessage } from "../lib/api";
import type { Settings, Status } from "../lib/types";
import { ws, type ConnInfo } from "../lib/ws";

interface StatusState {
  status: Status | null;
  statusError: string | null;
  /** True once the backend answered at least once. */
  reachable: boolean;
  settings: Settings | null;
  settingsError: string | null;
  conn: ConnInfo;
  /** Increments every time the WS (re)opens — use as an effect dependency to resync snapshots. */
  openCount: number;
  loadStatus: () => Promise<void>;
  setStatus: (s: Status) => void;
  loadSettings: () => Promise<void>;
  saveSettings: (patch: Partial<Settings>) => Promise<Settings>;
}

export const useStatusStore = create<StatusState>((set) => ({
  status: null,
  statusError: null,
  reachable: false,
  settings: null,
  settingsError: null,
  conn: ws.getState(),
  openCount: 0,
  loadStatus: async () => {
    try {
      const s = await api.status();
      set({ status: s, statusError: null, reachable: true });
    } catch (e) {
      set({ statusError: errorMessage(e) });
    }
  },
  setStatus: (s) => set({ status: s, statusError: null, reachable: true }),
  loadSettings: async () => {
    try {
      const s = await api.settings();
      set({ settings: s, settingsError: null });
    } catch (e) {
      set({ settingsError: errorMessage(e) });
    }
  },
  saveSettings: async (patch) => {
    const s = await api.updateSettings(patch);
    set({ settings: s, settingsError: null });
    return s;
  },
}));

let lastState = ws.getState().state;
ws.onState((conn) => {
  const opened = conn.state === "open" && lastState !== "open";
  lastState = conn.state;
  useStatusStore.setState((s) => ({ conn, openCount: opened ? s.openCount + 1 : s.openCount }));
});

export const useMode = () => useStatusStore((s) => s.status?.mode ?? null);
