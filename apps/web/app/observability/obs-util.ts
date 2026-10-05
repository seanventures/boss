/**
 * Small words and links the Observability tabs share: durations, the window's tick labels, the
 * way out to Infra (dropping Observability's own params), and the Ask BOSS starters built from
 * the Pulse on screen (the old Monitoring screen's `monitoringAsks`).
 */
import { monitoringAsks } from "../monitoring-ui";
import type { Pulse } from "../../lib/obs-view";
import { navigate } from "../agents/live-util";

/** `1h 4m`, `38m`, `2d 3h`. */
export function dur(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}
export const SPAN: Record<string, string> = { "24h": "24 hours", "7d": "7 days", "30d": "30 days" };
export const HEAD: Record<string, string> = { "24h": "PULSE · 24H", "7d": "PULSE · 7D", "30d": "PULSE · 30D" };

/** Five evenly spaced labels across the window: times for 24h, days for 7d/30d, `now` last. */
export function timeTicks(fromIso: string | null, toIso: string | null, win: string): string[] {
  const from = fromIso ? Date.parse(fromIso) : NaN;
  const to = toIso ? Date.parse(toIso) : NaN;
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return [];
  return [0, 1, 2, 3, 4].map((k) => {
    if (k === 4) return "now";
    const at = new Date(from + ((to - from) * k) / 4);
    return win === "24h"
      ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
      : at.toLocaleDateString([], { month: "short", day: "numeric" });
  });
}

/** Leave for another area (Infra, Activity, a build): Observability's own params are dropped on the way. */
const LEAVE = { app: null, model: null, group: null, metric: null, win: null, q: null, watch: null };
export function leaveTo(href: string) {
  const p = new URLSearchParams(href.replace(/^\?/, ""));
  const patch: Record<string, string | null> = { ...LEAVE, view: null };
  p.forEach((v, k) => { patch[k] = v; });
  navigate(patch);
}

/** The starters the old Monitoring screen offered (`monitoringAsks`), built from the Pulse on
 *  screen: the sick app is asked about AS its deployment. */
export function obsAsks(pulse: Pulse | null) {
  const targets = (pulse?.rows ?? []).map((r) => ({
    target_id: r.targetId, app_name: r.app, environment: r.env, status: r.state,
  }));
  return monitoringAsks({ project_id: "", targets, summary: { up: 0, degraded: 0, down: 0, unknown: 0, overall: "", total: targets.length } } as never);
}

