/**
 * Small pieces Agents › Live, its cards and its rows share: where a press goes, how old a thing
 * is, and a clock that ticks only on the client (so the server render and the first client
 * render agree, and an elapsed time moves without a poll).
 */
import { useEffect, useState, type MouseEvent as ReactMouseEvent } from "react";
import { localFull, markHydrated } from "../../lib/local-time";

/** The current URL with `patch` applied (null deletes). Keeps the org and everything else. */
export function hrefWith(patch: Record<string, string | null>): string {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    return `${url.pathname}?${url.searchParams.toString()}`;
  } catch {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(patch)) if (v !== null) p.set(k, v);
    return `/?${p.toString()}`;
  }
}

/** A link that renders the same on the server and the client: `/?org=…&area=…`. Presses are
 *  handled by `navigate`, which keeps the rest of the URL; this is for a new tab. */
export function linkTo(patch: Record<string, string | null>, orgId?: string | null): string {
  const out = new URLSearchParams();
  if (orgId) out.set("org", orgId);
  for (const [k, v] of Object.entries(patch)) if (v !== null) out.set(k, v);
  return `/?${out.toString()}`;
}

/** Go there inside the console: push the URL and say so, the way the rail does. */
export function navigate(patch: Record<string, string | null>): void {
  try {
    window.history.pushState(null, "", hrefWith(patch));
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

/** A plain left click on a link: handled in the console. Anything else: the browser's. */
export function plainClick(e: ReactMouseEvent): boolean {
  return !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0);
}

/** The run, in Workflows. */
export const runPatch = (runId: string) => ({ area: "delivery_workflows", wfr: runId, role: null, view: null });
/** The decision, in Approvals. */
export const decisionPatch = (id: string) => ({ area: "decisions", decision: id, decision_filter: "needs_action", role: null });

/** "8m", "3h", "2d" — how long ago. Empty before the clock starts. */
export function ago(iso: string | null | undefined, now: number | null): string {
  if (!iso || now === null) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** "42s", "12m", "1h 05m", "3d 4h". */
export function span(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

/** Local time, for a tooltip beside a relative age. */
export function localTime(iso: string | null | undefined): string {
  if (!iso) return "";
  // Hydration-safe (lib/local-time.ts localFull): UTC text until mounted, then the viewer's own.
  return Number.isFinite(Date.parse(iso)) ? localFull(iso) : iso;
}

/** A clock, null until mounted, ticking every `ms`. */
export function useNow(ms = 1000): number | null {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    markHydrated();
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** True when the user asked for less motion. */
export function reducedMotion(): boolean {
  try {
    return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  } catch {
    return false;
  }
}

/** The viewport's width band (spec §5.3). Re-read on resize. */
export type Band = "wide" | "mid" | "narrow" | "phone";
export function bandOf(width: number): Band {
  if (width >= 1280) return "wide";
  if (width >= 1024) return "mid";
  if (width >= 640) return "narrow";
  return "phone";
}
export function useBand(): Band {
  const [band, setBand] = useState<Band>("wide");
  useEffect(() => {
    const read = () => setBand(bandOf(window.innerWidth || 1280));
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);
  return band;
}

/** Face states that move (spec §5.2): only these count against the ring budget. */
export const ANIMATED_STATES = new Set(["working", "needs_you", "speaking", "thinking"]);
/** At most this many rings move on Live at once; past it they hold still (the word still says). */
export const RING_BUDGET = 8;
