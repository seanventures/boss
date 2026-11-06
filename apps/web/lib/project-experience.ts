/**
 * PROJECT EXPERIENCE (BOSSAPP-913/914): the typed client for docs/product/PROJECT_EXPERIENCE_CONTRACT.md.
 *
 * One read per tab (§1-§12), each parsed tolerantly (a missing field is an empty value, never a
 * crash), plus the writes the contract names. Reads go same-origin through the /v1 proxy; writes
 * go through obsWrite → apiWrite, so a stale second factor opens the step-up sheet and a consent
 * refusal opens the consent sheet (no write path of its own).
 *
 * THE ENGINE MAY NOT ANSWER YET. A route that answers 404/405/501 is `missing`, not an error: the
 * screens then build what they can from the reads that already exist (adapters below) and say so.
 * `?px_demo=1` renders the contract's sample project (lib/project-experience-fixtures.ts) with a
 * "Sample data" banner; writes in that mode send nothing.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { onChanged } from "../app/changed";
import { obsWrite } from "../app/observability/obs-write";
import { useAreaShown } from "./area-shown";
import { isConsoleLive, subscribeConsoleLive } from "./console-live";

// ── shared enums (§ Shared enums) ────────────────────────────────────────────────────────────

export type Stage = "onboarding" | "ready" | "backlog" | "working" | "staging" | "production";
export type StepState = "done" | "now" | "waiting" | "todo" | "failed" | "skipped";
export type WorkState = "planned" | "running" | "needs_you" | "done" | "failed";
export type Health = "healthy" | "degraded" | "down" | "unknown";

export const TABS = ["overview", "work", "roadmap", "knowledge", "deployments", "health", "settings"] as const;
export type Tab = (typeof TABS)[number];
export const TAB_WORDS: Record<Tab, string> = {
  overview: "Overview", work: "Work", roadmap: "Roadmap", knowledge: "Repo & knowledge",
  deployments: "Deployments", health: "Health", settings: "Settings",
};
export const toTab = (v: unknown): Tab => (TABS as readonly string[]).includes(String(v)) ? (v as Tab) : "overview";

// ── tolerant readers ─────────────────────────────────────────────────────────────────────────

type R = Record<string, unknown>;
export const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
export const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
export const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
export const n = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const nn = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const sn = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const nums = (v: unknown): number[] => a(v).filter((x): x is number => typeof x === "number" && Number.isFinite(x));
const oneOf = <T extends string>(v: unknown, all: readonly T[], d: T): T => ((all as readonly string[]).includes(String(v)) ? (v as T) : d);

const STAGES = ["onboarding", "ready", "backlog", "working", "staging", "production"] as const;
const STEP_STATES = ["done", "now", "waiting", "todo", "failed", "skipped"] as const;
const WORK_STATES = ["planned", "running", "needs_you", "done", "failed"] as const;
const HEALTHS = ["healthy", "degraded", "down", "unknown"] as const;

/** Diagram labels are ≤ 4 words (contract rule): a longer one is cut, never wrapped into prose. */
export function label4(v: unknown): string {
  const words = s(v).trim().split(/\s+/).filter(Boolean);
  return words.length <= 4 ? words.join(" ") : `${words.slice(0, 4).join(" ")}…`;
}

// ── types ────────────────────────────────────────────────────────────────────────────────────

export type Agent = { name: string; role_key: string; avatar: string; role?: string };
export type Node = { key: string; label: string; state: StepState; sub: string; href: string };
export type ProjectCard = {
  id: string; name: string; initials: string; repo: string; stage: Stage; journey: Node[];
  needs_you: number; running: number; health: Health; live_url: string; captain: Agent | null;
  updated_at: string; href: string; cost_week: number | null; series: number[];
};
export type ProjectsSummary = { items: ProjectCard[] };
export type Repo = { full_name: string; branch: string; access: { read: boolean; write: boolean; pr: boolean }; external_url: string };
export type NeedsYou = {
  id: string; kind: "merge" | "deploy" | "plan" | "question" | "setup"; label: string; detail: string;
  mini: { label: string; state: StepState }[]; action: { label: string; href: string; decision_id: string } | null;
};
export type WorkItem = {
  id: string; label: string; kind: string; state: WorkState; age: string;
  stages: { key: string; label: string; state: StepState }[]; agents: Agent[]; cost_usd: number | null;
  links: { workflow_run: string; build: string; pr: { number: number; external_url: string } | null; deploy: string; preview: string };
  detail: string; href: string; source: string;
};
export type NextItem = { id: string; label: string; source: string; state: "next" | "proposed"; href: string; accept: { href: string } | null };
export type DemoLogin = { user: string; label: string; reveal: { href: string; requires: string } | null };
export type Overview = {
  project: { id: string; name: string; initials: string; stage: Stage; repos: Repo[]; live_url: string; status_label: string; health: Health };
  journey: Node[]; needs_you: NeedsYou[];
  tiles: {
    open_work: { count: number; by_state: Record<string, number>; href: string } | null;
    running: { count: number; current: { label: string; progress: [number, number]; agent: Agent | null } | null; href: string } | null;
    staging: { health: Health; uptime_pct: number | null; p95_ms: number | null; series: number[]; href: string } | null;
    cost_week: { usd: number; delta_pct: number | null; cap_usd: number | null; series: number[]; href: string } | null;
  };
  captain: Captain | null; recent_work: WorkItem[]; next: NextItem[];
  try_it: { url: string; demo_logins: DemoLogin[] } | null;
  week: { at: string; label: string; kind: string }[];
};
export type Captain = { agent_id: string; name: string; role_key: string; avatar: string; status: string; squad: Agent[]; last_said: string };
export type WorkList = { items: WorkItem[] };
export type Knowledge = {
  repos: Repo[];
  dossier: { stack: string[]; commands: Record<string, string>; hotspots: { path: string; label: string; why: string; heat: number }[];
             tests: { passed: number; total: number } | null; built_at: string; href: string } | null;
  graph: { nodes: { id: string; label: string; kind: string }[]; edges: { from: string; to: string; label: string }[]; href: string; counts: { nodes: number; rules: number; learned: number } };
  docs: { label: string; kind: string; href: string; sub: string }[];
  decisions: { label: string; at: string; href: string }[];
  learned: { label: string; sub: string; state: string; href: string }[];
};
export type Environment = {
  key: string; label: string; url: string; health: Health; host: string; host_href: string; uptime_pct: number | null; p95_ms: number | null;
  error_rate: number | null; series: number[];
  last_deploy: { commit: string; at: string; by: string; label: string; href: string } | null;
  demo_logins: DemoLogin[]; policy: "auto" | "person"; href: string;
};
export type Deployments = {
  environments: Environment[];
  production: { state: "none" | "needs_host" | "ready"; action: { label: string; href: string } | null };
  ship_path: { label: string; state: StepState }[];
  history: { id: string; label: string; sub: string; state: StepState; href: string }[];
};
export type Recommendation = {
  id: string; title: string; kind: string; strip: { label: string; type: string }[]; value: number; effort: string; cost_usd: number | null;
  status: string; rank: number | null; href: string;
};
export type Roadmap = { now: NextItem[]; next: NextItem[]; later: NextItem[]; recommendations: Recommendation[]; counts: Record<string, number> };
export type OnboardingItem = { key: string; label: string; state: StepState; owner: "agent" | "you"; action: { label: string; href: string } | null; detail: string };
export type Onboarding = {
  state: "running" | "ready" | "needs_you" | "failed";
  stages: Node[]; discover: { nodes: Node[] }; checklist: OnboardingItem[]; only_you: OnboardingItem[];
  first_work: { label: string; source: string; estimate_min: number | null; estimate_usd: number | null; start: { href: string } | null; others_href: string } | null;
  backlog: NextItem[]; now: string; elapsed_s: number;
};
export type JumpItem = { label: string; kind: string; href: string; sub: string; external: boolean };
export type SettingsSection = {
  key: string; label: string; icon: string;
  items: { label: string; value: string; sub: string; reveal: { href: string; requires: string } | null; href: string }[];
  actions: { label: string; href: string; requires: string }[];
  moved_from: string;
};
export type Settings = { sections: SettingsSection[] };
export type HealthView = {
  environments: { key: string; label: string; health: Health; uptime_pct: number | null; error_rate: number | null; p95_ms: number | null;
                  series: { errors: number[]; latency: number[] }; slo: { target: number | null; burn: "ok" | "warning" | "breach" }; href: string }[];
  alerts: { id: string; label: string; severity: "page" | "ticket" | "info"; state: "firing" | "resolved"; since: string; href: string }[];
  incidents: { id: string; label: string; state: "running" | "needs_you" | "resolved"; graph_href: string; mini: { label: string; state: StepState }[] }[];
  monitoring_pack: { rules: number; managed_in: string; href: string } | null;
  links: { grafana: string; logs: string; observability: string };
};

// ── parsers ──────────────────────────────────────────────────────────────────────────────────

export const initialsOf = (name: string): string => {
  const w = name.trim().split(/[\s_-]+/).filter(Boolean);
  return ((w[0]?.[0] ?? "?") + (w[1]?.[0] ?? w[0]?.[1] ?? "")).toUpperCase();
};
export const parseAgent = (v: unknown): Agent | null => {
  const x = o(v);
  const name = s(x.name);
  if (!name && !s(x.role_key)) return null;
  return { name: name || s(x.role_key), role_key: s(x.role_key), avatar: s(x.avatar), role: s(x.role) || undefined };
};
const agents = (v: unknown): Agent[] => a(v).map(parseAgent).filter((x): x is Agent => x !== null);
export const parseNode = (v: unknown): Node => {
  const x = o(v);
  return { key: s(x.key), label: label4(x.label), state: oneOf(x.state, STEP_STATES, "todo"), sub: s(x.sub), href: s(x.href) };
};
const mini = (v: unknown) => a(v).map((m) => ({ label: label4(o(m).label), state: oneOf(o(m).state, STEP_STATES, "todo") }));
export const parseRepo = (v: unknown): Repo => {
  const x = o(v); const acc = o(x.access);
  return { full_name: s(x.full_name), branch: s(x.branch, "main"), external_url: s(x.external_url),
           access: { read: acc.read === true, write: acc.write === true, pr: acc.pr === true } };
};
export const parseCard = (v: unknown): ProjectCard => {
  const x = o(v);
  const name = s(x.name, s(x.id));
  return {
    id: s(x.id), name, initials: s(x.initials) || initialsOf(name), repo: s(x.repo), stage: oneOf(x.stage, STAGES, "onboarding"),
    journey: a(x.journey).map(parseNode), needs_you: n(x.needs_you), running: n(x.running), health: oneOf(x.health, HEALTHS, "unknown"),
    live_url: s(x.live_url), captain: parseAgent(x.captain), updated_at: s(x.updated_at), href: s(x.href),
    cost_week: nn(x.cost_week), series: nums(x.series),
  };
};
export const parseSummary = (v: unknown): ProjectsSummary => ({ items: a(o(v).items).map(parseCard).filter((c) => c.id) });

export const parseWorkItem = (v: unknown): WorkItem => {
  const x = o(v); const l = o(x.links); const pr = o(l.pr);
  return {
    id: s(x.id), label: s(x.label), kind: s(x.kind, "feature"), state: oneOf(x.state, WORK_STATES, "planned"), age: s(x.age),
    stages: a(x.stages).map((st) => ({ key: s(o(st).key), label: s(o(st).label), state: oneOf(o(st).state, STEP_STATES, "todo") })),
    agents: agents(x.agents), cost_usd: nn(x.cost_usd),
    links: { workflow_run: s(l.workflow_run), build: s(l.build), deploy: s(l.deploy), preview: s(l.preview),
             pr: pr.number || pr.external_url ? { number: n(pr.number), external_url: s(pr.external_url) } : null },
    detail: s(x.detail), href: s(x.href), source: s(x.source),
  };
};
const parseNext = (v: unknown): NextItem => {
  const x = o(v);
  return { id: s(x.id), label: s(x.label), source: s(x.source), state: x.state === "proposed" ? "proposed" : "next", href: s(x.href),
           accept: o(x.accept).href ? { href: s(o(x.accept).href) } : null };
};
const parseLogin = (v: unknown): DemoLogin => {
  const x = o(v); const r = o(x.reveal);
  return { user: s(x.user), label: s(x.label), reveal: r.href ? { href: s(r.href), requires: s(r.requires, "step_up") } : null };
};
export const parseCaptain = (v: unknown): Captain | null => {
  const x = o(v);
  if (!s(x.name) && !s(x.role_key)) return null;
  return { agent_id: s(x.agent_id), name: s(x.name) || s(x.role_key), role_key: s(x.role_key), avatar: s(x.avatar), status: s(x.status),
           squad: agents(x.squad), last_said: s(x.last_said) };
};

export const parseOverview = (v: unknown): Overview => {
  const x = o(v); const p = o(x.project); const t = o(x.tiles);
  const ow = o(t.open_work); const rn = o(t.running); const st = o(t.staging); const cw = o(t.cost_week);
  const cur = o(rn.current);
  const name = s(p.name, s(p.id));
  const byState: Record<string, number> = {};
  for (const [k, val] of Object.entries(o(ow.by_state))) byState[k] = n(val);
  const prog = nums(cur.progress);
  const ti = o(x.try_it);
  return {
    project: { id: s(p.id), name, initials: s(p.initials) || initialsOf(name), stage: oneOf(p.stage, STAGES, "onboarding"),
               repos: a(p.repos).map(parseRepo), live_url: s(p.live_url), status_label: s(p.status_label),
               health: oneOf(p.health ?? st.health, HEALTHS, "unknown") },
    journey: a(x.journey).map(parseNode),
    needs_you: a(x.needs_you).map((it) => {
      const y = o(it); const act = o(y.action);
      return { id: s(y.id), kind: oneOf(y.kind, ["merge", "deploy", "plan", "question", "setup"] as const, "question"), label: label4(y.label),
               detail: s(y.detail), mini: mini(y.mini),
               action: act.label || act.href ? { label: s(act.label, "Open"), href: s(act.href), decision_id: s(act.decision_id) } : null };
    }),
    tiles: {
      open_work: Object.keys(ow).length ? { count: n(ow.count), by_state: byState, href: s(ow.href) } : null,
      running: Object.keys(rn).length ? { count: n(rn.count), href: s(rn.href),
        current: Object.keys(cur).length ? { label: s(cur.label), progress: [prog[0] ?? 0, prog[1] ?? 0], agent: parseAgent(cur.agent) } : null } : null,
      staging: Object.keys(st).length ? { health: oneOf(st.health, HEALTHS, "unknown"), uptime_pct: nn(st.uptime_pct), p95_ms: nn(st.p95_ms),
                                          series: nums(st.series), href: s(st.href) } : null,
      cost_week: Object.keys(cw).length ? { usd: n(cw.usd), delta_pct: nn(cw.delta_pct), cap_usd: nn(cw.cap_usd), series: nums(cw.series), href: s(cw.href) } : null,
    },
    captain: parseCaptain(x.captain),
    recent_work: a(x.recent_work).map(parseWorkItem).slice(0, 5),
    next: a(x.next).map(parseNext),
    try_it: Object.keys(ti).length ? { url: s(ti.url), demo_logins: a(ti.demo_logins).map(parseLogin) } : null,
    week: a(x.week).map((w) => ({ at: s(o(w).at), label: label4(o(w).label), kind: s(o(w).kind) })),
  };
};
export const parseWork = (v: unknown): WorkList => ({ items: a(o(v).items).map(parseWorkItem).filter((w) => w.id) });

export const parseKnowledge = (v: unknown): Knowledge => {
  const x = o(v); const d = o(x.dossier); const g = o(x.graph); const c = o(g.counts);
  const cmds: Record<string, string> = {};
  for (const [k, val] of Object.entries(o(d.commands))) if (typeof val === "string" && val) cmds[k] = val;
  const tests = o(d.tests);
  return {
    repos: a(x.repos).map(parseRepo),
    dossier: Object.keys(d).length ? {
      stack: a(d.stack).map((z) => s(z)).filter(Boolean), commands: cmds,
      hotspots: a(d.hotspots).map((h) => ({ path: s(o(h).path), label: s(o(h).label), why: s(o(h).why), heat: n(o(h).heat, 1) })),
      tests: Object.keys(tests).length ? { passed: n(tests.passed), total: n(tests.total) } : null,
      built_at: s(d.built_at), href: s(d.href) } : null,
    graph: { nodes: a(g.nodes).map((z) => ({ id: s(o(z).id), label: label4(o(z).label), kind: s(o(z).kind) })),
             edges: a(g.edges).map((z) => ({ from: s(o(z).from), to: s(o(z).to), label: s(o(z).label) })), href: s(g.href),
             counts: { nodes: n(c.nodes, a(g.nodes).length), rules: n(c.rules), learned: n(c.learned) } },
    docs: a(x.docs).map((z) => ({ label: s(o(z).label), kind: s(o(z).kind), href: s(o(z).href), sub: s(o(z).sub) })),
    decisions: a(x.decisions).map((z) => ({ label: s(o(z).label), at: s(o(z).at), href: s(o(z).href) })),
    learned: a(x.learned).map((z) => ({ label: s(o(z).label), sub: s(o(z).sub), state: s(o(z).state), href: s(o(z).href) })),
  };
};

export const parseDeployments = (v: unknown): Deployments => {
  const x = o(v); const pr = o(x.production); const act = o(pr.action);
  return {
    environments: a(x.environments).map((e) => {
      const y = o(e); const ld = o(y.last_deploy);
      return { key: s(y.key), label: s(y.label, s(y.key)), url: s(y.url), health: oneOf(y.health, HEALTHS, "unknown"), host: s(y.host),
               host_href: s(y.host_href), uptime_pct: nn(y.uptime_pct), p95_ms: nn(y.p95_ms), error_rate: nn(y.error_rate), series: nums(y.series),
               last_deploy: Object.keys(ld).length ? { commit: s(ld.commit), at: s(ld.at), by: s(ld.by), label: s(ld.label), href: s(ld.href) } : null,
               demo_logins: a(y.demo_logins).map(parseLogin), policy: y.policy === "person" ? "person" : "auto", href: s(y.href) };
    }),
    production: { state: oneOf(pr.state, ["none", "needs_host", "ready"] as const, "none"),
                  action: act.label || act.href ? { label: s(act.label, "Set up"), href: s(act.href) } : null },
    ship_path: mini(x.ship_path),
    history: a(x.history).map((h) => ({ id: s(o(h).id), label: s(o(h).label), sub: s(o(h).sub), state: oneOf(o(h).state, STEP_STATES, "done"), href: s(o(h).href) })),
  };
};

export const parseOnboarding = (v: unknown): Onboarding => {
  const x = o(v); const fw = o(x.first_work);
  const item = (z: unknown): OnboardingItem => {
    const y = o(z); const act = o(y.action);
    return { key: s(y.key), label: label4(y.label), state: oneOf(y.state, STEP_STATES, "todo"), owner: y.owner === "you" ? "you" : "agent",
             action: act.label || act.href ? { label: s(act.label, "Do it"), href: s(act.href) } : null, detail: s(y.detail) };
  };
  const checklist = a(x.checklist).map(item);
  const onlyYou = a(x.only_you).length ? a(x.only_you).map(item) : checklist.filter((c) => c.owner === "you" && c.state !== "done");
  return {
    state: oneOf(x.state, ["running", "ready", "needs_you", "failed"] as const, "running"),
    stages: a(x.stages).map(parseNode), discover: { nodes: a(o(x.discover).nodes).map(parseNode) }, checklist, only_you: onlyYou,
    first_work: Object.keys(fw).length ? { label: s(fw.label), source: s(fw.source), estimate_min: nn(fw.estimate_min), estimate_usd: nn(fw.estimate_usd),
                                           start: o(fw.start).href ? { href: s(o(fw.start).href) } : null, others_href: s(fw.others_href) } : null,
    backlog: a(x.backlog).map(parseNext), now: s(x.now), elapsed_s: n(x.elapsed_s),
  };
};

export const parseJump = (v: unknown): JumpItem[] => a(o(v).items).map((z) => {
  const y = o(z); const href = s(y.href);
  return { label: s(y.label), kind: s(y.kind), href, sub: s(y.sub), external: /^https?:\/\//.test(href) };
}).filter((j) => j.label && j.href);

export const parseSettings = (v: unknown): Settings => ({
  sections: a(o(v).sections).map((z) => {
    const y = o(z);
    return {
      key: s(y.key), label: s(y.label), icon: s(y.icon), moved_from: s(y.moved_from),
      items: a(y.items).map((it) => {
        const q = o(it); const r = o(q.reveal);
        return { label: s(q.label ?? q.user ?? q.name), value: s(q.value ?? q.user), sub: s(q.sub ?? q.env), href: s(q.href),
                 reveal: r.href ? { href: s(r.href), requires: s(r.requires, "step_up") } : null };
      }),
      actions: a(y.actions).map((q) => ({ label: s(o(q).label), href: s(o(q).href), requires: s(o(q).requires) })).filter((q) => q.label),
    };
  }).filter((sec) => sec.key),
});

export const parseHealth = (v: unknown): HealthView => {
  const x = o(v); const mp = o(x.monitoring_pack); const l = o(x.links);
  return {
    environments: a(x.environments).map((e) => {
      const y = o(e); const ser = o(y.series); const slo = o(y.slo);
      return { key: s(y.key), label: s(y.label, s(y.key)), health: oneOf(y.health, HEALTHS, "unknown"), uptime_pct: nn(y.uptime_pct),
               error_rate: nn(y.error_rate), p95_ms: nn(y.p95_ms), series: { errors: nums(ser.errors), latency: nums(ser.latency) },
               slo: { target: nn(slo.target), burn: oneOf(slo.burn, ["ok", "warning", "breach"] as const, "ok") }, href: s(y.href) };
    }),
    alerts: a(x.alerts).map((z) => {
      const y = o(z);
      return { id: s(y.id), label: label4(y.label), severity: oneOf(y.severity, ["page", "ticket", "info"] as const, "info"),
               state: y.state === "resolved" ? "resolved" : "firing", since: s(y.since), href: s(y.href) };
    }),
    incidents: a(x.incidents).map((z) => {
      const y = o(z);
      return { id: s(y.id), label: label4(y.label), state: oneOf(y.state, ["running", "needs_you", "resolved"] as const, "running"),
               graph_href: s(y.graph_href), mini: mini(y.mini) };
    }),
    monitoring_pack: Object.keys(mp).length ? { rules: n(mp.rules), managed_in: s(mp.managed_in), href: s(mp.href) } : null,
    links: { grafana: s(l.grafana), logs: s(l.logs), observability: s(l.observability) },
  };
};

// ── URLs ─────────────────────────────────────────────────────────────────────────────────────

const enc = encodeURIComponent;
export const PX_PATHS = {
  summary: () => "/v1/projects/summary",
  overview: (id: string) => `/v1/projects/${enc(id)}/overview`,
  work: (id: string, state = "", q = "") => `/v1/projects/${enc(id)}/work${state || q ? `?${new URLSearchParams({ ...(state ? { state } : {}), ...(q ? { q } : {}) })}` : ""}`,
  knowledge: (id: string) => `/v1/projects/${enc(id)}/knowledge`,
  deployments: (id: string) => `/v1/projects/${enc(id)}/deployments`,
  captain: (id: string) => `/v1/projects/${enc(id)}/captain`,
  onboarding: (id: string) => `/v1/projects/${enc(id)}/onboarding`,
  onboardingStart: (id: string) => `/v1/projects/${enc(id)}/onboarding/start`,
  settings: (id: string) => `/v1/projects/${enc(id)}/settings`,
  health: (id: string) => `/v1/projects/${enc(id)}/health`,
  jump: (q: string, projectId?: string) => `/v1/jump?${new URLSearchParams({ q, ...(projectId ? { project_id: projectId } : {}) })}`,
  /** Existing (§6): the roadmap reads ambient's stored recommendations; works with the preview off. */
  roadmap: (id: string) => `/v1/ambient/roadmap?${new URLSearchParams({ project_id: id, status: "open", limit: "30" })}`,
  /** Existing: the project page's start view (repo, plan, work rows) — the fallback for Overview/Work. */
  start: (id: string) => `/v1/projects/${enc(id)}/start`,
} as const;

/** Console URL state for the project page (§10). */
export type PxUrl = { tab: Tab; drawer: { kind: string; id: string } | null; firstRun: boolean };
export function readPxUrl(search = typeof window === "undefined" ? "" : window.location.search): PxUrl {
  const p = new URLSearchParams(search);
  const raw = p.get("drawer") ?? "";
  const i = raw.indexOf(":");
  return { tab: toTab(p.get("tab")), firstRun: p.get("first_run") === "1",
           drawer: raw ? { kind: i < 0 ? raw : raw.slice(0, i), id: i < 0 ? "" : raw.slice(i + 1) } : null };
}

/** Patch the URL (push by default, so Back closes a drawer) and tell the console it moved. */
export function writePxUrl(patch: Record<string, string | null>, mode: "push" | "replace" = "push"): void {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) { if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v); }
    if (url.toString() === window.location.href) return;
    if (mode === "push") window.history.pushState(null, "", url.toString());
    else window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

/** A contract `href` ("?project_id=…&area=…") as a console link that keeps the org and drops the
 *  rest of this page's state (a drawer, a filter); an absolute URL is returned as it is. */
export function pxHref(href: string, search = typeof window === "undefined" ? "" : window.location.search): string {
  if (!href) return "";
  if (/^(https?:)?\/\//.test(href) || href.startsWith("/v1/")) return href;
  const q = href.slice(href.indexOf("?") + 1);
  if (!href.includes("?") && !href.includes("=")) return href;
  const cur = new URLSearchParams(search);
  const next = new URLSearchParams();
  for (const keep of ["org", "project_id", "px_demo"]) { const v = cur.get(keep); if (v) next.set(keep, v); }
  new URLSearchParams(q).forEach((v, k) => next.set(k, v));
  return `/?${next.toString()}`;
}
export const isExternal = (href: string): boolean => /^https?:\/\//.test(href);

/** The project page's own link (§10). */
export function projectHref(projectId: string, extra: Record<string, string> = {}, search?: string): string {
  return pxHref(`?${new URLSearchParams({ project_id: projectId, area: "project", ...extra })}`, search);
}

// ── sample mode ──────────────────────────────────────────────────────────────────────────────

export function isPxDemo(search = typeof window === "undefined" ? "" : window.location.search): boolean {
  return new URLSearchParams(search).get("px_demo") === "1";
}

// ── the read hook ────────────────────────────────────────────────────────────────────────────

export type ReadState = "loading" | "ready" | "missing" | "error";
export type PxRead<T> = { data: T | null; state: ReadState; error: string | null; demo: boolean; refresh: () => void };

/** A status that means "this engine has no such route yet", not a failure to show. */
export const isMissingStatus = (status: number): boolean => status === 404 || status === 405 || status === 501;

/**
 * GET `url` now and every `everyMs` while the area is on screen, the tab is visible and the
 * console is Live; again on any console change (app/changed.ts). `demo` is what sample mode shows.
 */
export function usePxRead<T>(url: string | null, parse: (raw: unknown) => T, opts: { everyMs?: number; demo?: () => T | null } = {}): PxRead<T> {
  const demoMode = isPxDemo();
  const [data, setData] = useState<T | null>(() => (demoMode && opts.demo ? opts.demo() : null));
  const [state, setState] = useState<ReadState>(demoMode && opts.demo ? "ready" : url ? "loading" : "missing");
  const [error, setError] = useState<string | null>(null);
  const parser = useRef(parse);
  parser.current = parse;
  const demo = useRef(opts.demo);
  demo.current = opts.demo;
  const shown = useAreaShown();
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const kick = useRef<() => void>(() => {});
  const every = opts.everyMs ?? 20_000;

  useEffect(() => {
    if (demoMode && demo.current) {
      setData(demo.current()); setState("ready"); setError(null);
      return undefined;
    }
    if (!url) { setData(null); setState("missing"); return undefined; }
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let gen = 0;
    setState((cur) => (cur === "ready" ? cur : "loading"));
    const tick = async (mine: number) => {
      if (stopped || mine !== gen) return;
      const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
      let wait = every;
      if (!hidden && isConsoleLive() && shownRef.current) {
        try {
          const r = await fetch(url, { cache: "no-store" });
          if (stopped || mine !== gen) return;
          if (r.ok) {
            const parsed = parser.current(await r.json());
            setData(parsed); setState("ready"); setError(null);
          } else if (isMissingStatus(r.status)) {
            setState("missing"); setError(null); wait = 5 * 60_000;
          } else {
            let said = "";
            try { const b = o(await r.json()); said = s(b.detail) || s(b.error); } catch { /* none */ }
            setError(said || `BOSS answered ${r.status}.`);
            setState((cur) => (cur === "ready" ? cur : "error"));
          }
        } catch (e) {
          if (stopped) return;
          setError(e instanceof Error ? e.message : "BOSS did not answer.");
          setState((cur) => (cur === "ready" ? cur : "error"));
        }
      }
      if (!stopped && mine === gen) timer = setTimeout(() => void tick(mine), wait);
    };
    const restart = () => {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      gen += 1;
      void tick(gen);
    };
    kick.current = restart;
    restart();
    const offChanged = onChanged(() => restart());
    const offLive = subscribeConsoleLive((on) => { if (on) restart(); });
    const onVis = () => { if (document.visibilityState === "visible") restart(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      offChanged(); offLive();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [url, every, demoMode]);

  useEffect(() => { if (shown) kick.current(); }, [shown]);
  const refresh = useCallback(() => kick.current(), []);
  return { data, state, error, demo: demoMode && Boolean(opts.demo), refresh };
}

// ── writes ───────────────────────────────────────────────────────────────────────────────────

export type PxWrite = { ok: boolean; status: number; body: R | null; error: string | null; demo?: boolean };

/** One write. Sample mode sends nothing and says so. */
export async function pxWrite(path: string, body: R, label: string): Promise<PxWrite> {
  if (isPxDemo()) return { ok: true, status: 200, body: {}, error: null, demo: true };
  const r = await obsWrite<R>(path, body, { scope: `px:${path}`, label });
  return { ok: r.ok, status: r.status, body: r.body, error: r.error };
}
export const startWork = (projectId: string, kind: "feature" | "bug" | "ticket", text: string) =>
  pxWrite(`/v1/projects/${enc(projectId)}/work`, { kind, text }, "Start");
export const setCaptain = (projectId: string, agentId: string) =>
  pxWrite(PX_PATHS.captain(projectId), { agent_id: agentId }, "Change captain");
export const startOnboarding = (projectId: string) =>
  pxWrite(PX_PATHS.onboardingStart(projectId), {}, "Start onboarding");

// ── adapters: what the screens can show from reads that exist today ─────────────────────────

/** GET /v1/projects/{id}/start (lib/start-project StartView), as the contract's Overview. */
export function overviewFromStart(raw: unknown, projectId: string): Overview {
  const v = o(raw); const repo = o(v.repo);
  const name = s(v.name, projectId);
  const work = a(v.work).map((w) => workFromStartRow(w, projectId));
  const needs = work.filter((w) => w.state === "needs_you");
  const running = work.filter((w) => w.state === "running");
  return {
    project: { id: projectId, name, initials: initialsOf(name), stage: work.length ? "working" : repo.full_name ? "ready" : "onboarding",
               repos: repo.full_name ? [{ full_name: s(repo.full_name), branch: s(repo.branch, "main"), access: { read: true, write: true, pr: true },
                                          external_url: `https://github.com/${s(repo.full_name)}` }] : [],
               live_url: "", status_label: work.length ? "Working" : "Ready", health: "unknown" },
    journey: [], needs_you: needs.map((w) => ({ id: w.id, kind: "question", label: label4(w.label), detail: w.detail, mini: [],
                                                action: { label: "Open", href: w.href, decision_id: "" } })),
    tiles: {
      open_work: { count: work.filter((w) => w.state !== "done").length,
                   by_state: { running: running.length, needs_you: needs.length, planned: work.filter((w) => w.state === "planned").length,
                               done: work.filter((w) => w.state === "done").length }, href: "" },
      running: { count: running.length, current: running[0] ? { label: running[0].label, progress: [0, 0], agent: null } : null, href: "" },
      staging: null, cost_week: null,
    },
    captain: null, recent_work: work.slice(0, 5),
    next: a(v.next).map((x) => ({ id: s(o(x).key), label: s(o(x).label), source: s(o(x).who), state: "next" as const, href: "", accept: null })),
    try_it: null, week: [],
  };
}
const STATE_FROM_TONE: Record<string, WorkState> = { amber: "needs_you", blue: "running", green: "done", red: "failed", grey: "planned" };
export function workFromStartRow(raw: unknown, projectId: string): WorkItem {
  const r = o(raw); const act = o(r.action);
  const run = s(r.run_id);
  const pr = s(r.pull_request_url);
  const prNo = Number((pr.match(/\/pull\/(\d+)/) ?? [])[1] ?? 0);
  return {
    id: run, label: s(r.title, s(r.workflow)), kind: "feature", state: STATE_FROM_TONE[s(r.tone)] ?? "planned", age: "",
    stages: [], agents: [], cost_usd: null,
    links: { workflow_run: `?${new URLSearchParams({ project_id: projectId, area: "delivery_workflows", wfr: run })}`, build: "", deploy: "",
             preview: "", pr: pr ? { number: prNo, external_url: pr } : null },
    detail: s(r.status), href: "", source: s(r.workflow),
  };
}
/** The open-work bar's segments, in the order and colours of the mock (running · needs you · planned · done). */
export const WORK_ORDER: WorkState[] = ["needs_you", "running", "planned", "done", "failed"];
export const WORK_WORDS: Record<WorkState, string> = { needs_you: "Needs you", running: "Running", planned: "Planned", done: "Done", failed: "Failed" };

// ── the roadmap's writes (§6: the existing ambient routes), sample-mode safe ─────────────────

import { acceptRec as aaAccept, rejectRec as aaReject, snoozeRec as aaSnooze, type AcceptAs, type RejectCategory } from "./ambient";
const demoOk = (): PxWrite => ({ ok: true, status: 200, body: {}, error: null, demo: true });
const fromAa = (r: { ok: boolean; status: number; body: unknown; error: string | null }): PxWrite =>
  ({ ok: r.ok, status: r.status, body: o(r.body), error: r.error });
export const acceptRecommendation = async (projectId: string, id: string, as: AcceptAs = "ticket") =>
  (isPxDemo() ? demoOk() : fromAa(await aaAccept(projectId, id, as)));
export const rejectRecommendation = async (projectId: string, id: string, reason: string, category: RejectCategory) =>
  (isPxDemo() ? demoOk() : fromAa(await aaReject(projectId, id, reason, category)));
export const snoozeRecommendation = async (projectId: string, id: string, days = 14) =>
  (isPxDemo() ? demoOk() : fromAa(await aaSnooze(projectId, id, days)));

/** Follow a console link the way a click on it would: the shell reveals an in-console area
 *  without a page load (its link handler), anything else navigates. */
export function navigateTo(href: string): void {
  if (typeof document === "undefined" || !href) return;
  const link = document.createElement("a");
  link.href = href;
  link.style.display = "none";
  document.body.appendChild(link);
  try { link.click(); } finally { link.remove(); }
}
