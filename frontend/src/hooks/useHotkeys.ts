import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

function isTyping(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

export const SEARCH_INPUT_ID = "global-symbol-search";

export const GO_SHORTCUTS: { key: string; path: string; label: string }[] = [
  { key: "d", path: "/", label: "Dashboard" },
  { key: "s", path: "/scanner", label: "Scanner" },
  { key: "f", path: "/feeds", label: "Feeds" },
  { key: "i", path: "/intel", label: "Intel" },
  { key: "a", path: "/autotrader", label: "Auto-trader" },
  { key: "c", path: "/calendar", label: "Calendar" },
  { key: "b", path: "/backtest", label: "Backtest" },
  { key: "l", path: "/llm", label: "LLM Log" },
  { key: "t", path: "/settings", label: "Settings" },
  { key: "h", path: "/help", label: "Help" },
];

/** Global shortcuts: "/" focuses search; "g" then a letter navigates (within 1.2 s). */
export function useGlobalHotkeys(): void {
  const navigate = useNavigate();
  useEffect(() => {
    let gAt = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (isTyping(e.target)) {
        if (e.key === "Escape" && e.target instanceof HTMLElement) e.target.blur();
        return;
      }
      if (e.key === "/") {
        const el = document.getElementById(SEARCH_INPUT_ID) as HTMLInputElement | null;
        if (el) {
          e.preventDefault();
          el.focus();
          el.select();
        }
        return;
      }
      const now = performance.now();
      if (e.key === "g") {
        gAt = now;
        return;
      }
      if (gAt && now - gAt < 1200) {
        gAt = 0;
        const hit = GO_SHORTCUTS.find((s) => s.key === e.key.toLowerCase());
        if (hit) {
          e.preventDefault();
          navigate(hit.path);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);
}
