// Formatting helpers. Market time is always America/New_York.

export const ET = "America/New_York";

const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd4 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const compactUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export const DASH = "—";

/** $1,234.56 (prices under $1 keep up to 4 decimals). */
export function fmtUsd(v: number | null | undefined, opts: { signed?: boolean; decimals?: 0 | 2 } = {}): string {
  if (!isNum(v)) return DASH;
  // P&L / changes (signed or decimals: 2) always use cents; small prices (< $1) keep 4 decimals.
  const f = opts.decimals === 0 ? usd0 : opts.decimals === 2 || opts.signed ? usd2 : Math.abs(v) < 1 && v !== 0 ? usd4 : usd2;
  const s = f.format(Math.abs(v));
  if (opts.signed) return `${v > 0 ? "+" : v < 0 ? "−" : ""}${s}`;
  return v < 0 ? `−${s}` : s;
}

/** Plain price with 2 decimals (no $), e.g. 123.45. */
export function fmtPrice(v: number | null | undefined, decimals = 2): string {
  if (!isNum(v)) return DASH;
  return v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** Fraction → percentage: 0.034 → "3.40%". */
export function fmtPct(v: number | null | undefined, opts: { signed?: boolean; decimals?: number } = {}): string {
  if (!isNum(v)) return DASH;
  const d = opts.decimals ?? 2;
  const s = `${Math.abs(v * 100).toFixed(d)}%`;
  if (opts.signed) return `${v > 0 ? "+" : v < 0 ? "−" : ""}${s}`;
  return v < 0 ? `−${s}` : s;
}

export function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (!isNum(v)) return DASH;
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return v < 0 ? `−${s}` : s;
}

export function fmtSigned(v: number | null | undefined, decimals = 2): string {
  if (!isNum(v)) return DASH;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(decimals)}`;
}

export function fmtInt(v: number | null | undefined): string {
  return isNum(v) ? int.format(v) : DASH;
}

/** 1.2K, 3.4M. */
export function fmtCompact(v: number | null | undefined): string {
  return isNum(v) ? compact.format(v) : DASH;
}

export function fmtCompactUsd(v: number | null | undefined): string {
  return isNum(v) ? compactUsd.format(v) : DASH;
}

// ---------------------------------------------------------------- time (ET)
const timeFmt = new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit" });
const timeSecFmt = new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit", second: "2-digit" });
const dateFmt = new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short", day: "numeric", year: "numeric" });
const dateShortFmt = new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short", day: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat("en-US", { timeZone: ET, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const ymdFmt = new Intl.DateTimeFormat("en-CA", { timeZone: ET, year: "numeric", month: "2-digit", day: "2-digit" });
const partsFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  hourCycle: "h23",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

/** 10:35 AM */
export function fmtTime(ms: number | null | undefined): string {
  return isNum(ms) ? timeFmt.format(ms) : DASH;
}
export function fmtTimeSec(ms: number | null | undefined): string {
  return isNum(ms) ? timeSecFmt.format(ms) : DASH;
}
/** Oct 14, 2026 */
export function fmtDate(ms: number | null | undefined): string {
  return isNum(ms) ? dateFmt.format(ms) : DASH;
}
export function fmtDateShort(ms: number | null | undefined): string {
  return isNum(ms) ? dateShortFmt.format(ms) : DASH;
}
/** Oct 14, 10:35 AM */
export function fmtDateTime(ms: number | null | undefined): string {
  return isNum(ms) ? dateTimeFmt.format(ms) : DASH;
}
/** Same day → time, otherwise short date + time. */
export function fmtWhen(ms: number | null | undefined): string {
  if (!isNum(ms)) return DASH;
  return etYmd(ms) === etYmd(Date.now()) ? timeFmt.format(ms) : dateTimeFmt.format(ms);
}

/** "3m ago", "2h ago". */
export function fmtAgo(ms: number | null | undefined, now = Date.now()): string {
  if (!isNum(ms)) return DASH;
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/** "1h 23m" from milliseconds. */
export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return DASH;
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

/** YYYY-MM-DD in ET for an epoch ms. */
export function etYmd(ms: number): string {
  return ymdFmt.format(ms);
}

export interface EtParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

export function etParts(ms: number): EtParts {
  const p: Record<string, number> = {};
  for (const part of partsFmt.formatToParts(ms)) {
    if (part.type !== "literal") p[part.type] = Number(part.value);
  }
  return { year: p.year, month: p.month, day: p.day, hour: p.hour === 24 ? 0 : p.hour, minute: p.minute, second: p.second };
}

/** Offset (ms) of ET from UTC at the given instant (negative, e.g. −4h in summer). */
export function etOffsetMs(ms: number): number {
  const p = etParts(ms);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUtc - Math.floor(ms / 1000) * 1000;
}

/** Epoch ms of a wall-clock time in ET on the ET date of `refMs`. */
export function etWallTime(refMs: number, hour: number, minute = 0): number {
  const p = etParts(refMs);
  const guess = Date.UTC(p.year, p.month - 1, p.day, hour, minute) - etOffsetMs(refMs);
  // correct across DST boundary
  const off2 = etOffsetMs(guess);
  return Date.UTC(p.year, p.month - 1, p.day, hour, minute) - off2;
}

/** Epoch ms of midnight ET on the ET date of `ms`. */
export function etMidnight(ms: number): number {
  return etWallTime(ms, 0, 0);
}

/** Parse YYYY-MM-DD into a local Date (no TZ shift; used for calendar grid). */
export function parseYmd(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, (m ?? 1) - 1, d ?? 1);
}

export function toYmd(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Days between today (ET) and a YYYY-MM-DD date. */
export function daysUntil(ymd: string): number {
  const today = parseYmd(etYmd(Date.now()));
  return Math.round((parseYmd(ymd).getTime() - today.getTime()) / 86_400_000);
}

export function fmtExpiry(ymd: string): string {
  const d = parseYmd(ymd);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" });
}

// ---------------------------------------------------------------- colour helpers
/** CSS class for a signed value: "up" | "down" | "flat". */
export function signClass(v: number | null | undefined): "up" | "down" | "flat" {
  if (!isNum(v) || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}

export function arrow(v: number | null | undefined): string {
  if (!isNum(v) || v === 0) return "";
  return v > 0 ? "▲" : "▼";
}

// ---------------------------------------------------------------- OCC symbols
export interface OccParts {
  underlying: string;
  expiration: string; // YYYY-MM-DD
  type: "call" | "put";
  strike: number;
}

const OCC_RE = /^([A-Z0-9.]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;

export function parseOcc(sym: string): OccParts | null {
  const m = OCC_RE.exec(sym.toUpperCase());
  if (!m) return null;
  return {
    underlying: m[1],
    expiration: `20${m[2]}-${m[3]}-${m[4]}`,
    type: m[5] === "C" ? "call" : "put",
    strike: Number(m[6]) / 1000,
  };
}

export function isOcc(sym: string): boolean {
  return OCC_RE.test(sym.toUpperCase());
}

/** "AAPL Nov 20 '26 $250 Call" */
export function describeOcc(sym: string): string {
  const p = parseOcc(sym);
  if (!p) return sym;
  const d = parseYmd(p.expiration);
  const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const yy = String(d.getFullYear()).slice(2);
  return `${p.underlying} ${date} '${yy} $${fmtPrice(p.strike, p.strike % 1 === 0 ? 0 : 2)} ${p.type === "call" ? "Call" : "Put"}`;
}

export function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}
