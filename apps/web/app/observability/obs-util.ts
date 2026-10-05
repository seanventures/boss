/**
 * Small words and links the Observability tabs share: durations, the window's tick labels, the
 * way out to Infra (dropping Observability's own params), and the Ask BOSS starters built from
 * the Pulse on screen (the old Monitoring screen's `monitoringAsks`).
 */
import type { DockSubject } from "../../lib/dock-subject";
import type { Pulse } from "../../lib/obs-view";
import { navigate } from "../agents/live-util";
import type { LocalTime } from "../../lib/local-time";

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
export function timeTicks(fromIso: string | null, toIso: string | null, win: string, lt: LocalTime): string[] {
  const from = fromIso ? Date.parse(fromIso) : NaN;
  const to = toIso ? Date.parse(toIso) : NaN;
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return [];
  return [0, 1, 2, 3, 4].map((k) => {
    if (k === 4) return "now";
    const at = from + ((to - from) * k) / 4;
    return win === "24h" ? lt.hhmm(at) : lt.day(at);
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

/** One starter: the operator's question, and the app it is about when there is one. */
export type MonitoringAsk = { question: string; subject: DockSubject | null };
type AskTarget = { target_id: string; app_name: string; environment: string; status: string };

/** What is worth asking about, given the apps on screen (the old Monitoring screen's starters,
 *  moved here when it retired). A single sick app is asked about AS its deployment — its error,
 *  address and history travel as the record, not as a sentence (lib/dock-subject.ts). */
export function monitoringAsks(targets: AskTarget[]): MonitoringAsk[] {
  const sick = targets.filter((t) => t.status === "down" || t.status === "degraded");
  const unknown = targets.filter((t) => t.status === "unknown" || t.status === "not_deployed");
  const out: MonitoringAsk[] = [];
  const ask = (question: string, subject: DockSubject | null = null) => out.push({ question, subject });
  if (sick.length === 1) {
    const t = sick[0];
    ask(`Why is ${t.app_name} (${t.environment}) ${t.status}, and what is the fix?`,
        t.target_id ? { kind: "deployment", id: t.target_id } : null);
  } else if (sick.length > 1) {
    ask(`${sick.map((t) => t.app_name).join(", ")} are not healthy. What is actually wrong with each?`);
  } else if (targets.length) {
    ask("Everything here reads healthy. Check the probes are really running and that " +
        "each app answers on the port its target claims — not just that the row is green.");
  }
  if (unknown.length) {
    ask(`${unknown.map((t) => t.app_name).join(", ")} ${unknown.length === 1 ? "has" : "have"} no health signal. ` +
        "Is that because nothing is deployed, or because nothing is probing it?");
  }
  // The reason an operator comes to this screen with an idea rather than an incident.
  ask("I want to change what we monitor. What is being checked for each app today, " +
      "what would you add, and what can you set up yourself?");
  return out;
}

/** The starters, built from the Pulse on screen. */
export function obsAsks(pulse: Pulse | null): MonitoringAsk[] {
  return monitoringAsks((pulse?.rows ?? []).map((r) => ({
    target_id: r.targetId, app_name: r.app, environment: r.env, status: r.state,
  })));
}
