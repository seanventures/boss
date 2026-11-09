"use client";

/**
 * WATCH THIS APP (docs/product/wireframes/agentic-factory/2-watch-an-app.html; Project › Health):
 * the read and the writes the sheet and the landing make. The engine is services/watch_app.py,
 * a thin layer over Observability's alert rules, contact points and routing. Every write goes
 * through obsWrite (apiWrite: one press, one effect; "Confirm it's you" on a stale second
 * factor; the consent sheet on 409 consent_required, then the same write once more).
 */
import { apiWrite } from "./api-write";
import { arr, num, obj, str } from "./factory-floor";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type WatchKind = "project" | "url" | "elsewhere";
export type Tone = "wake" | "tell";
export type Night = "wake" | "wait" | "alone";
export const KIND_WORDS: Record<WatchKind, string> = { project: "Project app", url: "Just a URL", elsewhere: "Alerts elsewhere" };
export const NIGHT_WORDS: Record<Night, string> = { wake: "Wake me", wait: "Wait till 08:00", alone: "Fix alone, tell me" };
export const TELL_KINDS: { key: string; label: string; needsUrl: boolean }[] = [
  { key: "", label: "Only you", needsUrl: false },
  { key: "slack_webhook", label: "Slack", needsUrl: true },
  { key: "mattermost", label: "Mattermost", needsUrl: true },
  { key: "webhook", label: "Webhook", needsUrl: true },
  { key: "email", label: "Email", needsUrl: false },
];

export type Watch = {
  kind: string; ruleId: string | null; icon: string; title: string; what: string; words: string | null; from: string;
  tone: Tone; params: Record<string, number>; forS: number; condition: string; expr: string; fired: number | null;
  backtest: string; spark: (number | null)[]; baseline: number | null; sub: string; runbook: string | null; state: string | null;
};
export type Boundary = { prod: boolean; alone: { key: string; label: string; icon: string }[];
  waits: { key: string; label: string; title: string; why: string }[]; word: string };
export type WatchRead = {
  kind: WatchKind; detected: WatchKind; apps: { targetId: string; label: string; env: string; address: string }[];
  target: { targetId: string; app: string; env: string; label: string; address: string; prod: boolean } | null;
  word: string;
  signals: { metrics: boolean; families: number; probes: number; logs: boolean; word: string; value: string; sub: string } | null;
  collector: { kind: string; value: string; sub: string } | null;
  watches: Watch[];
  question: { ask: string; options: { key: Night; label: string }[]; answer: Night | null } | null;
  onCall: { agent: string; alone: boolean; word: string } | null;
  tell: { you: string; channel: { kind: string; name: string; address?: string } | null; outside: boolean; verified: boolean; needsUrl: boolean } | null;
  boundary: Boundary | null; runbooks: string[]; cost: string; watching: boolean; matters: { kind: string; words: string }[];
};
export type Step = { key: string; label: string; state: "done" | "now" | "you" | "run" | "todo"; detail: string };
export type Landing = {
  targetId: string; label: string; app: string; env: string; night: Night; nightWord: string; alone: boolean;
  steps: Step[]; now: { text: string; tone: string }; gate: { contactPointId: string; name: string; kind: string; word: string } | null;
  boundary: Boundary; tell: { name: string; kind: string; verified: boolean } | null; watches: Watch[];
  incidents: { id: string; title: string; status: string; at: string; triage: string; response: string; runId: string }[]; words: string;
};

const nums = (v: unknown): (number | null)[] => arr(v).map((x) => (typeof x === "number" ? x : null));
const tone = (v: unknown): Tone => (v === "wake" ? "wake" : "tell");
const night = (v: unknown): Night => (v === "wait" || v === "alone" ? v : "wake");

export function parseWatch(v: unknown): Watch {
  const w = obj(v);
  const p = obj(w.params);
  return {
    kind: str(w.kind), ruleId: w.rule_id ? str(w.rule_id) : null, icon: str(w.icon), title: str(w.title), what: str(w.what),
    words: w.words ? str(w.words) : null, from: str(w.from), tone: tone(w.tone),
    params: Object.fromEntries(Object.entries(p).filter(([, x]) => typeof x === "number")) as Record<string, number>,
    forS: num(w.for_s, 0), condition: str(w.condition), expr: str(w.expr), fired: typeof w.fired === "number" ? w.fired : null,
    backtest: str(w.backtest), spark: nums(w.spark), baseline: typeof w.baseline === "number" ? w.baseline : null, sub: str(w.sub),
    runbook: w.runbook ? str(w.runbook) : null, state: w.state ? str(w.state) : null,
  };
}

function parseBoundary(v: unknown): Boundary {
  const b = obj(v);
  return { prod: !!b.prod, word: str(b.word),
    alone: arr(b.alone).map((x) => { const y = obj(x); return { key: str(y.key), label: str(y.label), icon: str(y.icon) }; }),
    waits: arr(b.waits).map((x) => { const y = obj(x); return { key: str(y.key), label: str(y.label), title: str(y.title), why: str(y.why) }; }) };
}

const asKind = (v: unknown): WatchKind => (v === "url" || v === "elsewhere" ? v : "project");

export function parseRead(v: unknown): WatchRead {
  const o = obj(v);
  const t = o.target ? obj(o.target) : null;
  const sg = o.signals ? obj(o.signals) : null;
  const c = o.collector ? obj(o.collector) : null;
  const q = o.question ? obj(o.question) : null;
  const oc = o.on_call ? obj(o.on_call) : null;
  const tl = o.tell ? obj(o.tell) : null;
  const ch = tl?.channel ? obj(tl.channel) : null;
  return {
    kind: asKind(o.kind), detected: asKind(o.detected), word: str(o.word),
    apps: arr(o.apps).map((x) => { const y = obj(x); return { targetId: str(y.target_id), label: str(y.label), env: str(y.env), address: str(y.address) }; }),
    target: t ? { targetId: str(t.target_id), app: str(t.app), env: str(t.env), label: str(t.label), address: str(t.address), prod: !!t.prod } : null,
    signals: sg ? { metrics: !!sg.metrics, families: num(sg.families, 0), probes: num(sg.probes, 0), logs: !!sg.logs, word: str(sg.word),
                    value: str(sg.value), sub: str(sg.sub) } : null,
    collector: c ? { kind: str(c.kind), value: str(c.value), sub: str(c.sub) } : null,
    watches: arr(o.watches).map(parseWatch),
    question: q ? { ask: str(q.ask), answer: q.answer ? night(q.answer) : null,
                    options: arr(q.options).map((x) => { const y = obj(x); return { key: night(y.key), label: str(y.label) }; }) } : null,
    onCall: oc ? { agent: str(oc.agent, "SRE"), alone: !!oc.alone, word: str(oc.word) } : null,
    tell: tl ? { you: str(tl.you), outside: !!tl.outside, verified: !!tl.verified, needsUrl: !!tl.needs_url,
                 channel: ch ? { kind: str(ch.kind), name: str(ch.name), address: ch.address ? str(ch.address) : undefined } : null } : null,
    boundary: o.boundary ? parseBoundary(o.boundary) : null,
    runbooks: arr(o.runbooks).map((x) => str(x)), cost: str(o.cost), watching: !!o.watching,
    matters: arr(obj(o.words).matters).map((x) => { const y = obj(x); return { kind: str(y.kind), words: str(y.words) }; }),
  };
}

export function parseLanding(v: unknown): Landing {
  const o = obj(v);
  const g = o.gate ? obj(o.gate) : null;
  const tl = o.tell ? obj(o.tell) : null;
  return {
    targetId: str(o.target_id), label: str(o.label), app: str(o.app), env: str(o.env), night: night(o.night), nightWord: str(o.night_word),
    alone: !!o.alone, words: str(o.words),
    steps: arr(o.steps).map((x) => { const y = obj(x); return { key: str(y.key), label: str(y.label), detail: str(y.detail),
      state: (["done", "now", "you", "run"].includes(str(y.state)) ? str(y.state) : "todo") as Step["state"] }; }),
    now: { text: str(obj(o.now).text), tone: str(obj(o.now).tone) },
    gate: g ? { contactPointId: str(g.contact_point_id), name: str(g.name), kind: str(g.kind), word: str(g.word) } : null,
    boundary: parseBoundary(o.boundary), tell: tl ? { name: str(tl.name), kind: str(tl.kind), verified: !!tl.verified } : null,
    watches: arr(o.watches).map(parseWatch),
    incidents: arr(o.incidents).map((x) => { const y = obj(x); return { id: str(y.id), title: str(y.title), status: str(y.status), at: str(y.at),
      triage: str(y.triage), response: str(y.response), runId: str(y.response_run_id) }; }),
  };
}

export type Result<T> = { ok: true; data: T } | { ok: false; error: string; status?: number };
const enc = encodeURIComponent;

/** The sheet's read: which app, how it talks, the watches with their backtest. Creates nothing. */
export async function readWatch(projectId: string, text: string, kind: WatchKind | null, targetId: string | null,
                                signal?: AbortSignal): Promise<Result<WatchRead>> {
  try {
    const r = await apiWrite(`/v1/projects/${enc(projectId)}/watch/read`, {
      method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" }, signal,
      body: JSON.stringify({ text, kind, target_id: targetId }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) {
      const b = obj(data);
      return { ok: false, status: r.status, error: str(b.message || b.detail, `The API answered ${r.status}.`) };
    }
    return { ok: true, data: parseRead(data) };
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") return { ok: false, error: "", status: 0 };
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

export type StartSpec = {
  project_id: string; actor: string; target_id: string; words: string; night: Night; alone: boolean; timezone: string;
  watches: { kind: string; params: Record<string, number>; for_s: number; tone: Tone; enabled: boolean; words: string | null }[];
  removed: string[]; tell: { kind?: string; name?: string; url?: string; address?: string };
};

const done = (r: WriteResult): Result<Landing> => (r.ok ? { ok: true, data: parseLanding(r.body) } : { ok: false, error: r.error ?? "Not done", status: r.status });

/** Start watching: one press, gated by observability:manage. */
export async function startWatching(projectId: string, spec: StartSpec): Promise<Result<Landing>> {
  return done(await obsWrite(`/v1/projects/${enc(projectId)}/watch`, spec as unknown as Record<string, unknown>,
    { scope: `watch-start-${projectId}`, label: "Start watching" }));
}

/** Enter the outside channel's code (Talking to the outside: the one gate). */
export async function verifyChannel(projectId: string, actor: string, targetId: string, code: string): Promise<Result<Landing>> {
  return done(await obsWrite(`/v1/projects/${enc(projectId)}/watch/${enc(targetId)}/verify`, { project_id: projectId, actor, code },
    { scope: `watch-verify-${targetId}`, label: "Verify" }));
}

export async function resendCode(projectId: string, actor: string, targetId: string): Promise<Result<Landing>> {
  return done(await obsWrite(`/v1/projects/${enc(projectId)}/watch/${enc(targetId)}/resend`, { project_id: projectId, actor },
    { scope: `watch-resend-${targetId}`, label: "Send the code again" }));
}

/** A later edit of one watch, with its why. */
export async function changeWatch(projectId: string, actor: string, targetId: string,
                                  edit: { rule_id: string; reason: string; params?: Record<string, number>; for_s?: number; tone?: Tone; remove?: boolean }): Promise<Result<Landing>> {
  return done(await obsWrite(`/v1/projects/${enc(projectId)}/watch/${enc(targetId)}/change`, { project_id: projectId, actor, ...edit },
    { scope: `watch-change-${targetId}`, label: "Done" }));
}

/** A watch's line as its numbers change in the popover (the engine's watch_app._title). */
export function titleOf(kind: string, p: Record<string, number>, fallback: string): string {
  const n = (v: number | undefined) => (v === undefined ? "?" : String(Math.round(v * 100) / 100));
  if (kind === "error_rate") return `Errors > ${n(p.threshold)}% for ${n(p.window_minutes)} min`;
  if (kind === "latency_p95") return `p95 > ${n(p.threshold)} ms for ${n(p.window_minutes)} min`;
  if (kind === "health_check") return `App down · ${n(p.min_failures)} failed probes in ${n(p.window_minutes)} min`;
  if (kind === "no_traffic") return `No traffic for ${n(p.window_minutes)} min`;
  return fallback;
}

/** The one thing a watch's numbers mean, for the popover's chips. */
export const PARAM_WORDS: Record<string, Record<string, { label: string; unit: string }>> = {
  error_rate: { threshold: { label: "errors above", unit: "%" }, window_minutes: { label: "over", unit: "min" }, min_requests: { label: "at least", unit: "req" } },
  latency_p95: { threshold: { label: "p95 above", unit: "ms" }, window_minutes: { label: "over", unit: "min" }, min_requests: { label: "at least", unit: "req" } },
  health_check: { min_failures: { label: "failed probes", unit: "" }, window_minutes: { label: "in", unit: "min" } },
  no_traffic: { window_minutes: { label: "no requests for", unit: "min" }, expected_hours: { label: "after traffic in", unit: "h" } },
  deploy_health: {},
};
