/**
 * THE VIEWER'S CLOCK, ONLY AFTER MOUNT. Infra and Observability show times in the viewer's own
 * zone and locale (`14:15`, `Oct 4`). The server renders in ITS zone, so a time painted during
 * the server render and again on the client disagrees, and React 19 keeps the server's text — the
 * datetime-hydration guard forbids it. `useLocalTime()` hands a component formatters that answer
 * "" until it has mounted in the browser, then the real local text; the first client render
 * matches the server's exactly.
 */
import { useEffect, useState } from "react";

import { formatDateTime } from "./datetime";

export type LocalTime = {
  /** True once mounted in the browser; before that every formatter answers "". */
  ready: boolean;
  /** `14:15` */
  hhmm: (ms: number) => string;
  /** `Oct 4` */
  day: (ms: number) => string;
  /** `Oct 4, 14` — a day and an hour, for multi-day axes. */
  dayHour: (ms: number) => string;
  /** `MONDAY` / `OCT 4` style day heads: the weekday name, or the month and day. */
  weekday: (ms: number) => string;
  monthDay: (ms: number) => string;
  /** The full local date and time, for a tooltip. Accepts ms, an ISO string or nothing. */
  full: (v: number | string | null | undefined) => string;
  /** The viewer's clock, read only after mount (0 before: "now" never paints on the server). */
  now: () => number;
};

const toMs = (v: number | string | null | undefined): number =>
  typeof v === "number" ? v : v ? Date.parse(v) : NaN;

export const LOCAL: LocalTime = {
  ready: true,
  hhmm: (ms) => (Number.isFinite(ms) ? new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : ""),
  day: (ms) => (Number.isFinite(ms) ? new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" }) : ""),
  dayHour: (ms) => (Number.isFinite(ms) ? new Date(ms).toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", hour12: false }) : ""),
  weekday: (ms) => (Number.isFinite(ms) ? new Date(ms).toLocaleDateString("en-US", { weekday: "long" }) : ""),
  monthDay: (ms) => (Number.isFinite(ms) ? new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : ""),
  full: (v) => { const ms = toMs(v); return Number.isFinite(ms) ? new Date(ms).toLocaleString() : ""; },
  now: () => Date.now(),
};

export const NOT_YET: LocalTime = { ready: false, hhmm: () => "", day: () => "", dayHour: () => "", weekday: () => "",
  monthDay: () => "", full: () => "", now: () => 0 };

export function useLocalTime(): LocalTime {
  const [ready, setReady] = useState(false);
  useEffect(() => { markHydrated(); setReady(true); }, []);
  return ready ? LOCAL : NOT_YET;
}

/**
 * THE FULL LOCAL TIME FOR A TOOLTIP, FROM CODE THAT IS NOT A HOOK (round 2 triage). A `title=` or
 * a helper called at render painted `new Date(x).toLocaleString()` on the server in ITS zone and
 * locale, and the browser disagreed on hydration. Until the page has hydrated (a mount effect
 * here, useLocalTime or useNow, has run) this answers the deterministic UTC text the server
 * painted; after it, the viewer's own local text. The server never marks itself hydrated.
 */
let hydrated = false;
export function markHydrated(): void {
  if (typeof window !== "undefined") hydrated = true;
}
/** Whether this page has hydrated (a mount effect has run): before it, render as the server did. */
export function isHydrated(): boolean {
  return hydrated && typeof window !== "undefined";
}
export function localFull(v: number | string | null | undefined): string {
  if (hydrated && typeof window !== "undefined") return LOCAL.full(v);
  const ms = typeof v === "number" ? v : v ? Date.parse(v) : NaN;
  return Number.isFinite(ms) ? formatDateTime(new Date(ms).toISOString(), "") : "";
}

/** A date or time in the viewer's zone and locale with `options`, hydration-safe like localFull:
 *  pinned to en-US in UTC until the page has hydrated, the viewer's own after. */
export function localWith(v: number | string | null | undefined, options: Intl.DateTimeFormatOptions): string {
  const ms = typeof v === "number" ? v : v ? Date.parse(v) : NaN;
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  return hydrated && typeof window !== "undefined"
    ? d.toLocaleString([], options)
    : d.toLocaleString("en-US", { ...options, timeZone: "UTC" });
}

// ── THE VIEWER'S ZONE, ONE FORMATTER (operator, 2026-11-06) ─────────────────────────────────
// "since 3:23 PM" for a wait that began at 7:23 local (PST) was the console printing UTC. Every
// time on the Project screens and every step's "waiting on you · since …" goes through these:
// Intl.DateTimeFormat in the browser's own zone once hydrated, UTC before (the server's paint),
// so hydration never disagrees. `zone` is for tests.

/** The browser's IANA zone after hydration; "UTC" on the server and before hydration. */
export function viewerZone(): string {
  if (!hydrated || typeof window === "undefined") return "UTC";
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
}

const dayKey = (ms: number, zone: string) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(ms));

/** "3:23 PM" when it is today in `zone`, else "Nov 5". */
export function sinceLocal(sinceMs: number, nowMs: number, zone: string = viewerZone()): string {
  if (!Number.isFinite(sinceMs)) return "";
  const d = new Date(sinceMs);
  if (dayKey(sinceMs, zone) !== dayKey(nowMs, zone)) {
    return new Intl.DateTimeFormat("en-US", { timeZone: zone, month: "short", day: "numeric" }).format(d);
  }
  return new Intl.DateTimeFormat("en-US", { timeZone: zone, hour: "numeric", minute: "2-digit", hour12: true }).format(d);
}

/** "Nov 6, 7:23 AM PST" — a full stamp with the zone's short name, for a tooltip. */
export function stampLocal(v: number | string | null | undefined, zone: string = viewerZone()): string {
  const ms = typeof v === "number" ? v : v ? Date.parse(v) : NaN;
  if (!Number.isFinite(ms)) return "";
  return new Intl.DateTimeFormat("en-US", { timeZone: zone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
                                            hour12: true, timeZoneName: "short" }).format(new Date(ms));
}

/** A time from an API field: an ISO stamp becomes "3:23 PM" / "Nov 5" in the viewer's zone;
 *  anything already in words ("2h ago", "Mon") is shown as it came. */
export function whenLocal(v: string | null | undefined, nowMs: number = Date.now(), zone: string = viewerZone()): string {
  if (!v) return "";
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v)) return v;
  return sinceLocal(Date.parse(v), nowMs, zone);
}
