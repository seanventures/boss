/**
 * PROJECT EXPERIENCE (BOSSAPP-913/914): the typed client for docs/product/PROJECT_EXPERIENCE_CONTRACT.md.
 *
 * One read per tab (§1-§12), each parsed tolerantly (a missing field is an empty value, never a
 * crash), plus the writes the contract names. Reads go same-origin through the /v1 proxy; writes
 * go through obsWrite → apiWrite, so a stale second factor opens the step-up sheet and a consent
 * refusal opens the consent sheet (no write path of its own).
 *
 * Every read here is served by the engine (apps/api/app/routers/project_experience.py). A route
 * that answers 404/405/501 is `missing` — the project is gone or out of reach, or an engine older
 * than this console — never an error to print: the screens build what they can from the reads
 * that exist (adapters below) and say so in plain words.
 * `?px_demo=1` renders the contract's sample project (lib/project-experience-fixtures.ts) with a
 * "Sample data" banner; writes in that mode send nothing.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { onChanged } from "../app/changed";
import { obsWrite } from "../app/observability/obs-write";
import { useAreaShown } from "./area-shown";
import { isConsoleLive, subscribeConsoleLive } from "./console-live";
import { isHydrated, markHydrated } from "./local-time";

// ── shared enums (§ Shared enums) ────────────────────────────────────────────────────────────

export type Stage = "onboarding" | "ready" | "backlog" | "working" | "staging" | "production";
export type StepState = "done" | "now" | "waiting" | "todo" | "failed" | "skipped";
export type WorkState = "planned" | "running" | "stalled" | "needs_you" | "done" | "failed";
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
const WORK_STATES = ["planned", "running", "stalled", "needs_you", "done", "failed"] as const;
const ATTEMPT_STATES = ["planned", "running", "stalled", "handed_on", "needs_you", "done", "failed"] as const;
const HEALTHS = ["healthy", "degraded", "down", "unknown"] as const;

/** Diagram labels are ≤ 4 words (contract rule): a longer one is cut, never wrapped into prose. */
export function label4(v: unknown): string {
  const words = s(v).trim().split(/\s+/).filter(Boolean);
  return words.length <= 4 ? words.join(" ") : `${words.slice(0, 4).join(" ")}…`;
}

// ── types ────────────────────────────────────────────────────────────────────────────────────

export type Agent = { name: string; role_key: string; avatar: string; role?: string; agent_id?: string };
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
  links: { workflow_run: string; build: string; pr: { number: number; external_url: string } | null; deploy: string; preview: string; decision: string };
  detail: string; href: string; source: string;
  /** "Build" | "Workflow" | "Roadmap": the human word for `source` (never the raw string). */
  source_label: string;
  /** When it stopped moving (ISO), for a `stalled` item. */
  stalled_since: string;
  /** Every attempt of this piece of work, newest first (empty when it had only one). */
  attempts: Attempt[]; attempt_count: number; followups: number; retries: number;
};
/** One attempt of a piece of work: the build, a retry, or a follow-up (§3 `attempts[]`). */
export type Attempt = {
  id: string; label: string; kind: "build" | "retry" | "followup"; title: string;
  state: AttemptState; status: string; created_at: string; stalled_since: string;
  cost_usd: number | null; pr: { number: number; external_url: string } | null; build: string; href: string;
};
export type AttemptState = (typeof ATTEMPT_STATES)[number];
/** How an item is accepted (§2/§14): the roadmap's accept, or THE start door with its body. */
export type Accept = { href: string; method: string; body: R };
export type NextItem = {
  id: string; label: string; detail: string; source: string; kind: string; state: "next" | "proposed"; href: string; external_url: string;
  accept: Accept | null;
  /** A backlog candidate's words for the work and where it came from (§8 `text`, `ref`). */
  text: string; ref: string;
};
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
  week: { at: string; label: string; kind: string; href: string }[];
};
export type Captain = { agent_id: string; name: string; role_key: string; avatar: string; status: string; squad: Agent[]; last_said: string };
export type WorkList = { items: WorkItem[] };
export type Knowledge = {
  repos: Repo[];
  dossier: { stack: string[]; commands: Record<string, string>; hotspots: { path: string; label: string; why: string; heat: number | null; has_tests: boolean | null }[];
             tests: { passed: number; total: number } | null; test_files: number | null; built_at: string; href: string } | null;
  graph: { nodes: { id: string; label: string; kind: string }[]; edges: { from: string; to: string; label: string }[]; href: string;
           counts: { nodes: number; rules: number | null; learned: number | null } };
  docs: { label: string; kind: string; href: string; sub: string }[];
  decisions: { label: string; at: string; href: string }[];
  learned: { label: string; sub: string; state: string; href: string }[];
};
export type Environment = {
  key: string; label: string; url: string; health: Health; host: string; host_href: string; target_id: string; app: string; manage_href: string;
  uptime_pct: number | null; p95_ms: number | null;
  error_rate: number | null; series: number[];
  last_deploy: { commit: string; at: string; by: string; label: string; href: string } | null;
  demo_logins: DemoLogin[]; policy: "auto" | "person"; href: string;
};
export type Deployments = {
  environments: Environment[];
  production: { state: "none" | "needs_host" | "ready"; action: { label: string; href: string } | null };
  ship_path: { label: string; state: StepState }[];
  history: { id: string; label: string; sub: string; at: string; state: StepState; href: string }[];
};
export type Recommendation = {
  id: string; title: string; kind: string; strip: { label: string; type: string }[]; value: number; effort: string; cost_usd: number | null;
  status: string; rank: number | null; href: string;
};
export type Roadmap = { now: NextItem[]; next: NextItem[]; later: NextItem[]; recommendations: Recommendation[]; counts: Record<string, number> };
export type OnboardingItem = { key: string; label: string; state: StepState; owner: "agent" | "you"; action: { label: string; href: string } | null; detail: string; sub: string };
export type Onboarding = {
  state: "not_started" | "running" | "ready" | "needs_you" | "failed";
  stages: Node[]; discover: { nodes: Node[] }; checklist: OnboardingItem[]; only_you: OnboardingItem[];
  first_work: { label: string; kind: string; source: string; estimate_min: number | null; estimate_usd: number | null; start: Accept | null; others_href: string } | null;
  backlog: NextItem[];
  /** What the agent is doing now, in words: the engine's `now`, else the checklist item in progress. */
  now: string; elapsed_s: number; error: string;
};
export type JumpItem = { label: string; kind: string; href: string; sub: string; external: boolean };
export type SettingsItem = { label: string; value: string; sub: string; reveal: { href: string; requires: string } | null; href: string };
/** A section's action: the existing write route (`href`, `method`) and, as `open`, the console
 *  screen where a person makes that change with its own form. Only `open` is ever a link. */
export type SettingsAction = { label: string; href: string; method: string; requires: string; open: string };
export type SettingsSection = {
  key: string; label: string; icon: string;
  items: SettingsItem[];
  actions: SettingsAction[];
  moved_from: string; hint: string;
  /** The captain section's choosable agents (§14 `candidates`). */
  candidates: Agent[];
};
export type Settings = { sections: SettingsSection[] };
export type HealthView = {
  environments: { key: string; label: string; target_id: string; url: string; health: Health; uptime_pct: number | null; error_rate: number | null; p95_ms: number | null;
                  series: { errors: number[]; latency: number[] }; slo: { target: number | null; burn: "ok" | "warning" | "breach" }; href: string }[];
  alerts: { id: string; label: string; severity: "page" | "ticket" | "info"; state: "firing" | "resolved"; since: string; href: string }[];
  incidents: { id: string; label: string; state: "running" | "needs_you" | "resolved"; graph_href: string; mini: { label: string; state: StepState }[] }[];
  monitoring_pack: { rules: number; managed_in: string; href: string } | null;
  links: { grafana: string; grafana_reason: string; logs: string; observability: string };
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
  return { name: name || s(x.role_key), role_key: s(x.role_key), avatar: s(x.avatar), role: s(x.role) || undefined, agent_id: s(x.agent_id) || undefined };
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
    // `cost_week` is the week's dollars (a number); an object {usd} is read the same way.
    cost_week: nn(x.cost_week) ?? nn(o(x.cost_week).usd), series: nums(x.series).length ? nums(x.series) : nums(o(x.cost_week).series),
  };
};
export const parseSummary = (v: unknown): ProjectsSummary => ({ items: a(o(v).items).map(parseCard).filter((c) => c.id) });

export const parseWorkItem = (v: unknown): WorkItem => {
  const x = o(v); const l = o(x.links); const pr = o(l.pr);
  return {
    id: s(x.id), label: s(x.label), kind: s(x.kind, "feature"), state: oneOf(x.state, WORK_STATES, "planned"), age: s(x.age),
    stages: a(x.stages).map((st) => ({ key: s(o(st).key), label: s(o(st).label), state: oneOf(o(st).state, STEP_STATES, "todo") })),
    agents: agents(x.agents), cost_usd: nn(x.cost_usd),
    links: { workflow_run: s(l.workflow_run), build: s(l.build), deploy: s(l.deploy), preview: s(l.preview), decision: s(l.decision),
             pr: pr.number || pr.external_url ? { number: n(pr.number), external_url: s(pr.external_url) } : null },
    detail: s(x.detail), href: s(x.href), source: s(x.source),
    source_label: s(x.source_label) || SOURCE_WORDS[s(x.source)] || "",
    stalled_since: s(x.stalled_since),
    attempts: a(x.attempts).map(parseAttempt).filter((t) => t.id),
    attempt_count: n(x.attempt_count), followups: n(x.followups), retries: n(x.retries),
  };
};
/** Never a raw internal string on a row: `code_task` is a Build. */
const SOURCE_WORDS: Record<string, string> = { code_task: "Build", workflow_run: "Workflow", roadmap: "Roadmap" };
const parseAttempt = (v: unknown): Attempt => {
  const x = o(v); const pr = o(x.pr);
  return {
    id: s(x.id), label: s(x.label, "Build"), kind: oneOf(x.kind, ["build", "retry", "followup"] as const, "build"), title: s(x.title),
    state: oneOf(x.state, ATTEMPT_STATES, "done"), status: s(x.status), created_at: s(x.created_at), stalled_since: s(x.stalled_since),
    cost_usd: nn(x.cost_usd), pr: pr.number || pr.external_url ? { number: n(pr.number), external_url: s(pr.external_url) } : null,
    build: s(x.build), href: s(x.href),
  };
};
/** The work item an attempt opens in the drawer: the group's row, seen as that one attempt. */
export function attemptAsItem(w: WorkItem, t: Attempt): WorkItem {
  return {
    ...w, id: t.id, label: t.label, state: t.state === "handed_on" ? "failed" : t.state, cost_usd: t.cost_usd,
    stalled_since: t.stalled_since, detail: t.title || w.detail, attempts: [], attempt_count: 1, followups: 0, retries: 0,
    links: { ...w.links, build: t.build, pr: t.pr, preview: "", decision: t.state === "needs_you" ? w.links.decision : "" },
    href: t.href,
  };
}
/** The item (or one attempt of one) a drawer id names. */
export function findWork(items: WorkItem[], id: string): WorkItem | undefined {
  const direct = items.find((x) => x.id === id);
  if (direct) return direct;
  for (const w of items) {
    const t = w.attempts.find((x) => x.id === id);
    if (t) return attemptAsItem(w, t);
  }
  return undefined;
}
/** "↻ 3 follow-ups" / "4 attempts": the group's pill (empty for a single attempt). */
export function attemptsPill(w: Pick<WorkItem, "attempt_count" | "followups" | "retries">): string {
  if (w.attempt_count <= 1) return "";
  if (w.followups && !w.retries) return `↻ ${w.followups} follow-up${w.followups === 1 ? "" : "s"}`;
  return `${w.attempt_count} attempts`;
}
export const KIND_WORDS: Record<string, string> = { feature: "Feature", bug: "Bug", fix: "Fix", ticket: "Ticket", chore: "Chore", incident: "Incident" };
export const ATTEMPT_WORDS: Record<AttemptState, string> = {
  planned: "Queued", running: "Running", stalled: "Stalled", handed_on: "Handed on", needs_you: "Needs you", done: "Done", failed: "Failed",
};
export const parseAccept = (v: unknown): Accept | null => {
  const x = o(v);
  return s(x.href) ? { href: s(x.href), method: s(x.method, "POST"), body: o(x.body) } : null;
};
const parseNext = (v: unknown): NextItem => {
  const x = o(v);
  return { id: s(x.id), label: label4(s(x.label) || s(x.title) || s(x.text)), detail: s(x.detail), source: s(x.source), kind: s(x.kind),
           state: x.state === "proposed" ? "proposed" : "next", href: s(x.href), external_url: s(x.external_url), accept: parseAccept(x.accept),
           text: s(x.text), ref: s(x.ref) };
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
    // A week's event opens its record in the console (a PR's build, a deploy), else where it lives.
    week: a(x.week).map((w) => ({ at: s(o(w).at), label: label4(o(w).label), kind: s(o(w).kind), href: s(o(w).href) || s(o(w).external_url) })),
  };
};
export const parseWork = (v: unknown): WorkList => ({ items: a(o(v).items).map(parseWorkItem).filter((w) => w.id) });

export const parseKnowledge = (v: unknown): Knowledge => {
  const x = o(v); const d = o(x.dossier); const g = o(x.graph); const c = o(g.counts);
  const cmds: Record<string, string> = {};
  for (const [k, val] of Object.entries(o(d.commands))) if (typeof val === "string" && val) cmds[k] = val;
  const tests = o(d.tests);
  const hasRun = typeof tests.total === "number" && tests.total > 0;
  return {
    repos: a(x.repos).map(parseRepo),
    dossier: Object.keys(d).length ? {
      stack: a(d.stack).map((z) => s(z)).filter(Boolean), commands: cmds,
      hotspots: a(d.hotspots).map((h) => ({ path: s(o(h).path), label: s(o(h).label), why: s(o(h).why), heat: nn(o(h).heat),
                                             has_tests: typeof o(h).has_tests === "boolean" ? (o(h).has_tests as boolean) : null })),
      // A pass count only when a run was read (CI); the dossier itself knows how many test files exist.
      tests: hasRun ? { passed: n(tests.passed), total: n(tests.total) } : null,
      test_files: nn(tests.files),
      built_at: s(d.built_at), href: s(d.href) } : null,
    graph: { nodes: a(g.nodes).map((z) => ({ id: s(o(z).id), label: label4(o(z).label), kind: s(o(z).kind) })),
             edges: a(g.edges).map((z) => ({ from: s(o(z).from), to: s(o(z).to), label: s(o(z).label) })), href: s(g.href),
             counts: { nodes: n(c.nodes, n(g.total_nodes, a(g.nodes).length)), rules: nn(c.rules), learned: nn(c.learned) } },
    // A doc opens where it lives (GitHub for a README or runbook), else its console page.
    docs: a(x.docs).map((z) => ({ label: s(o(z).label), kind: s(o(z).kind), href: s(o(z).external_url) || s(o(z).href), sub: s(o(z).sub) || s(o(z).detail) })),
    decisions: a(x.decisions).map((z) => ({ label: s(o(z).label), at: s(o(z).at), href: s(o(z).href) })),
    learned: a(x.learned).map((z) => ({ label: s(o(z).label), sub: s(o(z).sub), state: s(o(z).state), href: s(o(z).href) })),
  };
};

/** A deploy's status (stack_deployments) as the shared step state. */
const DEPLOY_STATE: Record<string, StepState> = {
  healthy: "done", succeeded: "done", success: "done", deployed: "done", live: "done",
  failed: "failed", unhealthy: "failed", error: "failed", rolled_back: "failed",
  running: "now", deploying: "now", applying: "now", pending: "waiting", queued: "waiting", awaiting_approval: "waiting",
};
const deployState = (status: unknown, state: unknown): StepState =>
  (STEP_STATES as readonly string[]).includes(String(state)) ? (state as StepState) : DEPLOY_STATE[s(status)] ?? "done";
export const parseDeployments = (v: unknown): Deployments => {
  const x = o(v); const pr = o(x.production); const act = o(pr.action);
  return {
    environments: a(x.environments).map((e) => {
      const y = o(e); const ld = o(y.last_deploy);
      return { key: s(y.key), label: s(y.label, s(y.key)), url: s(y.url), health: oneOf(y.health, HEALTHS, "unknown"), host: s(y.host),
               host_href: s(y.host_href), target_id: s(y.target_id), app: s(y.app), manage_href: s(y.manage_href),
               uptime_pct: nn(y.uptime_pct), p95_ms: nn(y.p95_ms), error_rate: nn(y.error_rate), series: nums(y.series),
               last_deploy: Object.keys(ld).length ? { commit: s(ld.commit), at: s(ld.at), by: s(ld.by),
                                                       label: s(ld.label) || s(ld.commit) || s(ld.status), href: s(ld.href) } : null,
               demo_logins: a(y.demo_logins).map(parseLogin), policy: y.policy === "person" ? "person" : "auto", href: s(y.href) };
    }),
    production: { state: oneOf(pr.state, ["none", "needs_host", "ready"] as const, "none"),
                  action: act.label || act.href ? { label: s(act.label, "Set up"), href: s(act.href) } : null },
    ship_path: mini(x.ship_path),
    history: a(x.history).map((h) => {
      const y = o(h);
      const sub = s(y.sub) || [s(y.status), s(y.by) ? `by ${s(y.by)}` : ""].filter(Boolean).join(" · ");
      return { id: s(y.id), label: s(y.label), sub, at: s(y.at), state: deployState(y.status, y.state), href: s(y.href) };
    }),
  };
};

export const parseOnboarding = (v: unknown): Onboarding => {
  const x = o(v); const fw = o(x.first_work);
  const item = (z: unknown): OnboardingItem => {
    const y = o(z); const act = o(y.action);
    return { key: s(y.key), label: label4(y.label), state: oneOf(y.state, STEP_STATES, "todo"), owner: y.owner === "you" ? "you" : "agent",
             action: act.label || act.href ? { label: s(act.label, "Do it"), href: s(act.href) } : null, detail: s(y.detail), sub: s(y.sub) };
  };
  const checklist = a(x.checklist).map(item);
  const onlyYou = a(x.only_you).length ? a(x.only_you).map(item) : checklist.filter((c) => c.owner === "you" && c.state !== "done");
  const state = oneOf(x.state, ["not_started", "running", "ready", "needs_you", "failed"] as const, "running");
  // WHAT IT IS DOING NOW: the engine has no sentence of its own; the item in progress says it.
  const doing = checklist.find((c) => c.state === "now" && c.owner === "agent");
  const now = s(x.now) || (state === "running" && doing ? [doing.label, doing.sub || doing.detail].filter(Boolean).join(" · ") : "");
  return {
    state, stages: a(x.stages).map(parseNode), discover: { nodes: a(o(x.discover).nodes).map(parseNode) }, checklist, only_you: onlyYou,
    first_work: Object.keys(fw).length ? { label: s(fw.label), kind: s(fw.kind), source: s(fw.source), estimate_min: nn(fw.estimate_min),
                                           estimate_usd: nn(fw.estimate_usd), start: parseAccept(fw.start), others_href: s(fw.others_href) } : null,
    backlog: a(x.backlog).map(parseNext), now, elapsed_s: n(x.elapsed_s), error: s(x.error),
  };
};

export const parseJump = (v: unknown): JumpItem[] => a(o(v).items).map((z) => {
  const y = o(z); const href = s(y.href);
  return { label: s(y.label), kind: s(y.kind), href, sub: s(y.sub), external: /^https?:\/\//.test(href) };
}).filter((j) => j.label && j.href);

const POLICY_WORD: Record<string, string> = { auto: "deploys on merge", person: "a person approves" };
/** One settings row, from whatever the section's items are (§11/§14: demo users, deploy values,
 *  targets and hosts, the squad, autonomy levels, repositories, notification policies). */
function settingsItem(key: string, q: R): SettingsItem {
  const r = o(q.reveal);
  const reveal = r.href ? { href: s(r.href), requires: s(r.requires, "step_up") } : null;
  if (key === "demo_logins") return { label: s(q.label) || s(q.user), value: s(q.user) || s(q.value), sub: s(q.env_label) || s(q.sub) || s(q.env), href: s(q.href), reveal };
  if (key === "deploy_values" && s(q.name)) {
    return { label: s(q.name), value: q.set === false ? "not set" : s(q.mode, "set"), sub: [s(q.app_key), s(q.env)].filter(Boolean).join(" · "), href: s(q.href), reveal: null };
  }
  if (key === "targets" && q.kind === "target") {
    return { label: [s(q.app), s(q.env_label) || s(q.env)].filter(Boolean).join(" · "), value: q.paused ? "paused" : POLICY_WORD[s(q.policy)] ?? s(q.policy),
             sub: s(q.host) ? `on ${s(q.host)}` : "", href: s(q.href), reveal: null };
  }
  if (key === "targets" && q.kind === "host") {
    return { label: s(q.name), value: s(q.status), sub: [s(q.address), q.shared ? "shared server" : ""].filter(Boolean).join(" · "), href: s(q.href), reveal: null };
  }
  if (key === "autonomy" && s(q.task_class)) {
    return { label: s(q.task_class).replace(/_/g, " "), value: s(q.level), sub: s(q.ceiling) ? `ceiling ${s(q.ceiling)}` : "", href: s(q.href), reveal: null };
  }
  if (key === "repos" && s(q.full_name)) {
    return { label: s(q.full_name), value: s(q.branch, "main"), sub: q.primary ? "primary" : "", href: s(q.href) || s(q.external_url), reveal: null };
  }
  if (key === "notifications" && (s(q.name) || s(q.id))) {
    const n2 = n(q.contact_points);
    return { label: s(q.name, "Default"), value: `${n2} contact point${n2 === 1 ? "" : "s"}`, sub: "", href: s(q.href), reveal: null };
  }
  return { label: s(q.label) || s(q.user) || s(q.name), value: s(q.value) || s(q.user), sub: s(q.sub) || s(q.role) || s(q.env), href: s(q.href), reveal };
}
export const parseSettings = (v: unknown): Settings => ({
  sections: a(o(v).sections).map((z) => {
    const y = o(z);
    const key = s(y.key);
    const items = a(y.items).map((it) => settingsItem(key, o(it))).filter((it) => it.label);
    // A section's own facts (autonomy's ambient budget and spending cap) are rows too.
    if (key === "autonomy") {
      const amb = o(y.ambient); const bud = o(y.budget);
      if (nn(amb.daily_cap_usd) !== null) items.push({ label: "Ambient budget", value: `$${n(amb.daily_cap_usd)}/day`, sub: nn(amb.max_concurrent) !== null ? `${n(amb.max_concurrent)} at once` : "", href: "", reveal: null });
      if (Object.keys(bud).length) {
        items.push({ label: "Spending cap", value: n(bud.monthly_cap_usd) ? `$${n(bud.monthly_cap_usd)}/month` : n(bud.daily_cap_usd) ? `$${n(bud.daily_cap_usd)}/day` : "not set",
                     sub: s(bud.intensity), href: "", reveal: null });
      }
    }
    return {
      key, label: s(y.label), icon: s(y.icon), moved_from: s(y.moved_from), hint: s(y.hint),
      items, candidates: agents(y.candidates),
      actions: a(y.actions).map((q) => ({ label: s(o(q).label), href: s(o(q).href), method: s(o(q).method, "GET"), requires: s(o(q).requires),
                                          // An in-console href is its own place to go; an API route is not a link.
                                          open: s(o(q).open) || (s(o(q).href).startsWith("?") ? s(o(q).href) : "") })).filter((q) => q.label),
    };
  }).filter((sec) => sec.key),
});

export const parseHealth = (v: unknown): HealthView => {
  const x = o(v); const mp = o(x.monitoring_pack); const l = o(x.links);
  return {
    environments: a(x.environments).map((e) => {
      const y = o(e); const ser = o(y.series); const slo = o(y.slo);
      return { key: s(y.key), label: s(y.label, s(y.key)), target_id: s(y.target_id), url: s(y.url), health: oneOf(y.health, HEALTHS, "unknown"), uptime_pct: nn(y.uptime_pct),
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
    links: { grafana: s(l.grafana), grafana_reason: s(l.grafana_reason), logs: s(l.logs), observability: s(l.observability) },
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
export function pxHref(href: string, search = isHydrated() ? window.location.search : ""): string {
  // Before hydration the server's text (no org): the browser's first render must match it, and the
  // shell's link handler restores the org on any in-console link that lacks it.
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

/** The page's own search params, the same on the server and in the browser's first render
 *  (Next's), or the window's where there is no Next router (a test). */
export function usePageSearch(): URLSearchParams {
  const sp = useSearchParams();
  return new URLSearchParams(sp ? sp.toString() : typeof window === "undefined" ? "" : window.location.search);
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
  const demoMode = usePageSearch().get("px_demo") === "1";
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
    // Times on these screens are the viewer's (lib/local-time.ts): mounted means hydrated.
    markHydrated();
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

  // Coming back on screen asks at once (not on mount: the effect above has just asked).
  const wasShown = useRef(shown);
  useEffect(() => {
    if (shown && !wasShown.current) kick.current();
    wasShown.current = shown;
  }, [shown]);
  const refresh = useCallback(() => kick.current(), []);
  return { data, state, error, demo: demoMode && Boolean(opts.demo), refresh };
}

// ── writes ───────────────────────────────────────────────────────────────────────────────────

export type PxWrite = { ok: boolean; status: number; body: R | null; error: string | null; demo?: boolean;
  /** A start the project isn't ready for (409 from THE start door): its checklist, for "Start anyway". */
  readiness?: unknown };

/** One write. Sample mode sends nothing and says so. */
export async function pxWrite(path: string, body: R, label: string): Promise<PxWrite> {
  if (isPxDemo()) return { ok: true, status: 200, body: {}, error: null, demo: true };
  const r = await obsWrite<R>(path, body, { scope: `px:${path}`, label });
  const refused = o(o(r.refusal).detail);
  // A refusal's own sentence (`message` beside an `error` code) reads better than the code.
  const sentence = r.status === 401 || r.status === 403 ? "" : s(o(r.refusal).message) || s(refused.message);
  return { ok: r.ok, status: r.status, body: r.body, error: !r.ok && sentence ? sentence : r.error,
           ...(!r.ok && refused.readiness ? { readiness: refused.readiness } : {}) };
}
export type WorkKind = "feature" | "bug" | "ticket";
export const toWorkKind = (v: unknown): WorkKind => (v === "bug" || v === "ticket" ? v : "feature");
/** §3 New work through THE start door. `extra` carries the door's own fields (title, source_ref,
 *  start_anyway) so a refused start can be re-sent with the person's ticks. */
export const startWork = (projectId: string, kind: WorkKind, text: string, extra: R = {}) =>
  pxWrite(`/v1/projects/${enc(projectId)}/work`, { kind, text, ...extra }, "Start");
/** An item's own accept (§2 next, §8 first work / backlog): the roadmap's accept route, or THE
 *  start door with the body the engine wrote for it. Nothing else is ever posted. */
export async function runAccept(projectId: string, accept: Accept, label = "Accept"): Promise<PxWrite> {
  const roadmap = /^\/v1\/ambient\/roadmap\/([^/]+)\/accept$/.exec(accept.href);
  if (roadmap) {
    const as = s(accept.body.as, "ticket") as AcceptAs;
    return acceptRecommendation(projectId, decodeURIComponent(roadmap[1]), as);
  }
  if (accept.href === `/v1/projects/${enc(projectId)}/work`) {
    const b = accept.body;
    return startWork(projectId, toWorkKind(b.kind), s(b.text) || s(b.title), {
      ...(s(b.title) ? { title: s(b.title) } : {}), ...(s(b.source_ref) ? { source_ref: s(b.source_ref) } : {}) });
  }
  return { ok: false, status: 0, body: null, error: `${label}: BOSS named a route this page doesn't send (${accept.href}).` };
}
/** Onboarding's first work (§8 `first_work`): the engine's own `start`, else the door with its words. */
export const firstWorkAccept = (projectId: string, fw: NonNullable<Onboarding["first_work"]>): Accept => fw.start ?? ({
  href: `/v1/projects/${enc(projectId)}/work`, method: "POST", body: { kind: toWorkKind(fw.kind || (fw.source === "bug" ? "bug" : "ticket")), text: fw.label, title: fw.label },
});
/** A backlog candidate the onboarding agent proposed (§8 backlog[]): started through the door. */
export const backlogAccept = (projectId: string, b: NextItem): Accept => b.accept ?? ({
  href: `/v1/projects/${enc(projectId)}/work`, method: "POST",
  body: { kind: toWorkKind(b.kind), text: b.text || b.detail || b.label, title: b.label, ...(b.ref ? { source_ref: b.ref } : {}) },
});
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
    next: a(v.next).map((x) => ({ id: s(o(x).key), label: s(o(x).label), detail: "", source: s(o(x).who), kind: "", state: "next" as const, href: "",
                                  external_url: "", accept: null, text: "", ref: "" })),
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
             preview: "", decision: "", pr: pr ? { number: prNo, external_url: pr } : null },
    detail: s(r.status), href: "", source: s(r.workflow), source_label: "Workflow",
    stalled_since: "", attempts: [], attempt_count: 1, followups: 0, retries: 0,
  };
}
/** The open-work bar's segments, in the order and colours of the mock (running · needs you · planned · done). */
export const WORK_ORDER: WorkState[] = ["needs_you", "stalled", "running", "planned", "done", "failed"];
export const WORK_WORDS: Record<WorkState, string> = { needs_you: "Needs you", stalled: "Stalled", running: "Running", planned: "Planned", done: "Done", failed: "Failed" };

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
