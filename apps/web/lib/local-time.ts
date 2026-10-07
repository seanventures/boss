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
};

export const NOT_YET: LocalTime = { ready: false, hhmm: () => "", day: () => "", dayHour: () => "", weekday: () => "",
  monthDay: () => "", full: () => "" };

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
