/**
 * Factory › a build's cockpit: the frozen contract (factory spec §4.5, §6.3; Phase 0).
 *
 * engine/boss_engine/services/factory_view.py holds the same shapes. Every collapsed value an
 * Outputs row shows (`word`) and every state word is the server's; the web renders them and
 * maps none of its own. No percent, no ETA, no test counts (only commands are real), no hunks.
 */

import { parseAttention, type AttentionItem } from "./agents-live-board";
import {
  arr, num, obj, opt, parseOrigin, parseStation, roleOf, str, usePolled,
  type BuildStateKey, type Origin, type Polled, type Station, type StationKey,
} from "./factory-floor";

export type CockpitState = {
  state: BuildStateKey; word: string; tip?: string; verb: string;
  station: StationKey | null; k: number; n: number; rounds: number;
  preview: "starting" | "running" | "failed" | null;
};

export type CockpitRun = {
  id: string; title: string; status: string; phase: string;
  createdAt?: string; updatedAt?: string;
  prUrl?: string; prNumber?: number; decisionId?: string; error?: string;
  attempt: number; rootRunId: string;
};

export type LineageItem = { runId: string; attempt: number; state: BuildStateKey; word: string; at?: string };

export type StoryAct = {
  type: "act"; id: string; station: StationKey | null;
  actId: string; roleKey: string | null; status: string; verb: string; label: string;
  startedAt?: string; endedAt?: string; costUsd: number;
  title: string; seat: number | null; round: number;
  /** `Done` · `Working` · `Stuck` · or the verdict word (`Approved` · `Changes asked` · `Blocked`). */
  word: string; verdict: string | null;
  files: string[] | null; toolCalls: number; commentsAuthored: number; commentsFixed: number;
  said: string | null;
  failure: { line: string; tip: string } | null;
};
export type StoryYou = {
  type: "you"; id: string; station: StationKey | null;
  what: "steered" | "approved_plan" | "waived" | "retried" | "stopped" | string;
  actor: string; text?: string; at: string;
  appliedAt?: string; appliedActId?: string; appliedRole: string | null; count?: number;
  /** The server's word for a steer: `Queued for next station` · `Read by <name>` ·
   *  `Not read · build ended`. Absent from an older API (then derived from appliedAt). */
  word?: string;
};
export type StoryBoss = {
  type: "boss"; id: string; station: StationKey | null;
  what: "cloned" | "ran_tests" | "opened_pr" | "started_preview" | "deployed" | string;
  text: string; at: string; ms?: number; detail?: Record<string, unknown>;
};
export type StoryRow = StoryAct | StoryYou | StoryBoss;

export type NowLine = { actId: string; roleKey: string | null; tool: string; arg?: string; at: string };

export type PrOutput = {
  number?: number; url?: string; filesUrl?: string; decisionId?: string; decisionStatus?: string;
  madeBy: number; word: string; roleKey: string | null;
};
export type Gate = {
  name: string; label: string; ok: boolean; waivedBy?: string; roleKey: string | null;
  station: StationKey | null; evidence?: string; waivable: boolean; reason?: string;
};
export type ChecksOutput = { word: string; tip?: string; advisory: boolean; gates: Gate[] };
export type TestCommand = { command: string; exitCode: number | null; ok: boolean; timedOut: boolean };
export type TestsOutput = { word: string; roleKey: string | null; commands: TestCommand[]; outputTail?: string };
export type ChangedFile = { path: string; bytes?: number };
export type ChangesOutput = {
  word: string; tests: ChangedFile[]; code: ChangedFile[];
  testsRoleKey: string | null; codeRoleKey: string | null; diffUrl?: string;
};
export type ReviewComment = {
  id: string; round: number; severity: "blocker" | "major" | "minor" | "nit" | string;
  body: string; path?: string; line?: number; endLine?: number;
  author?: string; authorRoleKey: string | null; status: "open" | "addressed" | "dismissed" | string;
};
export type CommentsOutput = { word: string; items: ReviewComment[] };
export type PreviewOutput = {
  id: string; status: string; word: string; url?: string; expiresAt?: string;
  stepsDone?: number; stepsTotal?: number; error?: string;
};
export type DeployOutput = { word: string; items: Record<string, unknown>[] };
export type CostOutput = {
  word: string; meteredUsd: number; subscriptionCalls: number;
  byAgent: { roleKey: string | null; turns: number; tokens: number; usd: number }[];
  byStation: Partial<Record<StationKey, number>>;
};

export type CockpitOutputs = {
  mergeReady: { ready: boolean; word: string };
  pr: PrOutput | null;
  checks: ChecksOutput | null;
  tests: TestsOutput | null;
  changes: ChangesOutput | null;
  comments: CommentsOutput;
  preview: PreviewOutput | null;
  deploy: DeployOutput;
  cost: CostOutput;
};

export type Cockpit = {
  etag: string;
  run: CockpitRun;
  app: string;
  state: CockpitState;
  startedAt?: string; stationSince?: string;
  costUsd: number; metered: boolean;
  /** Poll at 4s while true, 30s otherwise. */
  live: boolean;
  stoppable: boolean;
  retryable: boolean; retryReason?: string;
  failure: { line: string; tip: string } | null;
  lineage: LineageItem[];
  origin: Origin | null;
  agents: { roleKey: string; state: string }[];
  stations: Station[];
  attention: AttentionItem[];
  story: StoryRow[];
  now: NowLine | null;
  outputs: CockpitOutputs;
};

// ── Parsers ─────────────────────────────────────────────────────────────────────────────────

const optNum = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const station = (v: unknown): StationKey | null => (opt(v) as StationKey | undefined) ?? null;
const failureOf = (v: unknown): { line: string; tip: string } | null => {
  const o = obj(v);
  return opt(o.line) ? { line: str(o.line), tip: str(o.tip) } : null;
};

export function parseStory(w: unknown): StoryRow {
  const o = obj(w);
  const type = str(o.type);
  if (type === "you") {
    return { type: "you", id: str(o.id), station: station(o.station), what: str(o.what), actor: str(o.actor),
             text: opt(o.text), at: str(o.at), appliedAt: opt(o.applied_at), appliedActId: opt(o.applied_act_id),
             appliedRole: roleOf(o.applied_role), count: optNum(o.count), word: opt(o.word) };
  }
  if (type === "boss") {
    return { type: "boss", id: str(o.id), station: station(o.station), what: str(o.what), text: str(o.text),
             at: str(o.at), ms: optNum(o.ms), detail: o.detail ? obj(o.detail) : undefined };
  }
  const a = obj(o.act);
  return {
    type: "act", id: str(o.id, str(a.act_id)), station: station(o.station),
    actId: str(a.act_id), roleKey: roleOf(a.role_key), status: str(a.status), verb: str(a.verb, "Working"),
    label: str(a.label), startedAt: opt(a.started_at), endedAt: opt(a.ended_at), costUsd: num(a.cost_usd),
    title: str(o.title), seat: optNum(o.seat) ?? null, round: num(o.round, 1),
    word: str(o.word), verdict: opt(o.verdict) ?? null,
    files: Array.isArray(o.files) ? o.files.map(String) : null,
    toolCalls: num(o.tool_calls), commentsAuthored: num(o.comments_authored), commentsFixed: num(o.comments_fixed),
    said: opt(o.said) ?? null, failure: failureOf(o.failure),
  };
}

const files = (v: unknown): ChangedFile[] => arr(v).map((f) => {
  if (typeof f === "string") return { path: f };
  const x = obj(f);
  return { path: str(x.path), bytes: optNum(x.bytes) };
});

export function parseOutputs(w: unknown): CockpitOutputs {
  const o = obj(w);
  const pr = o.pr ? obj(o.pr) : null;
  const checks = o.checks ? obj(o.checks) : null;
  const tests = o.tests ? obj(o.tests) : null;
  const changes = o.changes ? obj(o.changes) : null;
  const comments = obj(o.comments);
  const preview = o.preview ? obj(o.preview) : null;
  const deploy = obj(o.deploy);
  const cost = obj(o.cost);
  const mr = obj(o.merge_ready);
  return {
    mergeReady: { ready: mr.ready === true, word: str(mr.word) },
    pr: pr ? { number: optNum(pr.number), url: opt(pr.url), filesUrl: opt(pr.files_url), decisionId: opt(pr.decision_id),
               decisionStatus: opt(pr.decision_status), madeBy: num(pr.made_by), word: str(pr.word),
               roleKey: roleOf(pr.role_key) } : null,
    checks: checks ? {
      word: str(checks.word), tip: opt(checks.tip), advisory: checks.advisory === true,
      gates: arr(checks.gates).map((g) => {
        const x = obj(g);
        return { name: str(x.name), label: str(x.label), ok: x.ok === true, waivedBy: opt(x.waived_by),
                 roleKey: roleOf(x.role_key), station: station(x.station), evidence: opt(x.evidence),
                 waivable: x.waivable !== false, reason: opt(x.reason) };
      }),
    } : null,
    tests: tests ? {
      word: str(tests.word), roleKey: roleOf(tests.role_key), outputTail: opt(tests.output_tail),
      commands: arr(tests.commands).map((c) => {
        const x = obj(c);
        return { command: str(x.command), exitCode: optNum(x.exit_code) ?? null, ok: x.ok === true, timedOut: x.timed_out === true };
      }),
    } : null,
    changes: changes ? {
      word: str(changes.word), tests: files(changes.tests), code: files(changes.code),
      testsRoleKey: roleOf(changes.tests_role_key), codeRoleKey: roleOf(changes.code_role_key), diffUrl: opt(changes.diff_url),
    } : null,
    comments: {
      word: str(comments.word, "None"),
      items: arr(comments.items).map((c) => {
        const x = obj(c);
        return { id: str(x.id), round: num(x.round, 1), severity: str(x.severity, "minor"), body: str(x.body),
                 path: opt(x.path), line: optNum(x.line), endLine: optNum(x.end_line), author: opt(x.author),
                 authorRoleKey: roleOf(x.author_role_key), status: str(x.status, "open") };
      }),
    },
    preview: preview ? { id: str(preview.id), status: str(preview.status), word: str(preview.word), url: opt(preview.url),
                         expiresAt: opt(preview.expires_at), stepsDone: optNum(preview.steps_done),
                         stepsTotal: optNum(preview.steps_total), error: opt(preview.error) } : null,
    deploy: { word: str(deploy.word, "Not set up"), items: arr(deploy.items).map((d) => obj(d)) },
    cost: {
      word: str(cost.word), meteredUsd: num(cost.metered_usd), subscriptionCalls: num(cost.subscription_calls),
      byAgent: arr(cost.by_agent).map((a) => {
        const x = obj(a);
        return { roleKey: roleOf(x.role_key), turns: num(x.turns), tokens: num(x.tokens), usd: num(x.usd) };
      }),
      byStation: Object.fromEntries(Object.entries(obj(cost.by_station)).map(([k, v]) => [k, num(v)])) as CostOutput["byStation"],
    },
  };
}

export function parseCockpit(w: unknown): Cockpit {
  const o = obj(w);
  const run = obj(o.run);
  const st = obj(o.state);
  const n = num(st.n);
  const preview = str(st.preview);
  const now = o.now ? obj(o.now) : null;
  return {
    etag: str(o.etag),
    run: {
      id: str(run.id), title: str(run.title), status: str(run.status), phase: str(run.phase),
      createdAt: opt(run.created_at), updatedAt: opt(run.updated_at),
      prUrl: opt(run.pull_request_url), prNumber: optNum(run.pull_request_number),
      decisionId: opt(run.decision_id), error: opt(run.error),
      attempt: num(run.attempt, 1), rootRunId: str(run.root_run_id, str(run.id)),
    },
    app: str(o.app),
    state: {
      state: str(st.state, "building") as BuildStateKey, word: str(st.word), tip: opt(st.tip),
      verb: str(st.verb, "Working"), station: station(st.station), k: Math.min(num(st.k), n), n,
      rounds: num(st.rounds),
      preview: preview === "starting" || preview === "running" || preview === "failed" ? preview : null,
    },
    startedAt: opt(o.started_at), stationSince: opt(o.station_since),
    costUsd: o.metered === false ? 0 : num(o.cost_usd), metered: o.metered === true,
    live: o.live === true, stoppable: o.stoppable === true,
    retryable: o.retryable === true, retryReason: opt(o.retry_reason),
    failure: failureOf(o.failure),
    lineage: arr(o.lineage).map((l) => {
      const x = obj(l);
      return { runId: str(x.run_id), attempt: num(x.attempt, 1), state: str(x.state) as BuildStateKey,
               word: str(x.word), at: opt(x.at) };
    }),
    origin: o.origin ? parseOrigin(o.origin) : null,
    agents: arr(o.agents).map((a) => { const x = obj(a); return { roleKey: str(x.role_key), state: str(x.state, "idle") }; })
      .filter((a) => a.roleKey),
    stations: arr(o.stations).map(parseStation),
    attention: arr(o.attention).map(parseAttention),
    story: arr(o.story).map(parseStory),
    now: now && opt(now.tool) ? { actId: str(now.act_id), roleKey: roleOf(now.role_key), tool: str(now.tool),
                                   arg: opt(now.arg), at: str(now.at) } : null,
    outputs: parseOutputs(o.outputs),
  };
}

// ── The poll ────────────────────────────────────────────────────────────────────────────────

export const COCKPIT_LIVE_MS = 4000;
export const COCKPIT_IDLE_MS = 30_000;

export function cockpitUrl(projectId: string, runId: string, viewer?: string): string {
  return `/v1/code-tasks/${encodeURIComponent(runId)}/cockpit?project_id=${encodeURIComponent(projectId)}`
    + (viewer ? `&viewer=${encodeURIComponent(viewer)}` : "");
}

export function useCockpit(projectId: string, runId: string | null, viewer?: string): Polled<Cockpit> {
  return usePolled(projectId && runId ? cockpitUrl(projectId, runId, viewer) : null, parseCockpit,
    (c) => (c && !c.live ? COCKPIT_IDLE_MS : COCKPIT_LIVE_MS));
}
