/**
 * INFRA AGENT · STORAGE SELF-HEALING — the console's typed client (BOSSAPP-1145 / ST-11 1156),
 * built against the wire contract in docs/infra/INFRA_AGENT_API.md (ST-12, the backend branch).
 *
 * VISIBILITY IS A DIAGRAM, NOT TEXT. The API sends the incident's progress as `flow`, a list of
 * StripNodes (lib/ambient.ts) each with a state and a time — detected → paused N → cleaning →
 * freed → resumed — and every surface draws those nodes; the prose sits behind "details". This
 * file parses the wire defensively (a field the API does not send is null or empty, never a
 * crash), so the screens are the same against the sample data and the live API.
 *
 * Routes (every read is same-origin through app/v1/[...path]/route.ts; permissions in §1 of the
 * contract):
 *   GET  /v1/infra/storage/incidents/current          {incidents, hold}      · every surface, 10 s
 *   GET  /v1/infra/storage/incidents/{id}             {incident, actions, receipts, paused_work, decisions} (S5)
 *   GET  /v1/infra/storage/incidents/{id}/report.md   text/markdown export
 *   POST /v1/infra/storage/incidents/{id}/acknowledge system:operate · the strip stops flashing, never hides
 *   GET  /v1/infra/storage/summary?host_id=           S3, 60 s
 *   POST /v1/infra/storage/clean                      autonomy:manage · tier 1 now, no hold
 *   PUT  /v1/infra/storage/rules                      autonomy:manage · a rule on/off
 *   GET  /v1/decisions/{id}                           the tier 3 question (recommendation.action = storage_delete)
 *   POST /v1/decisions/{id}/approve|reject            the one tap (S4)
 *
 * SAMPLE MODE (`?infra_demo=1|asking|resolved|quiet`, remembered for the tab): every read is
 * answered from lib/infra-storage-demo.ts (in the wire's own shape) and every write is a no-op
 * that says so, so the screens can be walked against the wireframes before a host helper runs.
 * Always labelled "Sample data".
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { clampWords, type NodeType, type StripNode } from "./ambient";
import type { Permission } from "./identity/permissions.gen";
import { usePolled } from "./factory-floor";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";
import { demoDecision, demoIncident, demoIncidentDetail, demoSummary, type DemoMode } from "./infra-storage-demo";

// ── the wire → the console's shapes ─────────────────────────────────────────────────────────

export type IncidentStatus = "warn" | "critical" | "asking" | "resuming" | "resolved";
export type NodeState = "done" | "active" | "waiting" | "ask";
export type StepState = "done" | "active" | "waiting" | "ask" | "failed" | "skipped";

/** One node of the incident's flow: a StripNode with its state, time and one-sentence detail. */
export type FlowNode = StripNode & { key: string; state: NodeState; at: string | null; detail: string };

/** One step of "Cleaning, safest first" (contract §2 PlanStep, in the console's words). */
export type PlanStep = {
  key: string; tool: string; label: string; detail: string; tier: 1 | 2 | 3; where: "host" | "api";
  state: StepState;
  /** The dry run's count and GB; `freedGb` once it ran. */
  items: number | null; gb: number | null; freedGb: number;
  planHash: string | null; decisionId: string | null; receiptId: string | null; reason: string | null;
};

export type StorageIncident = {
  id: string; hostId: string; hostLabel: string; status: IncidentStatus; severity: "warn" | "critical";
  openedAt: string | null; resolvedAt: string | null;
  causeKey: string | null; causeText: string;
  freePctStart: number | null; freePctNow: number | null; targetPct: number;
  freedGb: number; etaSeconds: number | null;
  hold: { active: boolean; since: string | null; releasedAt: string | null; pausedRuns: number };
  pausedRuns: number;
  stepIndex: number; stepCount: number;
  plan: PlanStep[];
  flow: FlowNode[];
  acknowledgedBy: string | null; acknowledgedAt: string | null;
  spendUsd: number;
  /** Receipted tool calls so far (steps with a receipt). */
  receipts: number;
  helper: { lastSeenAt: string | null; reachable: boolean };
  /** The tier 3 decision the plan waits for, if any. */
  askDecisionId: string | null;
  report: Report | null;
};

export type HoldStatus = { active: boolean; incidentId: string | null; hostId: string | null; since: string | null; pausedRuns: number; releasedAt: string | null };
export type CurrentIncidents = { incidents: StorageIncident[]; hold: HoldStatus | null; generatedAt: string | null };

export type RemovedRow = { type: string; detail: string; by: "rule" | "person" | "rebuildable"; gb: number; receiptId: string | null };
export type Report = {
  summary: string; freedGb: number; pausedRuns: number; waitedSecondsMax: number; lostOrRepeated: number;
  costUsd: number; durationSeconds: number;
  cause: { key: string | null; text: string; chips: string[] };
  removed: RemovedRow[];
  learned: { ruleAdded: RetentionRule | null; ruleProposed: { decisionId: string; kind: string; words: string } | null };
  timeline: FlowNode[];
};

export type PausedRow = { jobId: string; kind: string; runId: string | null; title: string; pausedAt: string | null; resumedAt: string | null; waitedSeconds: number };
export type IncidentDetail = {
  incident: StorageIncident;
  receiptsCount: number;
  pausedWork: PausedRow[];
  decisions: { id: string; kind: string; status: string; question: string }[];
  toolCalls: number;
};

/** The tier 3 question, as the decision's `recommendation` carries it (contract §7). */
export type StorageAsk = {
  decisionId: string; status: string; question: string; askedAt: string | null;
  incidentId: string | null; hostId: string;
  tool: string; args: Record<string, string | number | boolean>; planHash: string;
  count: number; gb: number; keeps: number | null;
  toDelete: string[]; kept: string[];
  alreadyDoneGb: number; freePctNow: number | null; targetPct: number; pausedRuns: number;
  rule: { kind: string; words: string; preticked: boolean } | null;
  alternatives: { label: string; args: Record<string, string | number | boolean> }[];
  neverAutomatic: string[];
  /** What this cleanup left for a person (unlabelled items, data volumes, non-scratch databases). */
  leftAlone: string[];
};

export type StorageType = { key: string; label: string; gb: number; pct: number; rebuildable: boolean };
export type RetentionRule = { id: string; kind: string; label: string; words: string; rule: Record<string, unknown>; enabled: boolean; source: string; hostId: string | null };
export type HostStorageRow = { hostId: string; label: string; freePct: number; fullInDays: number | null; level: string; spark: number[] };
export type Thresholds = { warnPct: number; criticalPct: number; targetPct: number; floorPct: number; floorGb: number | null; warnDays: number | null; criticalDays: number | null };

export type StorageSummary = {
  hostId: string; hostLabel: string; sampledAt: string | null; source: string | null; stale: boolean; helperOverdue?: boolean;
  totalGb: number; freeGb: number; freePct: number; usedPct: number;
  byType: StorageType[];
  /** 7-day growth, GB per day; "full in N days" (null = not growing) and with the rules. */
  growthGbPerDay: number; fullInDays: number | null; fullAt: string | null; withRulesDays: number | null; windowDays: number; samples: number;
  /** 30 days of used GB, oldest first. */
  trend: { at: string; usedGb: number; freeGb: number }[];
  thresholds: Thresholds;
  level: "ok" | "warn" | "critical" | "floor";
  rules: RetentionRule[];
  lastCleanup: { incidentId: string; at: string; freedGb: number; causeKey: string | null } | null;
  openIncidentId: string | null;
  hosts: HostStorageRow[];
  docker: { unreclaimedGb: number } | null;
  /** ST-8: the telemetry tables' share of the database and the last retention sweep. */
  telemetry: TelemetryShare | null;
  /** BOSSAPP-1193: what orphan-volume cleanup could free, and BOSS volumes it cannot judge yet. */
  volumes: { reclaimableGb: number; needsRelabel: number } | null;
  /** BOSSAPP-1459: where the space goes by category, against the one budget (disk_budget.py). */
  budget: DiskBudget | null;
};

export type BudgetLevel = "ok" | "near" | "over" | "unknown";
/** note: what a person might read as a problem with nothing to run for it (cache that installed
 *  images share, ENT-SETUP-01); "" when there is none. */
export type BudgetRow = { key: string; label: string; gb: number | null; budgetGb: number; level: BudgetLevel; boundedBy: string; what: string; action: string; note: string };
export type DiskBudget = { rows: BudgetRow[]; totalGb: number; budgetGb: number; warnAtPct: number };

export type TelemetrySweep = { at: string; rows: number; bytes: number; partitions: string[]; archived: number; errors: string[]; receiptId: string | null };
export type TelemetryShare = {
  gb: number; sharePct: number | null;
  tables: { table: string; gb: number; keepDays: number | null; partitioned: boolean }[];
  lastSweep: TelemetrySweep | null;
};

export function parseTelemetry(w: unknown): TelemetryShare | null {
  if (!w || typeof w !== "object" || Array.isArray(w)) return null;
  const o = obj(w);
  const last = obj(o.last_sweep);
  return {
    gb: num(o.gb), sharePct: optNum(o.share_pct),
    tables: arr(o.tables).map((t) => { const x = obj(t); return { table: str(x.table), gb: num(x.gb), keepDays: optNum(x.keep_days), partitioned: x.partitioned === true }; }).filter((t) => t.table),
    lastSweep: str(last.at) ? { at: str(last.at), rows: num(last.rows), bytes: num(last.bytes), partitions: strs(last.partitions), archived: num(last.archived), errors: strs(last.errors), receiptId: optStr(last.receipt_id) } : null,
  };
}

/** The budget block of the summary; null when the API sends none (an older one) or odd shapes. */
export function parseBudget(w: unknown): DiskBudget | null {
  if (!w || typeof w !== "object" || Array.isArray(w)) return null;
  const o = obj(w);
  const levels: BudgetLevel[] = ["ok", "near", "over", "unknown"];
  const rows = arr(o.rows).map((r) => {
    const x = obj(r);
    const level = str(x.level) as BudgetLevel;
    return { key: str(x.key), label: str(x.label) || str(x.key), gb: optNum(x.gb), budgetGb: num(x.budget_gb),
             level: levels.includes(level) ? level : "unknown", boundedBy: str(x.bounded_by), what: str(x.what), action: str(x.action), note: str(x.note) };
  }).filter((r) => r.key);
  if (!rows.length) return null;
  const t = obj(o.total);
  return { rows, totalGb: num(t.gb), budgetGb: num(t.budget_gb), warnAtPct: num(o.warn_at_pct, 80) };
}

// ── parse ───────────────────────────────────────────────────────────────────────────────────

type W = Record<string, unknown>;
const obj = (w: unknown): W => (w && typeof w === "object" && !Array.isArray(w) ? (w as W) : {});
const arr = (w: unknown): unknown[] => (Array.isArray(w) ? w : []);
const str = (w: unknown, d = ""): string => (typeof w === "string" ? w : w === null || w === undefined ? d : String(w));
const optStr = (w: unknown): string | null => (typeof w === "string" && w ? w : null);
const num = (w: unknown, d = 0): number => (typeof w === "number" && Number.isFinite(w) ? w
  : w !== null && w !== undefined && w !== "" && Number.isFinite(Number(w)) ? Number(w) : d);
const optNum = (w: unknown): number | null => (w === null || w === undefined || w === "" ? null : Number.isFinite(Number(w)) ? Number(w) : null);
const strs = (w: unknown): string[] => arr(w).map((x) => str(x)).filter(Boolean);

const STATUSES = new Set<IncidentStatus>(["warn", "critical", "asking", "resuming", "resolved"]);
const NODE_STATES = new Set<NodeState>(["done", "active", "waiting", "ask"]);
const NODE_TYPES = new Set<NodeType>(["signal", "agent", "person", "policy", "squad", "boss", "action", "decision", "needs_you", "run", "outcome", "learning"]);
const STEP_STATE: Record<string, StepState> = {
  planned: "waiting", queued: "waiting", running: "active", done: "done", failed: "failed", refused: "failed", skipped: "skipped", ask: "ask",
  active: "active", waiting: "waiting",
};

/** A flow node as the API sends it (label ≤ 4 words, clamped again here). The state and time
 *  are fields; an older shape that put them among the badges is read the same way. */
export function parseFlowNode(w: unknown): FlowNode | null {
  const o = obj(w);
  const label = str(o.label).trim();
  if (!label) return null;
  const type = str(o.type);
  const rawBadges = strs(o.badges);
  const stateWord = str(o.state) || rawBadges.find((b) => NODE_STATES.has(b as NodeState)) || "waiting";
  const badges = rawBadges.filter((b) => !NODE_STATES.has(b as NodeState));
  const timeBadge = badges.find((b) => /^\d{1,2}:\d{2}(\s*[–-]\s*\d{1,2}:\d{2})?$/.test(b));
  return {
    type: (NODE_TYPES.has(type as NodeType) ? type : "action") as NodeType,
    label: clampWords(label, 4),
    badges: badges.slice(0, 2),
    ...(optStr(o.icon) ? { icon: str(o.icon) } : {}),
    key: str(o.key) || label.toLowerCase().replace(/\W+/g, "_"),
    state: (NODE_STATES.has(stateWord as NodeState) ? stateWord : "waiting") as NodeState,
    at: optStr(o.at) ?? timeBadge ?? null,
    detail: str(o.detail) || badges.find((b) => b !== timeBadge) || "",
  };
}

/** What a step says under its name: the dry-run count, or why it was skipped or refused. */
function stepDetail(o: W, items: number | null, reason: string | null, state: StepState): string {
  const given = str(o.detail);
  if (given) return given;
  if ((state === "failed" || state === "skipped") && reason) return reason;
  if (items !== null) return `${items.toLocaleString("en-US")} item${items === 1 ? "" : "s"}${state === "ask" ? " · asks you first" : ""}`;
  return state === "ask" ? "asks you first" : "";
}

export function parsePlanStep(w: unknown): PlanStep | null {
  const o = obj(w);
  const label = str(o.label) || str(o.tool).replace(/^storage\./, "").replace(/_/g, " ");
  if (!label) return null;
  const tier = num(o.tier, 1);
  const state = STEP_STATE[str(o.state)] ?? "waiting";
  const items = optNum(o.items);
  const reason = optStr(o.reason);
  return {
    key: str(o.tool) || str(o.key) || label.toLowerCase().replace(/\W+/g, "_"), tool: str(o.tool), label: clampWords(label, 4),
    detail: stepDetail(o, items, reason, state),
    tier: (tier === 2 || tier === 3 ? tier : 1) as 1 | 2 | 3, where: str(o.where) === "api" ? "api" : "host",
    state, items, gb: optNum(o.gb), freedGb: num(o.freed_gb),
    planHash: optStr(o.plan_hash), decisionId: optStr(o.decision_id), receiptId: optStr(o.receipt_id), reason,
  };
}

/** The retention rule as words: "keep 10 · 7 days · 20 GB cap". */
export function ruleWords(kind: string, rule: Record<string, unknown>): string {
  const n = (k: string) => optNum(rule[k]);
  const parts: string[] = [];
  if (kind === "backups") {
    if (n("keep_newest") !== null) parts.push(`keep ${n("keep_newest")}`);
    if (n("keep_days") !== null) parts.push(`${n("keep_days")} days`);
    if (n("cap_gb") !== null) parts.push(`${n("cap_gb")} GB cap`);
  } else if (kind === "images") {
    if (n("keep_versions") !== null) parts.push(n("keep_versions") === 2 ? "current + previous version" : `current + ${num(rule.keep_versions) - 1} versions`);
  } else if (kind === "build_cache") {
    if (n("cap_gb") !== null) parts.push(`${n("cap_gb")} GB cap`);
  } else if (kind === "logs") {
    if (n("max_size_mb") !== null) parts.push(`${n("max_size_mb")} MB × ${n("max_file") ?? 1} files`);
  } else if (kind === "telemetry") {
    if (n("keep_days") !== null) parts.push(`${n("keep_days")} days`);
  } else if (kind === "scratch_db") {
    if (n("ttl_hours") !== null) parts.push(`${n("ttl_hours")} h`);
  } else if (kind === "kg_derived") {
    if (n("unused_days") !== null) parts.push(`${n("unused_days")} days unused`);
  } else if (kind === "pg_temp") {
    if (n("cap_gb") !== null) parts.push(`${n("cap_gb")} GB per query`);
  } else if (kind === "docker_trim") {
    parts.push(rule.enabled === false ? "off" : str(rule.schedule) || "after every cleanup");
  }
  if (!parts.length) {
    for (const [k, v] of Object.entries(rule)) if (typeof v !== "object") parts.push(`${k.replace(/_/g, " ")} ${String(v)}`);
  }
  return parts.join(" · ");
}

export function parseRule(w: unknown): RetentionRule | null {
  const o = obj(w);
  const kind = str(o.kind);
  if (!kind && !str(o.id)) return null;
  const rule = obj(o.rule);
  return { id: str(o.id) || kind, kind, label: str(o.label) || kind.replace(/_/g, " "), words: ruleWords(kind, rule), rule,
           enabled: o.enabled !== false, source: str(o.source, "default"), hostId: optStr(o.host_id) };
}

export function parseReport(w: unknown): Report | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  const cause = obj(o.cause);
  const learned = obj(o.learned);
  const proposed = obj(learned.rule_proposed);
  return {
    summary: str(o.summary), freedGb: num(o.freed_gb), pausedRuns: num(o.paused_runs), waitedSecondsMax: num(o.waited_seconds_max),
    lostOrRepeated: num(o.lost_or_repeated), costUsd: num(o.cost_usd), durationSeconds: num(o.duration_seconds),
    cause: { key: optStr(cause.key), text: str(cause.text), chips: strs(cause.chips) },
    removed: arr(o.removed).map((r) => { const x = obj(r); const by = str(x.by); return { type: str(x.type), detail: str(x.detail), by: (by === "person" || by === "rule" ? by : "rebuildable") as RemovedRow["by"], gb: num(x.gb), receiptId: optStr(x.receipt_id) }; }).filter((r) => r.type),
    learned: {
      ruleAdded: learned.rule_added ? parseRule(learned.rule_added) : null,
      ruleProposed: str(proposed.decision_id) ? { decisionId: str(proposed.decision_id), kind: str(proposed.kind), words: ruleWords(str(proposed.kind), obj(proposed.rule)) } : null,
    },
    timeline: arr(o.timeline).map(parseFlowNode).filter((n): n is FlowNode => n !== null),
  };
}

export function parseIncident(w: unknown): StorageIncident | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  const id = str(o.id);
  if (!id) return null;
  const status = str(o.status, "critical");
  const hold = obj(o.hold);
  const helper = obj(o.helper);
  const plan = arr(o.plan).map(parsePlanStep).filter((s): s is PlanStep => s !== null);
  const done = plan.filter((s) => s.state === "done" || s.state === "skipped").length;
  const pausedRuns = num(hold.paused_runs, num(o.paused_runs));
  const flow = arr(o.flow).map(parseFlowNode).filter((n): n is FlowNode => n !== null);
  const hostId = str(o.host_id, "this host");
  return {
    id, hostId, hostLabel: str(o.host_label) || hostId,
    status: (STATUSES.has(status as IncidentStatus) ? status : "critical") as IncidentStatus,
    severity: str(o.severity) === "warn" || status === "warn" ? "warn" : "critical",
    openedAt: optStr(o.opened_at), resolvedAt: optStr(o.resolved_at),
    causeKey: optStr(o.cause_key), causeText: str(o.cause_text),
    freePctStart: optNum(o.free_pct_start), freePctNow: optNum(o.free_pct_now), targetPct: num(o.target_pct, 25),
    freedGb: num(o.freed_gb), etaSeconds: optNum(o.eta_seconds),
    hold: { active: hold.active === true, since: optStr(hold.since), releasedAt: optStr(hold.released_at), pausedRuns },
    pausedRuns,
    stepIndex: Math.min(plan.length, done + 1), stepCount: plan.length,
    plan, flow,
    acknowledgedBy: optStr(o.acknowledged_by), acknowledgedAt: optStr(o.acknowledged_at),
    spendUsd: num(o.spend_usd),
    receipts: num(o.receipts, plan.filter((s) => s.receiptId).length),
    helper: { lastSeenAt: optStr(helper.last_seen_at), reachable: helper.reachable !== false },
    askDecisionId: plan.find((s) => s.state === "ask" && s.decisionId)?.decisionId ?? optStr(o.ask_decision_id),
    report: parseReport(o.report),
  };
}

export function parseHold(w: unknown): HoldStatus | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  return { active: o.active === true, incidentId: optStr(o.incident_id), hostId: optStr(o.host_id), since: optStr(o.since), pausedRuns: num(o.paused_runs), releasedAt: optStr(o.released_at) };
}

/** `/incidents/current`: `{incidents, hold}` (a bare list or one object is read the same way). */
export function parseCurrent(w: unknown): CurrentIncidents {
  const o = obj(w);
  const list = Array.isArray(w) ? w : Array.isArray(o.incidents) ? arr(o.incidents) : o.incident !== undefined ? [o.incident] : o.id !== undefined ? [w] : [];
  return { incidents: list.map(parseIncident).filter((i): i is StorageIncident => i !== null), hold: parseHold(o.hold), generatedAt: optStr(o.generated_at) };
}
/** The one incident the surfaces show: the first open (most severe first, as the API orders). */
export function openIncident(c: CurrentIncidents | null): StorageIncident | null {
  if (!c) return null;
  return c.incidents.find((i) => i.status !== "resolved") ?? null;
}

export function parseIncidentDetail(w: unknown): IncidentDetail | null {
  const o = obj(w);
  const incident = parseIncident(o.incident ?? o);
  if (!incident) return null;
  const actions = arr(o.actions).map(obj);
  return {
    incident,
    receiptsCount: Math.max(arr(o.receipts).length, incident.receipts),
    toolCalls: actions.length || incident.receipts,
    pausedWork: arr(o.paused_work).map((p) => { const x = obj(p); return { jobId: str(x.job_id), kind: str(x.kind), runId: optStr(x.run_id), title: str(x.title) || str(x.run_id) || str(x.job_id), pausedAt: optStr(x.paused_at), resumedAt: optStr(x.resumed_at), waitedSeconds: num(x.waited_seconds) }; }).filter((p) => p.jobId || p.title),
    decisions: arr(o.decisions).map((d) => { const x = obj(d); return { id: str(x.id), kind: str(x.kind), status: str(x.status), question: str(x.question) }; }).filter((d) => d.id),
  };
}

/** The tier 3 card from a decision (GET /v1/decisions/{id}, or the inbox's own row). */
export function parseAsk(w: unknown): StorageAsk | null {
  const o = obj(w);
  const decisionId = str(o.id) || str(o.decision_id);
  const r = obj(o.recommendation);
  if (!decisionId || (str(r.action) !== "storage_delete" && !str(r.plan_hash) && !str(r.tool))) return null;
  const args: Record<string, string | number | boolean> = {};
  for (const [k, v] of Object.entries(obj(r.args))) args[k] = typeof v === "number" || typeof v === "boolean" ? v : str(v);
  const proposal = obj(r.rule_proposal);
  return {
    decisionId, status: str(o.status, "pending"), question: str(o.question), askedAt: optStr(o.created_at),
    incidentId: optStr(r.incident_id), hostId: str(r.host_id, "this host"),
    tool: str(r.tool), args, planHash: str(r.plan_hash),
    count: num(r.items), gb: num(r.gb), keeps: optNum(r.keeps),
    toDelete: strs(r.to_delete), kept: strs(r.kept),
    alreadyDoneGb: num(r.already_done_gb), freePctNow: optNum(r.free_pct_now), targetPct: num(r.target_pct, 25), pausedRuns: num(r.paused_runs),
    rule: str(proposal.kind) ? { kind: str(proposal.kind), words: ruleWords(str(proposal.kind), obj(proposal.rule)), preticked: proposal.preticked !== false } : null,
    alternatives: arr(r.alternatives).map((a) => { const x = obj(a); const aa: Record<string, string | number | boolean> = {}; for (const [k, v] of Object.entries(obj(x.args))) aa[k] = typeof v === "number" || typeof v === "boolean" ? v : str(v); return { label: str(x.label), args: aa }; }).filter((a) => a.label),
    neverAutomatic: strs(r.never_automatic),
    leftAlone: strs(r.left_alone),
  };
}

export function parseSummary(w: unknown): StorageSummary | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  const disk = obj(o.disk);
  const totalGb = num(disk.total_gb, num(o.total_gb));
  const freeGb = num(disk.free_gb, num(o.free_gb));
  const fc = obj(o.forecast);
  const th = obj(o.thresholds);
  const last = obj(o.last_cleanup);
  const byType = arr(o.by_type).map((t) => { const x = obj(t); return { key: str(x.key), label: str(x.label) || str(x.key), gb: num(x.gb), pct: num(x.pct, totalGb > 0 ? (num(x.gb) / totalGb) * 100 : 0), rebuildable: x.rebuildable === true }; }).filter((t) => t.key);
  const level = str(o.level);
  const hostId = str(o.host_id, "this host");
  const unreclaimed = byType.find((t) => t.key === "docker_vm_unreclaimed");
  return {
    hostId, hostLabel: str(o.host_label) || hostId, sampledAt: optStr(o.sampled_at), source: optStr(o.source), stale: o.stale === true, helperOverdue: o.helper_overdue === true,
    totalGb, freeGb, freePct: num(disk.free_pct, totalGb > 0 ? Math.round((freeGb / totalGb) * 100) : 0), usedPct: num(disk.used_pct, totalGb > 0 ? Math.round(((totalGb - freeGb) / totalGb) * 100) : 0),
    byType,
    growthGbPerDay: num(fc.growth_gb_per_day), fullInDays: optNum(fc.days_until_full), fullAt: optStr(fc.full_at),
    withRulesDays: optNum(fc.with_rules_days_until_full), windowDays: num(fc.window_days, 7), samples: num(fc.samples),
    trend: arr(o.trend).map((p) => { const x = obj(p); return { at: str(x.at), usedGb: num(x.used_gb), freeGb: num(x.free_gb) }; }),
    thresholds: { warnPct: num(th.warn_free_pct, 20), criticalPct: num(th.critical_free_pct, 15), targetPct: num(th.target_free_pct, 25), floorPct: num(th.floor_free_pct, 10),
                  floorGb: optNum(th.floor_free_gb), warnDays: optNum(th.warn_days), criticalDays: optNum(th.critical_days) },
    level: (level === "warn" || level === "critical" || level === "floor" ? level : "ok") as StorageSummary["level"],
    rules: arr(o.rules).map(parseRule).filter((r): r is RetentionRule => r !== null),
    lastCleanup: str(last.at) ? { incidentId: str(last.incident_id), at: str(last.at), freedGb: num(last.freed_gb), causeKey: optStr(last.cause_key) } : null,
    openIncidentId: optStr(o.open_incident_id),
    hosts: arr(o.hosts).map((h) => { const x = obj(h); return { hostId: str(x.host_id), label: str(x.host_label) || str(x.host_id), freePct: num(x.free_pct), fullInDays: optNum(x.days_until_full), level: str(x.level, "ok"), spark: arr(x.spark).map((v) => num(v)) }; }).filter((h) => h.hostId),
    docker: unreclaimed && unreclaimed.gb > 0 ? { unreclaimedGb: unreclaimed.gb } : null,
    telemetry: parseTelemetry(o.telemetry),
    volumes: o.volumes && typeof o.volumes === "object"
      ? { reclaimableGb: num(obj(o.volumes).reclaimable_gb), needsRelabel: Math.max(0, Math.floor(num(obj(o.volumes).needs_relabel))) }
      : null,
    budget: parseBudget(o.budget),
  };
}

// ── words and arithmetic (pure, tested) ─────────────────────────────────────────────────────

/** "38 GB", "4.4 GB", "0.7 GB". */
export function gb(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "…";
  const a = Math.abs(v);
  return `${a >= 10 ? Math.round(a).toLocaleString("en-US") : a.toFixed(1).replace(/\.0$/, "")} GB`;
}

/** "~6 min left", "~40 s left", "" when unknown. */
export function etaWords(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 90) return `~${Math.max(5, Math.round(seconds / 5) * 5)} s left`;
  return `~${Math.round(seconds / 60)} min left`;
}

/** "18:02" from an ISO time in the viewer's zone; a non-ISO value (an "18:03–18:09" range from a
 *  sample) is kept as it is; "" when absent. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  if (/^\d{1,2}:\d{2}/.test(iso)) return iso;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** "18:14 today", "2 Nov 09:10": when something last happened, in the viewer's zone. */
export function whenWord(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (d.toDateString() === new Date(now).toDateString()) return `${hm} today`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${hm}`;
}

/** "full in N days": the server's forecast, or the arithmetic from growth when it gave none. */
export function fullInDays(freeGb: number, growthGbPerDay: number, given: number | null): number | null {
  if (given !== null && Number.isFinite(given)) return Math.max(0, Math.round(given * 10) / 10);
  if (!(growthGbPerDay > 0) || !(freeGb >= 0)) return null;
  return Math.max(0, Math.round((freeGb / growthGbPerDay) * 10) / 10);
}

/** How loud the Storage card is: red under critical, amber under the alert, green above. */
export function storageTone(freePct: number, th: { warnPct: number; criticalPct: number }): "green" | "amber" | "red" {
  if (freePct <= th.criticalPct) return "red";
  if (freePct <= th.warnPct) return "amber";
  return "green";
}

/** "84 days", "1.8 days", "> 90 days", "today", "not filling". */
export function daysWord(days: number | null): string {
  if (days === null) return "not filling";
  if (days < 0.5) return "today";
  if (days > 90) return "> 90 days";
  const n = days < 10 && !Number.isInteger(days) ? days.toFixed(1) : String(Math.round(days));
  return `${n} day${n === "1" ? "" : "s"}`;
}

/** The strip's one sentence: "Storage critical on this Mac · Infra Agent is cleaning". */
export function stripHeadline(i: StorageIncident): string {
  const doing = storageActivity(i);
  return `Storage ${i.severity === "warn" ? "low" : "critical"} on ${i.hostLabel} · ${doing}`;
}
export function storageActivity(i: StorageIncident): string {
  if (i.status === "resolved") return "Storage recovered";
  if (i.status === "asking") return "Infra Agent needs one answer";
  if (i.status === "resuming") return "Infra Agent is resuming work";
  if (i.plan.some(step => step.state === "active" && (step.where === "api" || i.helper.reachable))) return "Infra Agent is cleaning";
  if (!i.helper.reachable) return "Cleanup blocked: host helper unavailable";
  if (i.plan.some(step => step.state === "waiting")) return "Cleanup queued";
  return "Cleanup exhausted: free space by hand";
}
export function stripSubline(i: StorageIncident): string {
  const parts: string[] = [];
  if (i.freePctNow !== null) parts.push(`${Math.round(i.freePctNow)} % free`);
  if (i.pausedRuns > 0) parts.push(`${i.pausedRuns} run${i.pausedRuns === 1 ? "" : "s"} paused at a checkpoint`, "nothing is lost");
  if (!i.helper.reachable) {
    parts.push("The host helper isn't answering, so BOSS can't free space. On the host run "
      + "`boss steward status`, then `boss steward install`.");
  }
  return parts.join(" · ");
}
/** Progress towards the target, 0..1. */
export function progressToTarget(i: Pick<StorageIncident, "freePctStart" | "freePctNow" | "targetPct">): number {
  const start = i.freePctStart ?? 0;
  const now = i.freePctNow ?? start;
  const span = i.targetPct - start;
  if (!(span > 0)) return now >= i.targetPct ? 1 : 0;
  return Math.max(0, Math.min(1, (now - start) / span));
}

/** The Command Center's status line while an incident is open (between budget_stopped and
 *  host_critical, as the server orders them; contract §9). */
export function withInfraStatus<S extends { key: string; text: string; tone: string; tip: string }>(status: S, incident: StorageIncident | null): S {
  if (!incident || incident.status === "resolved") return status;
  if (status.key === "unreachable" || status.key === "budget_stopped") return status;
  return { ...status, key: "infra_incident", text: storageActivity(incident), tone: "red",
           tip: incident.causeText ? `Storage on ${incident.hostLabel}: ${incident.causeText}` : `Storage incident on ${incident.hostLabel}` };
}

// ── sample mode ─────────────────────────────────────────────────────────────────────────────

const DEMO_KEY = "boss.infra_demo";
const MODES = new Set<DemoMode>(["1", "asking", "resolved", "quiet"]);

/** `?infra_demo=` wins; otherwise what this tab remembered. "" when off — and ALWAYS "" in a
 *  production build: the sample never stands in for a real host there, and nothing is remembered. */
export function infraDemoMode(search = typeof window === "undefined" ? "" : window.location.search): DemoMode | "" {
  if (process.env.NODE_ENV === "production") return "";
  const q = new URLSearchParams(search).get("infra_demo");
  if (q !== null) {
    const mode = (q === "" ? "1" : q) as DemoMode;
    if (q === "0" || q === "off") { try { window.sessionStorage.removeItem(DEMO_KEY); } catch { /* none */ } return ""; }
    if (MODES.has(mode)) { try { window.sessionStorage.setItem(DEMO_KEY, mode); } catch { /* none */ } return mode; }
  }
  try {
    const kept = typeof window === "undefined" ? null : window.sessionStorage.getItem(DEMO_KEY);
    return kept && MODES.has(kept as DemoMode) ? (kept as DemoMode) : "";
  } catch { return ""; }
}

function useDemoMode(): DemoMode | "" {
  const [demo, setDemo] = useState<DemoMode | "">("");
  useEffect(() => { setDemo(infraDemoMode()); }, []);
  return demo;
}

// ── urls ────────────────────────────────────────────────────────────────────────────────────

const qs = (more: Record<string, string | null | undefined> = {}) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(more)) if (v) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
};
export const currentIncidentUrl = (projectId: string) => `/v1/infra/storage/incidents/current${qs({ project_id: projectId })}`;
export const incidentUrl = (projectId: string, id: string) => `/v1/infra/storage/incidents/${encodeURIComponent(id)}${qs({ project_id: projectId })}`;
export const incidentMarkdownUrl = (id: string) => `/v1/infra/storage/incidents/${encodeURIComponent(id)}/report.md`;
export const summaryUrl = (projectId: string, hostId?: string | null) => `/v1/infra/storage/summary${qs({ project_id: projectId, host_id: hostId })}`;
export const decisionUrl = (projectId: string, id: string) => `/v1/decisions/${encodeURIComponent(id)}${qs({ project_id: projectId })}`;

export const INCIDENT_POLL_MS = 10_000;
export const SUMMARY_POLL_MS = 60_000;

/** Where the incident's report lives in the console (Govern › Infrastructure). */
export const reportPatch = (incidentId: string, hostId?: string | null) =>
  ({ area: "budget", tab: "infrastructure", incident: incidentId, host: hostId ?? null, role: null, view: null });
export const storagePatch = (hostId?: string | null) => ({ area: "budget", tab: "infrastructure", incident: null, host: hostId ?? null, role: null, view: null });

// ── hooks ───────────────────────────────────────────────────────────────────────────────────

export type IncidentState = { incident: StorageIncident | null; hold: HoldStatus | null; error: string | null; loading: boolean; refresh: () => void; sample: boolean };

/** ONE POLL PER PAGE: the shell polls once and hands the answer down; every surface under it (the
 *  strip, the Command Center card, Storage) reads the context instead of polling again. A
 *  component mounted without the provider (a test, a page outside the shell) polls on its own. */
const InfraIncidentContext = createContext<(IncidentState & { projectId: string | null }) | null>(null);

export function InfraIncidentProvider({ projectId, children }: { projectId: string | null; children: React.ReactNode }) {
  const state = usePolledIncident(projectId);
  const value = useMemo(() => ({ ...state, projectId }), [state, projectId]);
  return React.createElement(InfraIncidentContext.Provider, { value }, children);
}

/** The open storage incident, polled every 10 s (an SSE `storage.*` event restarts the poll
 *  through notifyChanged; lib/console-refresh.ts). `incident` is null when every host is fine. */
export function useInfraIncident(projectId: string | null): IncidentState {
  const shared = useContext(InfraIncidentContext);
  // The shared poll serves the same project; a different project (or no provider) polls itself.
  const own = usePolledIncident(shared && shared.projectId === projectId ? null : projectId);
  return shared && shared.projectId === projectId ? shared : own;
}

function usePolledIncident(projectId: string | null): IncidentState {
  const demo = useDemoMode();
  const [tick, setTick] = useState(0);
  const polled = usePolled(projectId && !demo ? currentIncidentUrl(projectId) : null, parseCurrent, () => INCIDENT_POLL_MS);
  const [local, setLocal] = useState<CurrentIncidents | null>(null);
  useEffect(() => { if (demo) setLocal(demoIncident(demo, tick)); }, [demo, tick]);
  // Sample mode "cleans" a little every 10 s so the card visibly moves.
  useEffect(() => {
    if (!demo) return undefined;
    const t = setInterval(() => setTick((n) => n + 1), INCIDENT_POLL_MS);
    return () => clearInterval(t);
  }, [demo]);
  const refresh = useCallback(() => { if (demo) setTick((n) => n + 1); else polled.refresh(); }, [demo, polled]);
  // A 404 from an API without the router yet is "no incident", not an error strip.
  const missing = polled.error !== null && /404|405|not found/i.test(polled.error);
  return useMemo<IncidentState>(() => (demo
    ? { incident: openIncident(local), hold: local?.hold ?? null, error: null, loading: false, refresh, sample: true }
    : { incident: openIncident(polled.data), hold: polled.data?.hold ?? null, error: missing ? null : polled.error, loading: polled.loading, refresh, sample: false }),
  [demo, local, polled.data, polled.error, polled.loading, missing, refresh]);
}

export type SummaryState = { summary: StorageSummary | null; error: string | null; loading: boolean; refresh: () => void; sample: boolean };

export function useStorageSummary(projectId: string | null, hostId?: string | null): SummaryState {
  const demo = useDemoMode();
  const polled = usePolled(projectId && !demo ? summaryUrl(projectId, hostId) : null, parseSummary, () => SUMMARY_POLL_MS);
  const sample = useMemo(() => (demo ? demoSummary(demo, hostId ?? null) : null), [demo, hostId]);
  if (demo) return { summary: sample, error: null, loading: false, refresh: () => {}, sample: true };
  return { summary: polled.data, error: polled.error, loading: polled.loading, refresh: polled.refresh, sample: false };
}

export type DetailState = { detail: IncidentDetail | null; error: string | null; loading: boolean; sample: boolean };

export function useIncidentDetail(projectId: string | null, incidentId: string | null): DetailState {
  const demo = useDemoMode();
  const polled = usePolled(projectId && incidentId && !demo ? incidentUrl(projectId, incidentId) : null, parseIncidentDetail,
                           (d) => (d && d.incident.status !== "resolved" ? INCIDENT_POLL_MS : 10 * SUMMARY_POLL_MS));
  const sample = useMemo(() => (demo && incidentId ? demoIncidentDetail(incidentId) : null), [demo, incidentId]);
  if (demo) return { detail: sample, error: null, loading: false, sample: true };
  return { detail: polled.data, error: polled.error, loading: polled.loading, sample: false };
}

export type AskState = { ask: StorageAsk | null; error: string | null; loading: boolean; refresh: () => void; sample: boolean };

/** The tier 3 question by decision id (the Command Center card's Review, Needs you). */
export function useStorageAsk(projectId: string | null, decisionId: string | null): AskState {
  const demo = useDemoMode();
  const polled = usePolled(projectId && decisionId && !demo ? decisionUrl(projectId, decisionId) : null, parseAsk, () => 3 * INCIDENT_POLL_MS);
  const sample = useMemo(() => (demo && decisionId ? demoDecision(decisionId) : null), [demo, decisionId]);
  if (demo) return { ask: sample, error: null, loading: false, refresh: () => {}, sample: true };
  return { ask: polled.data, error: polled.error, loading: polled.loading, refresh: polled.refresh, sample: false };
}

// ── writes ──────────────────────────────────────────────────────────────────────────────────

/** The permissions the API checks (contract §1); the buttons gate on these same constants. */
export const ACKNOWLEDGE_PERMISSION: Permission = "instance:manage";
export const CLEAN_PERMISSION: Permission = "instance:manage";
export const RULES_PERMISSION: Permission = "instance:manage";
export const STORAGE_DELETE_PERMISSION: Permission = "decision:approve";
export const REPLAN_PERMISSION: Permission = "instance:manage";

const sampleWrite = (): WriteResult => ({ ok: true, status: 200, body: { sample: true }, error: null });

/** Acknowledge (system:operate): the strip stops flashing and stays, quietly, until the incident ends. */
export async function acknowledgeIncident(incidentId: string): Promise<WriteResult> {
  if (infraDemoMode()) return sampleWrite();
  return obsWrite(`/v1/infra/storage/incidents/${encodeURIComponent(incidentId)}/acknowledge`, {}, { scope: `storage:${incidentId}:ack`, label: "Acknowledge" });
}

/** Tier 1 now (autonomy:manage): dry run first, opens a `warn` incident without the hold. */
export async function cleanNow(hostId?: string | null): Promise<WriteResult> {
  if (infraDemoMode()) return sampleWrite();
  return obsWrite("/v1/infra/storage/clean", hostId ? { host_id: hostId } : {}, { scope: "storage:clean", label: "Clean now" });
}

/** A retention rule on or off (autonomy:manage). Nothing is ever deleted: disable instead. */
export async function setRuleEnabled(rule: RetentionRule, enabled: boolean): Promise<WriteResult> {
  if (infraDemoMode()) return sampleWrite();
  return obsWrite("/v1/infra/storage/rules", { rules: [{ id: rule.id, host_id: rule.hostId, kind: rule.kind, rule: rule.rule, enabled }] },
                  { scope: `storage:rule:${rule.id}`, label: enabled ? "Turn on" : "Turn off" }, "PUT");
}

/** "Keep 20 instead" (autonomy:manage, step-up): the tier 3 step is dry-run again with these
 *  arguments and re-asked; the person then approves the NEW card. POST …/incidents/{id}/replan. */
export async function replanStorageStep(incidentId: string, tool: string, args: Record<string, string | number | boolean>): Promise<WriteResult> {
  if (infraDemoMode()) return sampleWrite();
  return obsWrite(`/v1/infra/storage/incidents/${encodeURIComponent(incidentId)}/replan`, { tool, args },
                  { scope: `storage:${incidentId}:replan`, label: "Re-plan" });
}

export type DeleteAnswer = { action: "approve"; makeRule: boolean } | { action: "reject"; note: string };

/** The tier 3 card's one tap. Approve binds to the plan hash the dry run produced (an expired one
 *  is re-planned and re-asked by the API) and carries `make_rule`. An alternative ("Keep 20
 *  instead") is NOT an approve: it is `replanStorageStep`, after which the agent asks again with a
 *  new card. The person is the session's, never a literal actor. */
export async function answerStorageDelete(decisionId: string, _actor: string, ask: StorageAsk, answer: DeleteAnswer): Promise<WriteResult> {
  if (infraDemoMode()) return sampleWrite();
  if (answer.action === "approve") {
    // The decision API reads the person's answer under `answer` (pydantic drops top-level extras);
    // content_hash stays top-level, as every approval binds to what was shown.
    const body = { content_hash: ask.planHash, answer: { make_rule: answer.makeRule, plan_hash: ask.planHash } };
    return obsWrite(`/v1/decisions/${encodeURIComponent(decisionId)}/approve`, body, { scope: `decision:${decisionId}`, label: "Delete" });
  }
  return obsWrite(`/v1/decisions/${encodeURIComponent(decisionId)}/reject`, { note: answer.note }, { scope: `decision:${decisionId}`, label: "Not now" });
}
