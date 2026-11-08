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
import { parseDiagnosis, parseWaitReason, type Diagnosis, type WaitReason } from "./stuck-work";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { onChanged } from "../app/changed";
import { obsWrite } from "../app/observability/obs-write";
import { useAreaShown } from "./area-shown";
import { isConsoleLive, subscribeConsoleLive } from "./console-live";
import { isHydrated, localWith, markHydrated } from "./local-time";

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

export type Agent = { name: string; role_key: string; avatar: string; role?: string; agent_id?: string;
  /** "working" when the agent is acting right now (the working-face avatar). */
  state?: "working" | "idle" };
export type Node = { key: string; label: string; state: StepState; sub: string; href: string;
  /** The node's tooltip in words ("Ready for work: 6 of 6 checks"). */
  detail?: string;
  /** When a done node happened (ISO): its sub is that day in the VIEWER's zone, and "{day}" in
   *  its detail is replaced by it (04-02 — the server never writes a day). */
  at?: string };
export type ProjectCard = {
  id: string; name: string; initials: string; repo: string; stage: Stage; journey: Node[];
  needs_you: number; running: number; health: Health; live_url: string; captain: Agent | null;
  updated_at: string; href: string; cost_week: number | null; series: number[];
  /** On a subscription: the week at list price (cost_words), never a bare $0.00. */
  on_subscription?: boolean; list_week?: number | null;
  /** Screen 02 (cards.extras): the ring's number, the two status lines, the live crew. */
  status_label: string; onboarding_active: boolean; start_onboarding: Accept | null; archived: boolean;
  uptime_pct: number | null; onboarding_progress: [number, number] | null; lines: string[]; line_tone: string;
  crew: Agent[]; merged: number; building: number; week_cap_usd: number | null;
  /** 02-05 the agents acting right now (the Running tile's working faces). */
  live_agents: Agent[];
};
export type ProjectsSummary = { items: ProjectCard[]; week_cap_usd: number | null };
export type Repo = { full_name: string; branch: string; access: { read: boolean; write: boolean; pr: boolean }; external_url: string };
export type NeedsYou = {
  id: string; kind: "merge" | "deploy" | "plan" | "question" | "setup"; label: string; detail: string; icon?: string;
  mini: { label: string; state: StepState }[]; action: { label: string; href: string; decision_id: string } | null;
  /** The piece of work it belongs to (a merge's build; the Onboarding row for its "only you"
   *  items), the PR on GitHub, and when it was asked (the drawer). */
  work_id?: string; work_label?: string; external_url?: string; created_at?: string;
  /** What the Shepherd found about the stuck or refused work this card is about (stuck_work). */
  diagnosis?: Diagnosis | null;
  /** A deploy's release card (engine release_scope, needs.release_of): the commits it ships as
   *  Approvals draws them (`preview`, an action preview), the hash an approval must carry, and
   *  whether approving needs the person's reason. Null when the deploy has no card. */
  release?: NeedsRelease | null;
  /** AN ALWAYS-ON CARD'S OWN ANSWERS (engine needs.ambient_answers): the card's, each with the
   *  decision action that carries it ("Close both" → reject), and each entry's own question,
   *  one-line finding and answers, answered one by one. Absent on every other card. */
  answers?: NeedsAnswer[];
  entries?: NeedsEntry[];
};
export type NeedsAnswer = { action: "approve" | "reject" | "revise"; label: string };
export type NeedsEntry = { work_item_id: string; question: string; detail: string; answers: { key: string; label: string }[] };
function parseAnswers(v: unknown): NeedsAnswer[] | undefined {
  if (!Array.isArray(v) || !v.length) return undefined;
  return v.map((x) => x as Record<string, unknown>)
    .filter((x) => x.action === "approve" || x.action === "reject" || x.action === "revise")
    .map((x) => ({ action: x.action as NeedsAnswer["action"], label: String(x.label ?? "") }));
}
function parseEntries(v: unknown): NeedsEntry[] | undefined {
  if (!Array.isArray(v) || !v.length) return undefined;
  return v.map((x) => x as Record<string, unknown>).map((x) => ({
    work_item_id: String(x.work_item_id ?? ""), question: String(x.question ?? ""), detail: String(x.detail ?? ""),
    answers: (Array.isArray(x.answers) ? x.answers : []).map((a) => a as Record<string, unknown>)
      .map((a) => ({ key: String(a.key ?? ""), label: String(a.label ?? "") })),
  }));
}
export type NeedsRelease ={ preview: unknown; content_hash: string; needs_reason: boolean; sentence: string };
function parseRelease(v: unknown): NeedsRelease | null {
  if (!v || typeof v !== "object") return null;
  const r = v as Record<string, unknown>;
  const hash = typeof r.content_hash === "string" ? r.content_hash : "";
  if (!hash || !r.preview || typeof r.preview !== "object") return null;
  return { preview: r.preview, content_hash: hash, needs_reason: r.needs_reason === true,
           sentence: typeof r.sentence === "string" ? r.sentence : "" };
}
/** X-75 ONE TIME BASIS for a piece of work, on every surface (Work rows, Overview's Recent work,
 *  drawers): a finished item (done / failed) is placed by WHEN IT FINISHED (`updated_at`):
 *  "12h" within a day, "Sat" this week, else "Nov 2", in the viewer's zone; anything still
 *  open by how long since it started (`age`). Never "Sat" on one screen and "12h" on another. */
export function workWhen(w: { state: string; age?: string; updated_at?: string | null; created_at?: string | null }, nowMs: number = Date.now()): string {
  if (w.state !== "done" && w.state !== "failed") return w.age || "";
  const at = Date.parse(w.updated_at || w.created_at || "");
  if (!Number.isFinite(at)) return w.age || "";
  const ago = Math.max(0, nowMs - at);
  if (ago < 3_600_000) return `${Math.max(1, Math.floor(ago / 60_000))}m`;
  if (ago < 86_400_000) return `${Math.floor(ago / 3_600_000)}h`;
  return ago < 6 * 86_400_000 ? localWith(at, { weekday: "short" }) : localWith(at, { month: "short", day: "numeric" });
}

export type WorkItem = {
  id: string; label: string; kind: string; state: WorkState; age: string;
  stages: { key: string; label: string; state: StepState }[]; agents: Agent[]; cost_usd: number | null;
  /** What the subscription calls would have cost at list price (costWords). */
  list_usd?: number | null;
  links: { workflow_run: string; build: string; pr: { number: number; external_url: string } | null; deploy: string; preview: string; decision: string;
    /** Its build's own live log: the build page with Logs open on that run (`logs=build`). */
    log?: string };
  detail: string; href: string; source: string;
  /** What its build waits for, in words (stuck_work.wait_reason), and what the Shepherd found. */
  why?: WaitReason | null; diagnosis?: Diagnosis | null;
  /** "Build" | "Workflow" | "Roadmap": the human word for `source` (never the raw string). */
  source_label: string;
  /** When it stopped moving (ISO), for a `stalled` item. */
  stalled_since: string;
  /** Every attempt of this piece of work, newest first (empty when it had only one). */
  attempts: Attempt[]; attempt_count: number; followups: number; retries: number;
  /** "Run #43": the workflow run's number in its project (null for a build on its own). */
  run_number: number | null; created_at: string; updated_at: string;
  /** The person's one action, named for what it does: "Merge" | "Approve deploy" | "Decide"… */
  action_label: string;
  /** The issue it came from (GitHub #12, a Plane key): the row's link chip (05-20). */
  issue: WorkIssue | null;
  /** Planned work: where it came from (ci | bug | ticket | roadmap), its tag, Marshal's rank. */
  origin: string; origin_label: string; next_up: boolean; lane: "next" | "later" | ""; rank: number | null;
  /** The file a failing check names ("tests/test_notify.py"). */
  where: string;
  /** Planned work: THE start door with its body (Start now), and the re-rank route (drag). */
  start: Accept | null; move: { href: string; method: string } | null;
  /** The onboarding row: its mini flow (Discover → Ready → Backlog), what waits on you, its drawer. */
  flow: { key: string; label: string; state: StepState }[]; waiting: { key: string; label: string }[]; onboarding: string;
};
export type WorkIssue = { provider: string; number: number | null; label: string; kind: string; external_url: string };
/** One attempt of a piece of work: the build, a retry, or a follow-up (§3 `attempts[]`). */
export type Attempt = {
  id: string; label: string; kind: "build" | "retry" | "followup"; title: string;
  state: AttemptState; status: string; created_at: string; stalled_since: string;
  cost_usd: number | null; list_usd?: number | null; pr: { number: number; external_url: string } | null; build: string; href: string;
};
export type AttemptState = (typeof ATTEMPT_STATES)[number];
/** How an item is accepted (§2/§14): the roadmap's accept, or THE start door with its body. */
export type Accept = { href: string; method: string; body: R };
export type NextItem = {
  id: string; label: string; detail: string; source: string; kind: string; state: "next" | "proposed"; href: string; external_url: string;
  /** Already accepted onto the Roadmap's Next lane ("Feature · accepted", a Next tag). */
  accepted?: boolean;
  accept: Accept | null;
  /** A backlog candidate's words for the work and where it came from (§8 `text`, `ref`). */
  text: string; ref: string;
  /** Why, in a few words ("71/190 pass · a blocked build"); the Roadmap item it is (`rec_id`). */
  why?: string; rec_id?: string;
};
export type DemoLogin = { user: string; label: string; reveal: { href: string; requires: string } | null };
export type Overview = {
  project: { id: string; name: string; initials: string; stage: Stage; repos: Repo[]; live_url: string; status_label: string; health: Health;
    /** Why it is not healthy, in words (health.state_of): "SLO breached: 98.13% up, target 99.5%". */
    health_reason?: string };
  journey: Node[]; needs_you: NeedsYou[];
  /** Cards that could not load this time ({card: words}): each shows "Couldn't load · Retry". */
  errors?: Record<string, string>;
  /** The Roadmap tab's count (open + accepted). */
  roadmap_count?: number | null;
  tiles: {
    open_work: { count: number; by_state: Record<string, number>; href: string } | null;
    running: { count: number; current: { label: string; progress: [number, number]; agent: Agent | null; sub?: string; id?: string } | null; href: string } | null;
    staging: { health: Health; uptime_pct: number | null; p95_ms: number | null; series: number[]; href: string; p95_source?: string; label?: string;
               health_reason?: string; window?: string } | null;
    cost_week: { usd: number; delta_pct: number | null; cap_usd: number | null; series: number[]; href: string;
      /** No money this week because the work runs on a subscription: the list price instead. */
      on_subscription?: boolean; list_usd?: number | null; list_series?: number[];
      /** The list-price delta on a subscription week; first_week: nothing last week to compare. */
      list_delta_pct?: number | null; first_week?: boolean; cap_pct?: number | null } | null;
  };
  captain: Captain | null; recent_work: WorkItem[]; next: NextItem[];
  /** The Backlog stage's list: the items its count is made of (open + accepted on the Roadmap). */
  backlog?: NextItem[];
  try_it: { url: string; demo_logins: DemoLogin[] } | null;
  week: { at: string; label: string; kind: string; href: string; detail?: string }[];
};
export type Captain = { agent_id: string; name: string; role_key: string; avatar: string; status: string; squad: Agent[]; last_said: string;
  /** 04-34 Marshal (Chief of Staff), who ranks this project's backlog against the others: closes the crew row. */
  ranker?: Agent | null };
export type WorkList = { items: WorkItem[] };
/** A repository on Repo & knowledge (screen 06): access is tri-state — true / false as the last
 *  check found, null when it was never checked. */
export type KnowledgeRepo = Repo & {
  binding_id: string; checked: { read: boolean | null; write: boolean | null; pr: boolean | null };
  last_push: string; via: string; deploys_to: string;
  /** A failed check's own reason; the Staging node's real result (06-04). */
  access_reasons?: { read?: string | null; write?: string | null; pr?: string | null };
  deploy_check?: { label: string; state: StepState; reason: string } | null;
};
export type GraphNode = { id: string; label: string; kind: string; group: string; detail: string };
export type Knowledge = {
  repos: KnowledgeRepo[];
  dossier: { stack: string[]; commands: Record<string, string>; hotspots: { path: string; label: string; why: string; heat: number | null; has_tests: boolean | null }[];
             /** `env`: BOSS could not run the suite (its environment, not the app's tests): the
              *  ring says so instead of a pass rate; `not_run` tests never ran. */
             tests: { passed: number; total: number; failing: number; from: string; href: string;
                      env?: { sentence: string; reason: string; owner: string } | null; not_run?: number } | null; test_files: number | null; tests_line: string;
             code_map: { parts: { dir: string; files: number }[]; files: number };
             by: string; built_ago: string; built_at: string; href: string } | null;
  graph: { nodes: GraphNode[]; edges: { from: string; to: string; label: string; kind: string }[]; href: string; hub: string;
           counts: { nodes: number; rules: number | null; learned: number | null } };
  docs: { label: string; kind: string; href: string; sub: string; path: string; external_url: string }[];
  decisions: { label: string; at: string; href: string; sub: string; detail?: string }[];
  learned: { id: string; label: string; sub: string; state: string; href: string; detail: string; reused: number; agent: Agent | null }[];
  build: { label: string; href: string; state: string; progress: string; nodes: { label: string; state: StepState; sub: string }[] } | null;
  empty: { label: string; detail: string; href: string } | null;
};
export type Environment = {
  key: string; label: string; url: string; health: Health; host: string; host_href: string; target_id: string; app: string; manage_href: string;
  /** The server's name for a title ("Staging server" when its name is only its address). */
  host_name: string;
  /** How many projects share this server (the "runs on" tooltip). */
  host_projects: number | null;
  /** Where p95 comes from: the app's own metrics, or BOSS's probe. */
  p95_source?: string;
  /** The window uptime and p95 are measured over ("24 h"), said beside them. */
  window?: string; health_reason?: string;
  uptime_pct: number | null; p95_ms: number | null;
  error_rate: number | null; series: number[];
  /** 24 hourly bars, oldest first (08-08): each hour's probes and its state. */
  hours: { at: string; n: number; ok: number; state: "healthy" | "degraded" | "down" | "none" }[];
  last_deploy: { commit: string; at: string; by: string; label: string; href: string; pr: number | null; pr_url: string } | null;
  demo_logins: DemoLogin[]; policy: "auto" | "person"; href: string;
};
export type Deployments = {
  environments: Environment[];
  production: { state: "none" | "needs_host" | "ready"; action: { label: string; href: string } | null };
  ship_path: { label: string; state: StepState }[];
  history: { id: string; label: string; sub: string; at: string; state: StepState; href: string; word: string; by: string; by_agent: boolean;
    /** A deploy that failed and an agent recovered (a later deploy went live): amber ↺. */
    recovered: boolean }[];
};
export type Recommendation = {
  id: string; title: string; kind: string; strip: { label: string; type: string }[]; value: number; effort: string; cost_usd: number | null;
  status: string; rank: number | null; href: string;
};
export type Roadmap = { now: NextItem[]; next: NextItem[]; later: NextItem[]; recommendations: Recommendation[]; counts: Record<string, number> };
/** An item's one action: a link (`href`), or — with `do` — a job the page does in place
 *  (`do: "staging_target"`: the confirm sheet that POSTs `post` for `host_id`; `href` is Infra). */
export type OnboardingAction = { label: string; href: string; do?: "staging_target"; host_id?: string; post?: string };
export type OnboardingItem = { key: string; label: string; state: StepState; owner: "agent" | "you"; action: OnboardingAction | null; detail: string; sub: string };
/** What onboarding's "Use for staging" will make (GET …/onboarding/deploy-target). */
export type StagingProposal = { project: string; app_name: string; repository: string; environment: string; host_id: string; host: string;
                                address: string; shared: boolean; port: number | null; manifest_path: string };
export type OnboardingLog = { at: string; agent: string; label: string; state: StepState; text: string };
export type Onboarding = {
  state: "not_started" | "running" | "ready" | "needs_you" | "failed";
  stages: Node[]; discover: { nodes: Node[]; live?: Node[] }; checklist: OnboardingItem[]; only_you: OnboardingItem[];
  first_work: { label: string; kind: string; source: string; estimate_min: number | null; estimate_usd: number | null; start: Accept | null; others_href: string;
                /** Why it comes first (Marshal's reason, or the score), and what the estimate is based on. */
                why?: string; why_first?: string; first_by?: string; estimate_runs?: number; estimate_basis?: string; workflow?: string; external_url?: string } | null;
  backlog: NextItem[];
  /** What the agent is doing now, in words: the engine's `now`, else the checklist item in progress. */
  now: string; elapsed_s: number; error: string;
  /** The Now line's agent (role key) and whether it moves now (10-09); the live log (Watch). */
  now_agent: string; now_live: boolean; log: OnboardingLog[];
  /** THE PHASE, one place (00b-13 / PX-15): live only while the run moves, and its title. */
  live: boolean; title: string;
  /** Screen 10: the Ready card's rows (its words, the agent on each), the whole backlog's size,
   *  which candidates are already on the Roadmap, and the accept route ("Accept backlog"). */
  ready: (OnboardingItem & { agent: string })[]; backlog_total: number; accepted: string[]; accept_backlog: Accept | null;
  /** A finished run: seconds since it read the repo ("re-read 38s ago"); how many runs so far; the
   *  run's trigger (created · manual · repo_added); the test baseline and where it came from. */
  read_ago_s?: number | null; runs?: number; trigger?: string; started_at?: string;
  tests?: { passed: number; total: number; failing: number; source: string; cause: string } | null;
  /** Add a project v2: the person's note to Helm from the one screen (lines under the repo). */
  seed?: { kind: string; note: string | null; title: string | null } | null;
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
  /** The section as the engine sent it (screen 13 draws each section's own fields from it:
   *  environments, rows in words, levels, routes…). Read with the o/a/s helpers. */
  raw: Record<string, unknown>;
};
export type Settings = { sections: SettingsSection[] };
export type HealthView = {
  environments: { key: string; label: string; target_id: string; url: string; health: Health; health_reason?: string; window?: string; p95_source?: string;
                  uptime_pct: number | null; error_rate: number | null; p95_ms: number | null;
                  series: { errors: number[]; latency: number[] }; slo: { target: number | null; burn: "ok" | "warning" | "breach"; budget_used_pct: number | null }; href: string }[];
  /** One row per alert rule (screen 12): firing since …, resolved at …, or ok · quiet 3d. */
  alerts: { id: string; label: string; condition: string; severity: "page" | "ticket" | "info"; state: "firing" | "resolved" | "ok" | "off"; since: string; quiet: string; href: string }[];
  incidents: { id: string; label: string; state: "running" | "needs_you" | "resolved"; since: string; duration_s: number | null; outcome: string; graph_href: string;
    /** An open incident held after it recovered: when BOSS closes it on its own. */
    closes_at?: string | null;
               mini: { label: string; state: StepState; type: string; at?: string | null }[] }[];
  monitoring_pack: { rules: number; off: number; managed_in: string; href: string; kinds: string[]; file: { path: string; external_url: string } | null } | null;
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
  return { name: name || s(x.role_key), role_key: s(x.role_key), avatar: s(x.avatar), role: s(x.role) || undefined, agent_id: s(x.agent_id) || undefined,
           ...(x.state === "working" || x.state === "idle" ? { state: x.state } : {}) };
};
const agents = (v: unknown): Agent[] => a(v).map(parseAgent).filter((x): x is Agent => x !== null);
export const parseNode = (v: unknown): Node => {
  const x = o(v);
  return { key: s(x.key), label: label4(x.label), state: oneOf(x.state, STEP_STATES, "todo"), sub: s(x.sub), href: s(x.href), detail: s(x.detail), at: s(x.at) || undefined };
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
    on_subscription: x.on_subscription === true, list_week: nn(x.list_week),
    status_label: s(x.status_label), onboarding_active: x.onboarding_active === true, archived: x.archived === true,
    start_onboarding: parseAccept(x.start_onboarding),
    uptime_pct: nn(x.uptime_pct),
    onboarding_progress: Array.isArray(x.onboarding_progress) && x.onboarding_progress.length === 2
      ? [n(x.onboarding_progress[0]), n(x.onboarding_progress[1])] : null,
    lines: a(x.lines).map((l) => s(l)).filter(Boolean), line_tone: s(x.line_tone),
    crew: a(x.crew).map(parseAgent).filter((g): g is Agent => g !== null),
    live_agents: a(x.live_agents).map(parseAgent).filter((g): g is Agent => g !== null),
    merged: n(x.merged), building: n(x.building), week_cap_usd: nn(x.week_cap_usd),
  };
};
export const parseSummary = (v: unknown): ProjectsSummary => ({ items: a(o(v).items).map(parseCard).filter((c) => c.id), week_cap_usd: nn(o(v).week_cap_usd) });

/** A build link as its live log: the same build, `logs=build` (never the old `view` value, which the
 *  build page never read — the old "Live log" opened the page with Logs closed). */
export function logOfBuild(build: string): string {
  if (!build) return "";
  const [path, query = ""] = build.split("?");
  const q = new URLSearchParams(query);
  q.delete("view");
  q.set("logs", "build");
  return `${path}?${q.toString()}`;
}
export const parseWorkItem = (v: unknown): WorkItem => {
  const x = o(v); const l = o(x.links); const pr = o(l.pr);
  return {
    id: s(x.id), label: s(x.label), kind: s(x.kind, "feature"), state: oneOf(x.state, WORK_STATES, "planned"), age: s(x.age),
    stages: a(x.stages).map((st) => ({ key: s(o(st).key), label: s(o(st).label), state: oneOf(o(st).state, STEP_STATES, "todo") })),
    agents: agents(x.agents), cost_usd: nn(x.cost_usd), list_usd: nn(x.list_usd),
    links: { workflow_run: s(l.workflow_run), build: s(l.build), deploy: s(l.deploy), preview: s(l.preview), decision: s(l.decision),
             log: s(l.log) || (s(l.build) ? logOfBuild(s(l.build)) : ""),
             pr: pr.number || pr.external_url ? { number: n(pr.number), external_url: s(pr.external_url) } : null },
    detail: s(x.detail), href: s(x.href), source: s(x.source),
    why: parseWaitReason(x.why), diagnosis: parseDiagnosis(x.diagnosis),
    source_label: s(x.source_label) || SOURCE_WORDS[s(x.source)] || "",
    stalled_since: s(x.stalled_since),
    attempts: a(x.attempts).map(parseAttempt).filter((t) => t.id),
    attempt_count: n(x.attempt_count), followups: n(x.followups), retries: n(x.retries),
    run_number: nn(x.run_number), created_at: s(x.created_at), updated_at: s(x.updated_at),
    action_label: s(x.action_label),
    issue: parseIssue(l.issue),
    origin: s(x.origin), origin_label: s(x.origin_label), next_up: x.next_up === true,
    lane: x.lane === "next" || x.lane === "later" ? x.lane : "", rank: nn(x.rank), where: s(x.where),
    start: parseAccept(x.start), move: o(x.move).href ? { href: s(o(x.move).href), method: s(o(x.move).method, "POST") } : null,
    flow: a(x.flow).map((f) => ({ key: s(o(f).key), label: s(o(f).label), state: oneOf(o(f).state, STEP_STATES, "todo") })),
    waiting: a(x.waiting).map((z) => ({ key: s(o(z).key), label: s(o(z).label) })).filter((z) => z.label),
    onboarding: s(l.onboarding),
  };
};
/** The fields only some work carries (planned, from an issue, the onboarding row), empty. */
export const WORK_EXTRA: Pick<WorkItem, "issue" | "origin" | "origin_label" | "next_up" | "lane" | "rank" | "where" | "start" | "move" | "flow" | "waiting" | "onboarding"> = {
  issue: null, origin: "", origin_label: "", next_up: false, lane: "", rank: null, where: "", start: null, move: null, flow: [], waiting: [], onboarding: "",
};
const parseIssue = (v: unknown): WorkIssue | null => {
  const x = o(v);
  if (!s(x.external_url)) return null;
  return { provider: s(x.provider, "github"), number: nn(x.number), label: s(x.label) || (nn(x.number) ? `#${n(x.number)}` : "Issue"),
           kind: s(x.kind, "issue"), external_url: s(x.external_url) };
};
/** Never a raw internal string on a row: `code_task` is a Build. */
const SOURCE_WORDS: Record<string, string> = { code_task: "Build", workflow_run: "Workflow", roadmap: "Roadmap" };
const parseAttempt = (v: unknown): Attempt => {
  const x = o(v); const pr = o(x.pr);
  return {
    id: s(x.id), label: s(x.label, "Build"), kind: oneOf(x.kind, ["build", "retry", "followup"] as const, "build"), title: s(x.title),
    state: oneOf(x.state, ATTEMPT_STATES, "done"), status: s(x.status), created_at: s(x.created_at), stalled_since: s(x.stalled_since),
    cost_usd: nn(x.cost_usd), list_usd: nn(x.list_usd), pr: pr.number || pr.external_url ? { number: n(pr.number), external_url: s(pr.external_url) } : null,
    build: s(x.build), href: s(x.href),
  };
};
/** The work item an attempt opens in the drawer: the group's row, seen as that one attempt. */
export function attemptAsItem(w: WorkItem, t: Attempt): WorkItem {
  return {
    ...w, id: t.id, label: t.label, run_number: null, created_at: t.created_at, state: t.state === "handed_on" ? "failed" : t.state, cost_usd: t.cost_usd, list_usd: t.list_usd ?? null,
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
export const KIND_WORDS: Record<string, string> = { feature: "Feature", bug: "Bug", fix: "Fix", ticket: "Ticket", chore: "Chore", incident: "Incident", setup: "Setup" };
export const ATTEMPT_WORDS: Record<AttemptState, string> = {
  planned: "Queued", running: "Running", stalled: "Stalled", handed_on: "Handed on", needs_you: "Needs you", done: "Done", failed: "Failed",
};
export const parseAccept = (v: unknown): Accept | null => {
  const x = o(v);
  return s(x.href) ? { href: s(x.href), method: s(x.method, "POST"), body: o(x.body) } : null;
};
const parseNext = (v: unknown): NextItem => {
  const x = o(v);
  return { id: s(x.id), label: s(x.label) || label4(s(x.title) || s(x.text)), detail: s(x.detail), source: s(x.source), kind: s(x.kind),
           accepted: x.accepted === true,
           state: x.state === "proposed" ? "proposed" : "next", href: s(x.href), external_url: s(x.external_url), accept: parseAccept(x.accept),
           text: s(x.text), ref: s(x.ref), why: s(x.why), rec_id: s(x.rec_id) };
};
const parseLogin = (v: unknown): DemoLogin => {
  const x = o(v); const r = o(x.reveal);
  return { user: s(x.user), label: s(x.label), reveal: r.href ? { href: s(r.href), requires: s(r.requires, "step_up") } : null };
};
export const parseCaptain = (v: unknown): Captain | null => {
  const x = o(v);
  if (!s(x.name) && !s(x.role_key)) return null;
  return { agent_id: s(x.agent_id), name: s(x.name) || s(x.role_key), role_key: s(x.role_key), avatar: s(x.avatar), status: s(x.status),
           squad: agents(x.squad), last_said: s(x.last_said), ranker: parseAgent(x.ranker) };
};

/** The captain drawer's read (§7 GET …/captain, screen 09): scope, facts known, what it and
 *  its crew did lately, and the suggestion chips grounded in the project's record. */
export type CaptainDetail = Captain & {
  scope: string; facts: number;
  recent: { act_id: string; role_key: string; label: string; state: StepState; at: string; href: string }[];
  starters: { label: string; message: string; icon: string }[];
};
export const parseCaptainDetail = (v: unknown): CaptainDetail | null => {
  const base = parseCaptain(v);
  if (!base) return null;
  const x = o(v);
  return { ...base, scope: s(o(x.scope).label), facts: n(x.facts),
           recent: a(x.recent).map((r) => { const y = o(r); return { act_id: s(y.act_id), role_key: s(y.role_key), label: s(y.label),
             state: oneOf(y.state, STEP_STATES, "todo"), at: s(y.at), href: s(y.href) }; }),
           starters: a(x.starters).map((r) => { const y = o(r); return { label: s(y.label), message: s(y.message), icon: s(y.icon) }; })
             .filter((st) => st.label && st.message) };
};

/** One piece of work, step by step (GET …/work/{id}/story, drawer 05b). */
export type WorkStory = {
  id: string; number: number | null; minutes: number | null; cost_usd: number | null;
  cost_parts: { label: string; usd: number }[]; preview: string; pr: { number: number; external_url: string } | null;
  stages: { key: string; name: string; state: StepState; sub: string; at: string; person: boolean;
            link: { label: string; href: string; external_url: string } | null }[];
};
export const parseWorkStory = (v: unknown): WorkStory => {
  const x = o(v); const pr = o(x.pr);
  return {
    id: s(x.id), number: nn(x.number), minutes: nn(x.minutes), cost_usd: nn(x.cost_usd),
    cost_parts: a(x.cost_parts).map((c) => ({ label: s(o(c).label), usd: n(o(c).usd) })).filter((c) => c.label),
    preview: s(x.preview), pr: s(pr.external_url) ? { number: n(pr.number), external_url: s(pr.external_url) } : null,
    stages: a(x.stages).map((r) => {
      const y = o(r); const l = o(y.link);
      return { key: s(y.key), name: s(y.name), state: oneOf(y.state, STEP_STATES, "todo"), sub: s(y.sub), at: s(y.at), person: y.person === true,
               link: s(l.label) ? { label: s(l.label), href: s(l.href), external_url: s(l.external_url) } : null };
    }),
  };
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
               health: oneOf(p.health ?? st.health, HEALTHS, "unknown"), health_reason: s(p.health_reason) },
    journey: a(x.journey).map(parseNode),
    roadmap_count: nn(x.roadmap_count),
    needs_you: a(x.needs_you).map((it) => {
      const y = o(it); const act = o(y.action);
      return { id: s(y.id), kind: oneOf(y.kind, ["merge", "deploy", "plan", "question", "setup"] as const, "question"), label: s(y.label),
               icon: s(y.icon), work_id: s(y.work_id), work_label: s(y.work_label), external_url: s(y.external_url), created_at: s(y.created_at),
               detail: s(y.detail), mini: mini(y.mini), release: parseRelease(y.release),
               diagnosis: parseDiagnosis(y.diagnosis),
               answers: parseAnswers(y.answers), entries: parseEntries(y.entries),
               action: act.label || act.href ? { label: s(act.label, "Open"), href: s(act.href), decision_id: s(act.decision_id) } : null };
    }),
    tiles: {
      open_work: Object.keys(ow).length ? { count: n(ow.count), by_state: byState, href: s(ow.href) } : null,
      running: Object.keys(rn).length ? { count: n(rn.count), href: s(rn.href),
        current: Object.keys(cur).length ? { label: s(cur.label), progress: [prog[0] ?? 0, prog[1] ?? 0], agent: parseAgent(cur.agent), sub: s(cur.sub), id: s(cur.id) } : null } : null,
      staging: Object.keys(st).length ? { health: oneOf(st.health, HEALTHS, "unknown"), uptime_pct: nn(st.uptime_pct), p95_ms: nn(st.p95_ms), p95_source: s(st.p95_source), label: s(st.label),
                                          health_reason: s(st.health_reason), window: s(st.window),
                                          series: nums(st.series), href: s(st.href) } : null,
      cost_week: Object.keys(cw).length ? { usd: n(cw.usd), delta_pct: nn(cw.delta_pct), cap_usd: nn(cw.cap_usd), series: nums(cw.series), href: s(cw.href),
                                            on_subscription: cw.on_subscription === true, list_usd: nn(cw.list_usd), list_series: nums(cw.list_series),
                                            list_delta_pct: nn(cw.list_delta_pct), first_week: cw.first_week === true, cap_pct: nn(cw.cap_pct) } : null,
    },
    captain: parseCaptain(x.captain),
    recent_work: a(x.recent_work).map(parseWorkItem).slice(0, 5),
    next: a(x.next).map(parseNext),
    backlog: a(x.backlog).map(parseNext),
    errors: Object.fromEntries(Object.entries(o(x.errors)).map(([k, v]) => [k, s(v, "Couldn't load")])),
    try_it: Object.keys(ti).length ? { url: s(ti.url), demo_logins: a(ti.demo_logins).map(parseLogin) } : null,
    // A week's event opens its record in the console (a PR's build, a deploy), else where it lives.
    week: a(x.week).map((w) => ({ at: s(o(w).at), label: label4(o(w).label), kind: s(o(w).kind), href: s(o(w).href) || s(o(w).external_url), detail: s(o(w).detail) })),
  };
};
export const parseWork = (v: unknown): WorkList => ({ items: a(o(v).items).map(parseWorkItem).filter((w) => w.id) });

const tri = (v: unknown): boolean | null => (v === true ? true : v === false ? false : null);
export const parseKnowledge = (v: unknown): Knowledge => {
  const x = o(v); const d = o(x.dossier); const g = o(x.graph); const c = o(g.counts);
  const cmds: Record<string, string> = {};
  // Install · Test · Build · Run, in the wireframe's order, then anything else the repo declares.
  const raw = o(d.commands);
  for (const k of ["install", "test", "build", "run", ...Object.keys(raw)]) {
    const val = raw[k];
    if (typeof val === "string" && val && !(k in cmds)) cmds[k] = val;
  }
  const tests = o(d.tests);
  const hasRun = typeof tests.total === "number" && tests.total > 0;
  const cm = o(d.code_map);
  const b = o(x.build); const em = o(x.empty);
  return {
    repos: a(x.repos).map((r) => {
      const y = o(r); const acc = o(y.access);
      return { ...parseRepo(r), binding_id: s(y.binding_id), checked: { read: tri(acc.read), write: tri(acc.write), pr: tri(acc.pr) },
               last_push: s(y.last_push), via: s(y.via), deploys_to: s(y.deploys_to),
               access_reasons: { read: s(o(y.access_reasons).read) || null, write: s(o(y.access_reasons).write) || null, pr: s(o(y.access_reasons).pr) || null },
               deploy_check: Object.keys(o(y.deploy_check)).length ? { label: s(o(y.deploy_check).label, "Staging"),
                 state: oneOf(o(y.deploy_check).state, STEP_STATES, "todo"), reason: s(o(y.deploy_check).reason) } : null };
    }),
    dossier: Object.keys(d).length ? {
      stack: a(d.stack).map((z) => s(z)).filter(Boolean), commands: cmds,
      hotspots: a(d.hotspots).map((h) => ({ path: s(o(h).path), label: s(o(h).label), why: s(o(h).why), heat: nn(o(h).heat),
                                             has_tests: typeof o(h).has_tests === "boolean" ? (o(h).has_tests as boolean) : null })),
      // A pass count only when a run was read (the newest build's tests); else the test files the dossier found.
      tests: hasRun ? { passed: n(tests.passed), total: n(tests.total), failing: n(tests.failing, n(tests.total) - n(tests.passed)),
                        from: s(tests.from), href: s(tests.href),
                        // BOSS could not run the suite (its environment): no pass rate to show.
                        env: tests.env ? { sentence: s(o(tests.env).sentence), reason: s(o(tests.env).reason), owner: s(o(tests.env).owner) } : null,
                        not_run: n(tests.not_run) } : null,
      test_files: nn(tests.files),
      // The tests' one statement ("40 test files · full suite on next build"), as Onboarding says it.
      tests_line: s(tests.line),
      code_map: { parts: a(cm.parts).map((z) => ({ dir: s(o(z).dir), files: n(o(z).files) })).filter((z) => z.files > 0), files: n(cm.files) },
      by: s(d.by, "Helm"), built_ago: s(d.built_ago), built_at: s(d.built_at), href: s(d.href) } : null,
    graph: { nodes: a(g.nodes).map((z) => ({ id: s(o(z).id), label: s(o(z).label), kind: s(o(z).kind), group: s(o(z).group), detail: s(o(z).detail) })),
             edges: a(g.edges).map((z) => ({ from: s(o(z).from), to: s(o(z).to), label: s(o(z).label), kind: s(o(z).kind) })), href: s(g.href), hub: s(g.hub),
             counts: { nodes: n(c.nodes, n(g.total_nodes, a(g.nodes).length)), rules: nn(c.rules), learned: nn(c.learned) } },
    // A doc opens in its drawer (`doc:<path>`); the drawer links to where it lives.
    docs: a(x.docs).map((z) => ({ label: s(o(z).label), kind: s(o(z).kind), href: s(o(z).href), external_url: s(o(z).external_url),
                                  sub: s(o(z).sub) || s(o(z).detail), path: s(o(z).detail) })),
    decisions: a(x.decisions).map((z) => ({ label: s(o(z).label), at: s(o(z).at), href: s(o(z).href), sub: s(o(z).sub), detail: s(o(z).detail) })),
    learned: a(x.learned).map((z) => {
      const y = o(z); const ag = o(y.agent);
      return { id: s(y.id), label: s(y.label), sub: s(y.sub), state: s(y.state), href: s(y.href), detail: s(y.detail), reused: n(y.reused),
               agent: s(ag.name) ? { name: s(ag.name), role_key: s(ag.role_key), avatar: "" } as Agent : null };
    }),
    build: Object.keys(b).length ? { label: s(b.label, "Build dossier now"), href: s(b.href), state: s(b.state), progress: s(b.progress),
                                     nodes: a(b.nodes).map((z) => ({ label: s(o(z).label), state: (s(o(z).state) || "todo") as StepState, sub: s(o(z).sub) })) } : null,
    empty: s(em.label) ? { label: s(em.label), detail: s(em.detail), href: s(em.href) } : null,
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
               host_name: s(y.host_name) || s(y.host), host_projects: nn(y.host_projects), p95_source: s(y.p95_source),
               window: s(y.window), health_reason: s(y.health_reason),
               uptime_pct: nn(y.uptime_pct), p95_ms: nn(y.p95_ms), error_rate: nn(y.error_rate), series: nums(y.series),
               hours: a(y.hours).map((h) => { const z = o(h); return { at: s(z.at), n: n(z.n), ok: n(z.ok),
                 state: oneOf(z.state, ["healthy", "degraded", "down", "none"] as const, "none") }; }),
               last_deploy: Object.keys(ld).length ? { commit: s(ld.commit), at: s(ld.at), by: s(ld.by), pr: nn(ld.pr), pr_url: s(ld.pr_url),
                                                       label: s(ld.label) || (nn(ld.pr) ? `PR #${nn(ld.pr)}` : s(ld.commit)), href: s(ld.href) } : null,
               demo_logins: a(y.demo_logins).map(parseLogin), policy: y.policy === "person" ? "person" : "auto", href: s(y.href) };
    }),
    production: { state: oneOf(pr.state, ["none", "needs_host", "ready"] as const, "none"),
                  action: act.label || act.href ? { label: s(act.label, "Set up"), href: s(act.href) } : null },
    ship_path: mini(x.ship_path),
    history: a(x.history).map((h) => {
      const y = o(h);
      // In words only: duration, smoke result or the reason it stopped — never a raw status or id.
      const sub = s(y.sub) || [s(y.duration), s(y.smoke), s(y.reason)].filter(Boolean).join(" · ");
      const recovered = y.recovered === true || y.state === "recovered";
      return { id: s(y.id), label: s(y.label), sub, at: s(y.at), state: recovered ? "failed" : deployState(y.status, y.state), href: s(y.href),
               word: s(y.state_word), by: s(y.by), by_agent: y.by_agent === true, recovered };
    }),
  };
};

export const parseOnboarding = (v: unknown): Onboarding => {
  const x = o(v); const fw = o(x.first_work);
  const item = (z: unknown): OnboardingItem => {
    const y = o(z); const act = o(y.action);
    return { key: s(y.key), label: label4(y.label), state: oneOf(y.state, STEP_STATES, "todo"), owner: y.owner === "you" ? "you" : "agent",
             action: act.label || act.href ? { label: s(act.label, "Do it"), href: s(act.href),
                                               ...(act.do === "staging_target" && s(act.post) ? { do: "staging_target" as const, host_id: s(act.host_id), post: s(act.post) } : {}) } : null,
             detail: s(y.detail), sub: s(y.sub) };
  };
  const checklist = a(x.checklist).map(item);
  const onlyYou = a(x.only_you).length ? a(x.only_you).map(item) : checklist.filter((c) => c.owner === "you" && c.state !== "done");
  const state = oneOf(x.state, ["not_started", "running", "ready", "needs_you", "failed"] as const, "running");
  // WHAT IT IS DOING NOW: the engine has no sentence of its own; the item in progress says it.
  const doing = checklist.find((c) => c.state === "now" && c.owner === "agent");
  const now = s(x.now) || (state === "running" && doing ? [doing.label, doing.sub || doing.detail].filter(Boolean).join(" · ") : "");
  return {
    state, stages: a(x.stages).map(parseNode),
    discover: { nodes: a(o(x.discover).nodes).map(parseNode), live: a(o(x.discover).live).map(parseNode) }, checklist, only_you: onlyYou,
    first_work: Object.keys(fw).length ? { label: s(fw.label), kind: s(fw.kind), source: s(fw.source), estimate_min: nn(fw.estimate_min),
                                           estimate_usd: nn(fw.estimate_usd), start: parseAccept(fw.start), others_href: s(fw.others_href),
                                           why: s(fw.why), why_first: s(fw.why_first), first_by: s(fw.first_by), workflow: s(fw.workflow),
                                           estimate_runs: n(o(fw.estimate_basis).minutes_runs), estimate_basis: s(o(fw.estimate_basis).minutes),
                                           external_url: s(fw.external_url) } : null,
    backlog: a(x.backlog).map((b) => ({ ...parseNext(b), accepted: s(o(b).state) === "accepted" })), now, elapsed_s: n(x.elapsed_s), error: s(x.error),
    now_agent: s(x.now_agent), now_live: typeof x.now_live === "boolean" ? x.now_live : state === "running",
    log: a(x.log).map((z) => { const y = o(z); return { at: s(y.at), agent: s(y.agent), label: s(y.label), state: oneOf(y.state, STEP_STATES, "done"), text: s(y.text) }; }),
    live: typeof x.live === "boolean" ? x.live : state === "running",
    title: s(x.title, state === "running" ? "Helm is reading your repo" : state === "not_started" ? "Helm hasn't started" : state === "failed" ? "Onboarding stopped" : "Helm read your repo"),
    ready: (a(x.ready).length ? a(x.ready) : a(x.checklist)).map((z) => ({ ...item(z), agent: s(o(z).agent) })),
    backlog_total: n(x.backlog_total, a(x.backlog).length), accepted: a(x.accepted).map((i) => s(i)),
    accept_backlog: parseAccept(x.accept_backlog),
    read_ago_s: nn(x.read_ago_s), runs: n(x.runs), trigger: s(x.trigger), started_at: s(x.started_at),
    tests: (() => {
      const t = o(o(x.facts).tests);
      return typeof t.total === "number" && t.total > 0 ? { passed: n(t.passed), total: n(t.total), failing: n(t.failing), source: s(t.label), cause: s(t.cause) } : null;
    })(),
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
      items, candidates: agents(y.candidates), raw: y,
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
               health_reason: s(y.health_reason), window: s(y.window), p95_source: s(y.p95_source),
               error_rate: nn(y.error_rate), p95_ms: nn(y.p95_ms), series: { errors: nums(ser.errors), latency: nums(ser.latency) },
               slo: { target: nn(slo.target), burn: oneOf(slo.burn, ["ok", "warning", "breach"] as const, "ok"), budget_used_pct: nn(slo.budget_used_pct) }, href: s(y.href) };
    }),
    alerts: a(x.alerts).map((z) => {
      const y = o(z);
      return { id: s(y.id), label: label4(y.label), condition: s(y.condition), severity: oneOf(y.severity, ["page", "ticket", "info"] as const, "info"),
               state: oneOf(y.state, ["firing", "resolved", "ok", "off"] as const, "firing"), since: s(y.since), quiet: s(y.quiet), href: s(y.href) };
    }),
    incidents: a(x.incidents).map((z) => {
      const y = o(z);
      return { id: s(y.id), label: label4(y.label), state: oneOf(y.state, ["running", "needs_you", "resolved"] as const, "running"),
               since: s(y.since), duration_s: nn(y.duration_s), outcome: s(y.outcome_word), graph_href: s(y.graph_href), closes_at: s(y.closes_at) || null,
               mini: a(y.mini).map((m) => ({ label: label4(o(m).label), state: oneOf(o(m).state, STEP_STATES, "todo"), type: s(o(m).type), at: s(o(m).at) || null })) };
    }),
    monitoring_pack: Object.keys(mp).length ? { rules: n(mp.rules), off: n(mp.off), managed_in: s(mp.managed_in), href: s(mp.href),
      kinds: a(mp.kinds).map((k) => s(k)).filter(Boolean),
      file: s(o(mp.file).path) ? { path: s(o(mp.file).path), external_url: s(o(mp.file).external_url) } : null } : null,
    links: { grafana: s(l.grafana), grafana_reason: s(l.grafana_reason), logs: s(l.logs), observability: s(l.observability) },
  };
};

// ── URLs ─────────────────────────────────────────────────────────────────────────────────────

const enc = encodeURIComponent;
export const PX_PATHS = {
  summary: () => "/v1/projects/summary",
  /** Screen 02 reads archived projects too (its Archived filter); the rail does not. */
  summaryAll: () => "/v1/projects/summary?include_archived=true",
  overview: (id: string) => `/v1/projects/${enc(id)}/overview`,
  work: (id: string, state = "", q = "") => `/v1/projects/${enc(id)}/work${state || q ? `?${new URLSearchParams({ ...(state ? { state } : {}), ...(q ? { q } : {}) })}` : ""}`,
  knowledge: (id: string) => `/v1/projects/${enc(id)}/knowledge`,
  deployments: (id: string) => `/v1/projects/${enc(id)}/deployments`,
  captain: (id: string) => `/v1/projects/${enc(id)}/captain`,
  /** Helm's plan for new work (screen 05): strip, estimates, the workflows "change" offers. */
  workPlan: (id: string, kind: string, workflowKey = "") => `/v1/projects/${enc(id)}/work/plan?${new URLSearchParams({ kind, ...(workflowKey ? { workflow_key: workflowKey } : {}) })}`,
  workStory: (id: string, work: string) => `/v1/projects/${enc(id)}/work/${enc(work)}/story`,
  onboarding: (id: string) => `/v1/projects/${enc(id)}/onboarding`,
  onboardingStart: (id: string) => `/v1/projects/${enc(id)}/onboarding/start`,
  backlogAccept: (id: string) => `/v1/projects/${enc(id)}/onboarding/backlog/accept`,
  plan: (id: string) => `/v1/projects/${enc(id)}/plan`,
  planMove: (id: string, item: string) => `/v1/projects/${enc(id)}/plan/${enc(item)}`,
  settings: (id: string) => `/v1/projects/${enc(id)}/settings`,
  health: (id: string) => `/v1/projects/${enc(id)}/health`,
  jump: (q: string, projectId?: string) => `/v1/jump?${new URLSearchParams({ q, ...(projectId ? { project_id: projectId } : {}) })}`,
  /** Existing (§6): the roadmap reads ambient's stored recommendations; works with the preview off. */
  roadmap: (id: string) => `/v1/ambient/roadmap?${new URLSearchParams({ project_id: id, status: "open", limit: "30" })}`,
  /** Existing: the project page's start view (repo, plan, work rows) — the fallback for Overview/Work. */
  start: (id: string) => `/v1/projects/${enc(id)}/start`,
} as const;

/** Console URL state for the project page (§10). */
/** `view`: a full-page view on the project page that is not a tab (`tab=onboarding`, screen 10). */
export type PxUrl = { tab: Tab; drawer: { kind: string; id: string } | null; firstRun: boolean; view: "onboarding" | null };
export function readPxUrl(search = typeof window === "undefined" ? "" : window.location.search): PxUrl {
  const p = new URLSearchParams(search);
  const raw = p.get("drawer") ?? "";
  const i = raw.indexOf(":");
  return { tab: toTab(p.get("tab")), firstRun: p.get("first_run") === "1", view: p.get("tab") === "onboarding" ? "onboarding" : null,
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
  if (accept.href === `/v1/projects/${enc(projectId)}/onboarding/backlog/accept`) {
    // A proposed candidate goes on Roadmap › Next (nothing starts).
    return acceptBacklog(projectId, a(accept.body.ids).map((i) => s(i)).filter(Boolean));
  }
  if (accept.href === `/v1/projects/${enc(projectId)}/work`) {
    const b = accept.body;
    return startWork(projectId, toWorkKind(b.kind), s(b.text) || s(b.title), {
      ...(s(b.title) ? { title: s(b.title) } : {}), ...(s(b.source_ref) ? { source_ref: s(b.source_ref) } : {}),
      ...(s(b.source_url) ? { source_url: s(b.source_url) } : {}) });
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
/** Screen 10 "Accept backlog" (after Edit): the chosen candidates, in this order, renamed where
 *  the person renamed them, go on the Roadmap's Next lane. Nothing starts. */
export const acceptBacklog = (projectId: string, ids: string[], titles: Record<string, string> = {}) =>
  pxWrite(PX_PATHS.backlogAccept(projectId), { ids, titles }, "Accept backlog");
/** Screen 07: move an accepted item to Next or Later, before another card (drag). */
export const movePlanItem = (projectId: string, itemId: string, lane: PlanLane, before?: string) =>
  pxWrite(PX_PATHS.planMove(projectId, itemId), { lane, ...(before ? { before } : {}) }, "Move");

// ── the Roadmap's plan (§6 GET /v1/projects/{id}/plan) ─────────────────────────────────────────
export type PlanLane = "next" | "later";
export type PlanCard = {
  id: string; label: string; title: string; state: WorkState | "planned"; agent: Agent | null; href: string;
  lane: "now" | PlanLane; source: string; external_url: string; start: Accept | null;
};
export type Plan = { now: PlanCard[]; next: PlanCard[]; later: PlanCard[]; counts: Record<string, number> };
const planCard = (lane: PlanCard["lane"]) => (z: unknown): PlanCard => {
  const y = o(z);
  return { id: s(y.id), label: s(y.label) || s(y.title), title: s(y.title) || s(y.label), lane,
           state: (s(y.state) || "planned") as PlanCard["state"], agent: parseAgent(y.agent), href: s(y.href),
           source: s(y.source), external_url: s(y.external_url), start: parseAccept(y.start) };
};
export const parsePlan = (v: unknown): Plan => {
  const x = o(v);
  const counts: Record<string, number> = {};
  for (const [k, c] of Object.entries(o(x.counts))) counts[k] = n(c);
  return { now: a(x.now).map(planCard("now")), next: a(x.next).map(planCard("next")), later: a(x.later).map(planCard("later")), counts };
};

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
    stalled_since: "", attempts: [], attempt_count: 1, followups: 0, retries: 0, run_number: null, created_at: "", updated_at: "", action_label: "",
    ...WORK_EXTRA,
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
export const snoozeRecommendation = async (projectId: string, id: string, days = 14, reason = "") =>
  (isPxDemo() ? demoOk() : fromAa(await aaSnooze(projectId, id, days, Date.now(), reason)));

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

// ── Helm's plan for new work (screen 05: GET /v1/projects/{id}/work/plan) ─────────────────────
export type PlanNode = { key: string; label: string; type: "agent" | "you" | "todo"; agent: Agent | null };
export type WorkflowOption = { key: string; name: string; detail: string; work_class: string; fits: boolean };
export type WorkPlan = {
  kind: WorkKind; planner: Agent | null;
  workflow: { key: string; name: string; chosen: "mapped" | "default" | "picked"; why: string; href: string } | null;
  nodes: PlanNode[];
  estimate: { minutes: number | null; minutes_basis: "history" | "typical" | null; minutes_runs: number;
              usd: number | null; usd_runs: number } | null;
  options: WorkflowOption[];
};
export const parseWorkPlan = (v: unknown): WorkPlan => {
  const x = o(v); const w = o(x.workflow); const e = o(x.estimate);
  return {
    kind: toWorkKind(x.kind), planner: parseAgent(x.planner),
    workflow: s(w.key) ? { key: s(w.key), name: s(w.name, s(w.key)), chosen: oneOf(w.chosen, ["mapped", "default", "picked"] as const, "default"),
                           why: s(w.why), href: s(w.href) } : null,
    nodes: a(x.nodes).map((z) => ({ key: s(o(z).key), label: s(o(z).label), type: oneOf(o(z).type, ["agent", "you", "todo"] as const, "todo"),
                                    agent: parseAgent(o(z).agent) })).filter((z) => z.label),
    estimate: x.estimate ? { minutes: nn(e.minutes), minutes_basis: e.minutes_basis === "history" || e.minutes_basis === "typical" ? e.minutes_basis : null,
                             minutes_runs: n(e.minutes_runs), usd: nn(e.usd), usd_runs: n(e.usd_runs) } : null,
    options: a(x.options).map((z) => ({ key: s(o(z).key), name: s(o(z).name), detail: s(o(z).detail), work_class: s(o(z).work_class),
                                        fits: o(z).fits === true })).filter((z) => z.key),
  };
};
/** "≈ $3" for a run of a few dollars, "≈ $0.90" under one. */
export const dollarsAbout = (usd: number): string => (usd < 1 ? `$${usd.toFixed(2)}` : `$${Math.round(usd)}`);

// ── Needs you: ONE source for the header bar and the Work tab ─────────────────────────────────
/** What waits on a person, exactly as the header bar lists it: the overview's cards, with the
 *  live onboarding record's "only you" items in place of the overview's copies while the project
 *  onboards (never both). The Work tab's Needs-you count is this list's length. */
export function needsYouCards(needs: NeedsYou[], onboarding: Onboarding | null, work: WorkItem[] = []): NeedsYou[] {
  const yours = onboarding?.only_you ?? [];
  const cards = yours.length ? needs.filter((it) => !it.id.startsWith("onb:")) : needs;
  const onbRow = needs.find((it) => it.id.startsWith("onb:"))?.work_id || work.find((w) => w.kind === "setup" && w.source === "onboarding")?.id;
  const out: NeedsYou[] = [...cards, ...yours.map((it): NeedsYou => ({
    id: `onb:${it.key}`, kind: "setup", label: it.label, detail: it.detail, mini: [], work_id: onbRow,
    action: it.action ? { label: it.action.label, href: it.action.href, decision_id: "" } : null,
  }))];
  // A piece of work waiting on a person always has its card (the engine writes one; an older
  // engine's list is completed here), so the bar and the Work tab never disagree.
  const carded = new Set(out.map((c) => c.work_id).filter(Boolean));
  const decided = new Set(out.map((c) => c.action?.decision_id).filter(Boolean));
  for (const w of work) {
    if (w.state !== "needs_you" || carded.has(w.id) || (w.kind === "setup" && w.source === "onboarding")) continue;
    if (w.links.decision && [...decided].some((d) => w.links.decision.includes(String(d)))) continue;
    if (!w.links.decision && !w.action_label) continue;
    out.push({ id: `work:${w.id}`, kind: w.action_label === "Merge" ? "merge" : w.action_label === "Approve deploy" ? "deploy" : w.action_label === "Approve plan" ? "plan" : "question",
               label: `${w.action_label || "Decide"}: ${w.label}`, detail: w.detail, mini: [], work_id: w.id,
               action: { label: w.action_label || "Decide", href: w.links.decision || w.href, decision_id: "" } });
  }
  return out;
}

// ── drag to re-rank Planned (05-23 / 05-33) ─────────────────────────────────────────────────────
/** Planned work moved to `to` (its index in the new order): the new order, and the move the engine
 *  is sent (plan.move: the lane of the item it lands before, else the lane of the one it follows). */
export function reRank(planned: WorkItem[], id: string, to: number): { order: WorkItem[]; lane: PlanLane; before?: string } | null {
  const from = planned.findIndex((w) => w.id === id);
  if (from < 0) return null;
  const rest = planned.filter((w) => w.id !== id);
  const at = Math.max(0, Math.min(to, rest.length));
  if (at === from) return null;
  const order = [...rest.slice(0, at), planned[from], ...rest.slice(at)];
  const after: WorkItem | undefined = rest[at];
  const prev: WorkItem | undefined = rest[at - 1];
  // Between the last of Next and the first of Later it stays in Next, at its end (Next is what
  // starts first); anywhere else it joins the lane of the item it lands before (or follows, last).
  const boundary = Boolean(prev && after && prev.lane !== "later" && after.lane === "later");
  const lane: PlanLane = boundary ? "next" : ((after ?? prev)?.lane || "next") as PlanLane;
  const before = boundary ? undefined : after?.id;
  return { order: order.map((w) => (w.id === id ? { ...w, lane } : w)), lane, ...(before ? { before } : {}) };
}

/**
 * ONE WAY TO SAY WHAT WORK COST, everywhere (Overview tile, Recent work, Work rows, the work
 * drawer, Projects cards): money as "$2.10"; work that ran on a subscription as "≈ $2.10" at
 * list price with the plan marker ("On subscription · ≈ $2.10 at list price"), never a bare
 * "$0.00". Null when there is nothing to say (no spend and no list price).
 */
export function costWords(usd: number | null | undefined, list?: number | null): { text: string; title: string; plan: boolean } | null {
  const money = typeof usd === "number" && usd > 0 ? usd : 0;
  const listed = typeof list === "number" && list > 0 ? list : 0;
  if (money > 0) {
    return { text: `$${money.toFixed(2)}`, plan: false,
             title: listed ? `$${money.toFixed(2)} billed · plus ≈ $${listed.toFixed(2)} on your subscription at list price` : `$${money.toFixed(2)} billed` };
  }
  if (listed > 0) return { text: `≈ $${listed.toFixed(2)}`, plan: true, title: `On subscription · ≈ $${listed.toFixed(2)} at list price (no money spent)` };
  return null;
}

/** A journey node as the viewer reads it (04-02): a node with `at` says its day in the viewer's
 *  own zone ("Nov 2"), as its sub and in its tooltip. `day` is lib/local-time's formatter, "" until
 *  the page has mounted (then the sub is the server's own, empty for a dated node). */
export function nodeWords(n: Node, day: (ms: number) => string): { sub: string; detail: string } {
  const ms = n.at ? Date.parse(n.at) : NaN;
  const said = Number.isFinite(ms) ? day(ms) : "";
  return { sub: n.at ? said || n.sub : n.sub, detail: (n.detail ?? "").replace("{day}", said).replace(/\s{2,}/g, " ").trim() };
}

/** WHAT WAITS ON YOU ACROSS EVERY PROJECT (X-14): the Projects page's "Need you" tile — each
 *  project's own count (its Needs-you bar: decisions + what only a person can set up), summed over
 *  the projects the viewer can open. The bell, the rail's Approvals badge and the tab title read
 *  this one number; null until the summary has answered (they fall back to the project's own). */
export function orgNeedsOf(summary: ProjectsSummary | null): number | null {
  if (!summary) return null;
  return summary.items.filter((c) => !c.archived).reduce((t, c) => t + (c.needs_you || 0), 0);
}

export function useOrgNeeds(): number | null {
  const read = usePxRead<ProjectsSummary>(PX_PATHS.summary(), parseSummary, { everyMs: 30_000 });
  return read.state === "ready" ? orgNeedsOf(read.data) : null;
}

