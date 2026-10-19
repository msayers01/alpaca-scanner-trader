// Read design tokens from CSS so canvas-drawn charts use the same palette as the DOM.
const cache = new Map<string, string>();

export function cssVar(name: string, fallback = "#888"): string {
  const hit = cache.get(name);
  if (hit) return hit;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const out = v || fallback;
  if (v) cache.set(name, out);
  return out;
}

/** Convert "#rrggbb" to rgba() with alpha. Passes through non-hex values. */
export function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export const palette = {
  up: () => cssVar("--up", "#00c805"),
  down: () => cssVar("--down", "#ff5000"),
  bg: () => cssVar("--bg", "#000000"),
  text: () => cssVar("--text", "#f2f2f0"),
  text2: () => cssVar("--text-2", "#a3a3a8"),
  text3: () => cssVar("--text-3", "#6e6e75"),
  border: () => cssVar("--border", "#232327"),
  borderStrong: () => cssVar("--border-strong", "#34343a"),
  accent: () => cssVar("--accent", "#8ab4ff"),
  font: () => cssVar("--font", "system-ui, sans-serif"),
};
