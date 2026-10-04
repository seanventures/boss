/**
 * Agents › Live: the frozen contract (agents-home spec §6.2-§6.4, Phase 0).
 *
 * Three streams build against these shapes at once — the engine (S1), Live and the pill (S2),
 * and Organization / Directory / Activity (S3) — so a shape changed here is changed under all
 * three: change it with the lead, never inside a stream. The wire is snake_case; the parsers
 * below are the only place it is mapped, and they never invent a field the server did not send
 * (no percent, no ETA, no guessed face: `roleKey: null` stays null).
 */

import type { AvatarState } from "./agent-world-types";
import { useCallback, useEffect, useRef, useState } from "react";

import { agentIdentity } from "./agent-identity";
import { isConsoleLive, subscribeConsoleLive } from "./console-live";
import { onChanged } from "../app/changed";

// ── Shared ──────────────────────────────────────────────────────────────────────────────────

export type TeamKey =
  | "leadership" | "architecture" | "engineering" | "quality" | "operations" | "cost" | "media"
  | "thinkers" | "yours" | (string & {});

/** The shipped teams, in the order every surface draws them (spec §4.6). */
export const TEAM_ORDER: readonly string[] = [
  "leadership", "architecture", "engineering", "quality", "operations", "cost", "media", "thinkers", "yours",
];

/** Labels and tooltips for the shipped teams — the same words agent_identity.TEAMS sends. */
export const TEAM_LABELS: Readonly<Record<string, { label: string; tip?: string }>> = {
  leadership: { label: "Leadership" }, architecture: { label: "Architecture" },
  engineering: { label: "Engineering" }, quality: { label: "Quality", tip: "Quality and security" },
  operations: { label: "Operations" }, cost: { label: "Cost", tip: "Cost and providers" },
  media: { label: "Media" },
  thinkers: { label: "Thinkers", tip: "Inspired by a way of thinking. Not real people." },
  yours: { label: "Yours" },
};

/** Which team an agent sits in, from the server's resolver (identities carry `team`). For a
 *  grouping that has no live board to hand — the Skills "Who can" matrix, Directory. */
export function teamOfAgent(roleKey: string): { key: string; label: string; tip?: string } {
  const key = agentIdentity(roleKey).team || "yours";
  const known = TEAM_LABELS[key];
  return known ? { key, ...known } : { key, label: key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) };
}

// ── Attention (the cards, the pill) ─────────────────────────────────────────────────────────

/** `gate_override` / `budget_refused` are a build stopped for you (factory spec §6.5). */
export type AttentionKind = "approval" | "failed_step" | "readiness" | "gate_override" | "budget_refused";
export type DecisionKindWire = "workflow_gate" | "deploy_gate" | "pull_request" | "refusal" | "general";

export type AttentionAction = {
  action: "approve" | "reject" | "revise" | "delegate" | "execute";
  label: string; tone: string; effect: string; needs: string[];
};
export type AttentionAnswer = { key: string; label: string; enabled: boolean; reason?: string; stepId?: string };

export type AttentionItem = {
  /** decision id | `run:<run>:<step>` (failed step) | `ready:<key>` (readiness). */
  id: string;
  kind: AttentionKind;
  decisionKind?: DecisionKindWire;
  decisionId?: string;
  title: string;
  detail: string;
  whatToDo?: string;
  /** The attributed agent, or null — never guessed (spec §1.3). */
  roleKey: string | null;
  run?: { id: string; subject: string; stepId?: string; stepLabel?: string; stepIndex?: number; stepCount?: number };
  actions: AttentionAction[];
  answers: AttentionAnswer[];
  /** Server policy (§4.2): the card may approve/reject in place. */
  decideInline: boolean;
  contentHash?: string;
  preview?: string;
  riskTier?: number;
  fix?: { area: string; label: string };
  at?: string;
  /** The build this is about, when its subject is a code task (factory spec §4.2.1, §6.5). */
  build?: AttentionBuild;
};

/** A failing quality gate, as a waiver sheet lists it. `waivable: false` → disabled + reason. */
export type FailingGate = { name: string; label: string; waivable: boolean; reason?: string };
export type AttentionBuild = {
  runId: string;
  app: string;
  /** Where it stopped, as the context line says it: `Plan`, `Review · round 2`, `before Build`, `PR open`. */
  where: string;
  failing: FailingGate[];
  retryable: boolean;
  retryReason?: string;
  prUrl?: string;
  previewUrl?: string;
  planUrl?: string;
};

/** The pill's slice of attention, on `/v1/agents/live` (§6.3). */
export type AttentionSummary = {
  count: number;
  items: { id: string; roleKey: string | null; title: string; at?: string; decideInline: boolean }[];
};

// ── Missions (Live work rows) ───────────────────────────────────────────────────────────────

export type MissionState = "running" | "waiting_you" | "waiting_person" | "waiting" | "stopped" | "blocked" | "done";

export type MissionHop =
  | { kind: "hop"; actId: string; roleKey: string | null; person?: string; state: AvatarState;
      label: string; edgeLabel: string; inside?: number; how?: string }
  | { kind: "loop"; a: string; b: string; rounds: number }
  | { kind: "fold"; hidden: number };

export type Mission = {
  runId: string; subject: string; workflowKey: string; workflowName: string;
  version?: number; actor?: string;
  state: MissionState; standing: string;
  stepIndex: number; stepCount: number; stepLabel?: string;
  verb: string;
  waitingOn?: string | null; waitingOnRole?: string | null; blockedReason?: string | null; error?: string | null;
  attentionId?: string | null;
  startedAt?: string; stepSince?: string; elapsedSeconds?: number; stepElapsedSeconds?: number;
  /** Metered model spend so far; 0 when unmetered (and then never shown). */
  costUsd: number;
  hops: MissionHop[];
  /** True when the server did not read this run's chain (past the per-request cap). */
  folded?: boolean;
  next: { roleKey: string | null; stepLabel: string; named: boolean }[];
  finishedAt?: string | null;
};

// ── Outcomes, teams, agents ─────────────────────────────────────────────────────────────────

export type Outcome = {
  actId?: string; roleKey: string | null; kind: "pr" | "pr_merged" | "report" | "minutes" | "run";
  text: string; url?: string; runId?: string; label?: string; at: string;
};

export type TeamSummary = {
  key: TeamKey; label: string; tip?: string; members: string[];
  working: number; needsYou: number; inPanel: number;
};

export type BoardAgent = {
  roleKey: string; name: string; callsign?: string; team: TeamKey;
  state: AvatarState | "in_panel"; line: string; since?: string;
};

export type LiveBoardCounts = { agents: number; working: number; needsYou: number; running: number; stopped: number };

export type LiveBoard = {
  projectId: string; etag: string; generatedAt: string;
  counts: LiveBoardCounts;
  attention: AttentionItem[];
  missions: Mission[];
  outcomes: Outcome[];
  teams: TeamSummary[];
  agents: BoardAgent[];
};

// ── Activity (§4.9, §6.4) ───────────────────────────────────────────────────────────────────

export type ActivityItem = {
  actId: string; roleKey: string | null; kind: string; status: string;
  how: string; edgeLabel: string; label: string; verb: string;
  parentActId?: string; rootActId?: string;
  workflowRunId?: string;
  from?: { kind: string; id?: string; roleKey?: string };
  children: number;
  toolCalls: number;
  costUsd: number;
  startedAt: string; endedAt?: string;
};
export type ActivityPage = { items: ActivityItem[]; next: string | null };
export type ToolCallRow = { toolName: string; ok: boolean | null; latencyMs: number | null; at?: string };
export type ToolCalls = { items: ToolCallRow[]; more: boolean };
export type ActivityFilter = "all" | "needs_you" | "handoffs" | "builds" | "panels" | "talks";

// ── Parsers (the only place the wire is mapped) ─────────────────────────────────────────────

type Wire = Record<string, unknown>;
const obj = (v: unknown): Wire => (v && typeof v === "object" && !Array.isArray(v) ? v as Wire : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const opt = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);
const num = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const optNum = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const roleOf = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

const AVATAR_STATES = new Set(["idle", "thinking", "speaking", "working", "needs_you", "done", "stuck", "paused", "retired"]);
const avatarState = (v: unknown): AvatarState => (AVATAR_STATES.has(str(v)) ? str(v) as AvatarState : "idle");

export function parseAttention(w: unknown): AttentionItem {
  const o = obj(w);
  const run = o.run ? obj(o.run) : null;
  const fix = o.fix ? obj(o.fix) : null;
  return {
    id: str(o.id), kind: str(o.kind, "approval") as AttentionKind,
    decisionKind: opt(o.decision_kind) as DecisionKindWire | undefined,
    decisionId: opt(o.decision_id),
    title: str(o.title), detail: str(o.detail), whatToDo: opt(o.what_to_do),
    roleKey: roleOf(o.role_key),
    run: run ? {
      id: str(run.id), subject: str(run.subject), stepId: opt(run.step_id), stepLabel: opt(run.step_label),
      stepIndex: optNum(run.step_index), stepCount: optNum(run.step_count),
    } : undefined,
    actions: arr(o.actions).map((a) => {
      const x = obj(a);
      const needs = Array.isArray(x.needs) ? x.needs.map(String) : x.needs ? [String(x.needs)] : [];
      return { action: str(x.action) as AttentionAction["action"], label: str(x.label), tone: str(x.tone),
               effect: str(x.effect), needs };
    }),
    answers: arr(o.answers).map((a) => {
      const x = obj(a);
      return { key: str(x.key), label: str(x.label), enabled: x.enabled !== false, reason: opt(x.reason),
               stepId: opt(x.step_id) };
    }),
    decideInline: o.decide_inline === true,
    contentHash: opt(o.content_hash), preview: opt(o.preview),
    riskTier: optNum(o.risk_tier),
    fix: fix ? { area: str(fix.area), label: str(fix.label) } : undefined,
    at: opt(o.at),
    build: o.build ? parseAttentionBuild(o.build) : undefined,
  };
}

export function parseAttentionBuild(w: unknown): AttentionBuild {
  const b = obj(w);
  return {
    runId: str(b.run_id), app: str(b.app), where: str(b.where),
    failing: arr(b.failing).map((f) => {
      const x = obj(f);
      return { name: str(x.name), label: str(x.label), waivable: x.waivable !== false, reason: opt(x.reason) };
    }),
    retryable: b.retryable === true, retryReason: opt(b.retry_reason),
    prUrl: opt(b.pr_url), previewUrl: opt(b.preview_url), planUrl: opt(b.plan_url),
  };
}

export function parseAttentionSummary(w: unknown): AttentionSummary {
  const o = obj(w);
  return {
    count: num(o.count),
    items: arr(o.items).map((i) => {
      const x = obj(i);
      return { id: str(x.id), roleKey: roleOf(x.role_key), title: str(x.title), at: opt(x.at),
               decideInline: x.decide_inline === true };
    }),
  };
}

function parseHop(w: unknown): MissionHop {
  const o = obj(w);
  if (o.loop) {
    const l = obj(o.loop);
    return { kind: "loop", a: str(l.a), b: str(l.b), rounds: num(l.rounds) };
  }
  if (o.fold) return { kind: "fold", hidden: num(obj(o.fold).hidden ?? o.fold) };
  return {
    kind: "hop", actId: str(o.act_id), roleKey: roleOf(o.role_key), person: opt(o.person),
    state: avatarState(o.state), label: str(o.label), edgeLabel: str(o.edge_label),
    inside: optNum(o.inside), how: opt(o.how),
  };
}

export function parseMission(w: unknown): Mission {
  const o = obj(w);
  return {
    runId: str(o.run_id), subject: str(o.subject), workflowKey: str(o.workflow_key),
    workflowName: str(o.workflow_name), version: optNum(o.version), actor: opt(o.actor),
    state: str(o.state, "running") as MissionState, standing: str(o.standing, "active"),
    stepIndex: num(o.step_index), stepCount: num(o.step_count), stepLabel: opt(o.step_label),
    verb: str(o.verb, "Working"),
    waitingOn: opt(o.waiting_on) ?? null, waitingOnRole: opt(o.waiting_on_role) ?? null,
    blockedReason: opt(o.blocked_reason) ?? null, error: opt(o.error) ?? null,
    attentionId: opt(o.attention_id) ?? null,
    startedAt: opt(o.started_at), stepSince: opt(o.step_since),
    elapsedSeconds: optNum(o.elapsed_seconds), stepElapsedSeconds: optNum(o.step_elapsed_seconds),
    costUsd: num(o.cost_usd),
    // A gate's own act comes as a hop flagged `gate` (it is real work: the wait). The lane draws
    // it once, as the amber You / A person hop from the row's state, so it is not a face here.
    hops: arr(o.hops).filter((h) => obj(h).gate !== true).map(parseHop),
    folded: o.folded === true || undefined,
    next: arr(o.next).map((n) => {
      const x = obj(n);
      return { roleKey: roleOf(x.role_key), stepLabel: str(x.step_label), named: x.named === true };
    }),
    finishedAt: opt(o.finished_at) ?? null,
  };
}

export function parseLiveBoard(w: unknown): LiveBoard {
  const o = obj(w);
  const c = obj(o.counts);
  return {
    projectId: str(o.project_id), etag: str(o.etag), generatedAt: str(o.generated_at),
    counts: { agents: num(c.agents), working: num(c.working), needsYou: num(c.needs_you),
              running: num(c.running), stopped: num(c.stopped) },
    attention: arr(o.attention).map(parseAttention),
    missions: arr(o.missions).map(parseMission),
    outcomes: arr(o.outcomes).map((x) => {
      const y = obj(x);
      return { actId: opt(y.act_id), roleKey: roleOf(y.role_key), kind: str(y.kind, "run") as Outcome["kind"],
               text: str(y.text), url: opt(y.url), runId: opt(y.run_id), label: opt(y.label), at: str(y.at) };
    }),
    teams: arr(o.teams).map((t) => {
      const y = obj(t);
      return { key: str(y.key, "yours"), label: str(y.label), tip: opt(y.tip), members: arr(y.members).map(String),
               working: num(y.working), needsYou: num(y.needs_you), inPanel: num(y.in_panel) };
    }),
    agents: arr(o.agents).map((a) => {
      const y = obj(a);
      const state = str(y.state, "idle");
      return { roleKey: str(y.role_key), name: str(y.name), callsign: opt(y.callsign), team: str(y.team, "yours"),
               state: state === "in_panel" ? "in_panel" : avatarState(state), line: str(y.line), since: opt(y.since) };
    }),
  };
}

export function parseActivity(w: unknown): ActivityPage {
  const o = obj(w);
  return {
    items: arr(o.items).map((i) => {
      const x = obj(i);
      const from = x.from ? obj(x.from) : null;
      const links = obj(x.links);
      return {
        actId: str(x.act_id), roleKey: roleOf(x.role_key), kind: str(x.kind), status: str(x.status),
        how: str(x.how), edgeLabel: str(x.edge_label), label: str(x.label), verb: str(x.verb, "Working"),
        parentActId: opt(x.parent_act_id), rootActId: opt(x.root_act_id),
        workflowRunId: opt(links.workflow_run_id ?? x.workflow_run_id),
        from: from ? { kind: str(from.kind), id: opt(from.id), roleKey: opt(from.role_key) } : undefined,
        children: num(obj(x.children).count ?? x.children),
        toolCalls: num(obj(x.turns).tool_calls),
        costUsd: num(x.cost_usd),
        startedAt: str(x.started_at), endedAt: opt(x.ended_at),
      };
    }),
    next: opt(o.next) ?? null,
  };
}

export function parseToolCalls(w: unknown): ToolCalls {
  const o = obj(w);
  return {
    items: arr(o.items).map((i) => {
      const x = obj(i);
      return { toolName: str(x.tool_name), ok: typeof x.ok === "boolean" ? x.ok : null,
               latencyMs: optNum(x.latency_ms) ?? null, at: opt(x.at) };
    }),
    more: o.more === true,
  };
}

// ── The poll (S2): one per mounted Agents area ──────────────────────────────────────────────

export const BOARD_POLL_MS = 8000;
const BOARD_MISSING_MS = 60_000;

export type LiveBoardState = {
  board: LiveBoard | null;
  /** The last failure's reason, while the board could not be read. Cleared by an answer. */
  error: string | null;
  /** True until the first answer (or failure). */
  loading: boolean;
  refresh: () => void;
};

/**
 * `/v1/agents/live-board`, every 8s while the page is visible, the console is Live and
 * `enabled` (the Live or Organization tab is open). If-None-Match makes an unchanged answer a
 * 304 and no re-render; elapsed times tick locally from `step_since`, so they are not in the
 * ETag. A change made anywhere in the console (`onChanged`) and pressing Live again ask now.
 */
export function useLiveBoard(projectId: string, enabled = true): LiveBoardState {
  const [board, setBoard] = useState<LiveBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const etag = useRef<string | null>(null);
  const kick = useRef<() => void>(() => {});

  useEffect(() => {
    if (!projectId || !enabled) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inflight = false;
    let gen = 0;
    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";

    const tick = async (mine: number) => {
      if (stopped || mine !== gen) return;
      let wait = BOARD_POLL_MS;
      if (!hidden() && isConsoleLive() && !inflight) {
        inflight = true;
        try {
          const r = await fetch(`/v1/agents/live-board?project_id=${encodeURIComponent(projectId)}`, {
            cache: "no-store", headers: etag.current ? { "If-None-Match": etag.current } : {},
          });
          if (stopped) return;
          if (r.status === 304) {
            setError(null);
          } else if (r.ok) {
            etag.current = r.headers.get("etag");
            setBoard(parseLiveBoard(await r.json()));
            setError(null);
          } else {
            if (r.status === 404 || r.status === 405) wait = BOARD_MISSING_MS;
            let said = "";
            try { said = String(((await r.json()) as { detail?: unknown }).detail ?? ""); } catch { /* none */ }
            setError(said || `The API answered ${r.status}.`);
          }
        } catch (e) {
          if (!stopped) setError(e instanceof Error ? e.message : "The API did not answer.");
        } finally {
          inflight = false;
          if (!stopped) setLoading(false);
        }
      }
      if (!stopped && mine === gen) timer = setTimeout(() => void tick(mine), wait);
    };
    const restart = () => {
      if (stopped || hidden() || !isConsoleLive()) return;
      if (timer) clearTimeout(timer);
      gen += 1;
      void tick(gen);
    };
    kick.current = restart;
    const onVis = () => restart();
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVis);
    const offChanged = onChanged(() => restart());
    const offPause = subscribeConsoleLive((on) => { if (on) restart(); });
    void tick(gen);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVis);
      offChanged();
      offPause();
    };
  }, [projectId, enabled]);

  const refresh = useCallback(() => kick.current(), []);
  return { board, error, loading: loading && !board && !error, refresh };
}
