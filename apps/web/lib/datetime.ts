/**
 * Hydration-safe datetime helpers for Mission Control.
 *
 * React #418 fires when SSR HTML text does not match the client's first paint.
 * Locale-aware `Intl.DateTimeFormat` / `toLocaleString` and `Date.now()`-based
 * relative labels are the usual culprits (server TZ/ICU ≠ browser TZ/ICU, and
 * relative buckets flip between the two renders).
 *
 * These helpers emit fixed UTC strings with ASCII separators so Node SSR and
 * every browser produce identical text. Relative labels accept an explicit
 * `nowMs` so callers can freeze time in tests or defer relative formatting
 * until after mount.
 */

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

function parseDate(value?: string | null): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** "Aug 12, 2026, 1:25 AM UTC" — medium date + short time, always UTC. */
export function formatDateTime(value?: string | null, fallback = "Unknown time"): string {
  const date = parseDate(value);
  if (!date) return fallback;
  const month = MONTHS[date.getUTCMonth()];
  const day = date.getUTCDate();
  const year = date.getUTCFullYear();
  let hours = date.getUTCHours();
  const minutes = pad2(date.getUTCMinutes());
  const ampm = hours >= 12 ? "PM" : "AM";
  hours = hours % 12;
  if (hours === 0) hours = 12;
  return `${month} ${day}, ${year}, ${hours}:${minutes} ${ampm} UTC`;
}

/** "Aug 12, 2026" — calendar day in UTC. */
export function formatDate(value?: string | null, fallback = "Never"): string {
  const date = parseDate(value);
  if (!date) return fallback;
  return `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}, ${date.getUTCFullYear()}`;
}

/** "Aug 12, 1:25 AM UTC" — short month/day + time. */
export function formatDateTimeShort(value?: string | null, fallback = ""): string {
  const date = parseDate(value);
  if (!date) return fallback;
  const month = MONTHS[date.getUTCMonth()];
  const day = date.getUTCDate();
  let hours = date.getUTCHours();
  const minutes = pad2(date.getUTCMinutes());
  const ampm = hours >= 12 ? "PM" : "AM";
  hours = hours % 12;
  if (hours === 0) hours = 12;
  return `${month} ${day}, ${hours}:${minutes} ${ampm} UTC`;
}

/** "01:25:15 AM UTC" — clock time only. */
export function formatTime(value?: string | null, fallback = ""): string {
  const date = parseDate(value);
  if (!date) return fallback || String(value ?? "");
  let hours = date.getUTCHours();
  const minutes = pad2(date.getUTCMinutes());
  const seconds = pad2(date.getUTCSeconds());
  const ampm = hours >= 12 ? "PM" : "AM";
  hours = hours % 12;
  if (hours === 0) hours = 12;
  return `${pad2(hours)}:${minutes}:${seconds} ${ampm} UTC`;
}

/**
 * Relative age from an explicit "now". Pass a frozen `nowMs` during SSR tests;
 * client UIs should only call this after mount (or wrap with ClientRelativeTime).
 */
export function formatRelativeTime(
  value?: string | null,
  nowMs: number = 0,
  opts: { empty?: string; style?: "long" | "short" } = {},
): string {
  const empty = opts.empty ?? "not yet";
  const style = opts.style ?? "long";
  const date = parseDate(value);
  if (!date) return empty;
  const delta = Math.max(0, nowMs - date.getTime());
  if (!Number.isFinite(delta)) return empty;
  const minutes = Math.floor(delta / 60_000);
  if (style === "short") {
    if (minutes < 1) return "now";
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.floor(hours / 24)}d`;
  }
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** Compact age without the "ago" suffix ("3h 12m"), for deploy banners. */
export function formatAgeCompact(value?: string | null, nowMs: number = 0): string {
  const date = parseDate(value);
  if (!date) return "";
  const ms = nowMs - date.getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}
