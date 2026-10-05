/**
 * One clock for every log in the console (activity-govern spec §4.3).
 *
 * The step panel said `18:02` in UTC, the Activity log `Oct 4, 2026, 6:01 PM UTC` and the river
 * `09:42` local — three clocks for one log. Every log now reads the reader's local time (the
 * Log to the second, the river and step panel to the minute) with the UTC instant in the
 * tooltip, and the same day words: `Today`, `Yesterday`, `Mon 29 Sep`.
 */

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const pad = (n: number) => String(n).padStart(2, "0");

/** `18:02:41` (or `18:02` without seconds), in the reader's own time zone. "" for a bad time. */
export function clock(iso: string, { seconds = true }: { seconds?: boolean } = {}): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return seconds ? `${hm}:${pad(d.getSeconds())}` : hm;
}

/** The tooltip on every time: the instant in UTC, unambiguous. */
export function utcTitle(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().replace(".000Z", "Z");
}

/** `Today` · `Yesterday` · `Mon 29 Sep`, in the reader's own time zone. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const key = (x: Date) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  if (key(d) === key(now)) return "Today";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (key(d) === key(y)) return "Yesterday";
  // Fixed three-letter names: Intl writes "Sept" in some locales and the label must not drift.
  return `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

/** A bucket's span as the pulse tooltip says it: `18:00–18:15`, or with the day when long. */
export function spanLabel(fromIso: string, seconds: number): string {
  const from = new Date(fromIso);
  if (Number.isNaN(from.getTime())) return "";
  const to = new Date(from.getTime() + seconds * 1000);
  if (seconds >= 6 * 3600) return `${dayLabel(fromIso)} ${clock(fromIso, { seconds: false })}–${clock(to.toISOString(), { seconds: false })}`;
  return `${clock(fromIso, { seconds: seconds < 60 })}–${clock(to.toISOString(), { seconds: seconds < 60 })}`;
}

/** `2.4s`, `3m`, `1h 4m` — how long something took. "" when unknown. */
export function took(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
