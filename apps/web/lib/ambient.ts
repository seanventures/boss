/**
 * AUTONOMOUS AGENTS — the console's typed client (docs/product/AUTONOMOUS_AGENTS_SPEC.md §7).
 *
 * VISIBILITY IS A DIAGRAM, NOT TEXT (design §11.0, the operator's standing rule). The API's read
 * model already returns each work item as nodes + edges + badges with ≤ 4-word labels; this file
 * parses that wire defensively, says nothing in paragraphs, and clamps every label again so a
 * long title can never reach the canvas (`clampWords`).
 *
 * Every read goes to the same-origin `/v1` proxy (app/v1/[...path]/route.ts). Every write goes
 * through obsWrite → apiWrite (one press, one effect, step-up and consent handled once).
 *
 * SAMPLE MODE (`?aa_demo=1`): every read is answered from lib/ambient-demo.ts and every write is
 * a no-op that says so. It exists so the morning review (and a fresh install with no ambient
 * work yet) can see the screens on realistic data; it is always labelled "Sample data".
 */
import { parseDiagnosis, parseWaitReason, type Diagnosis, type WaitReason } from "./stuck-work";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";
import { demoAnswer, demoWrite } from "./ambient-demo";

// ── the wire ────────────────────────────────────────────────────────────────────────────────

export type NodeType = "signal" | "agent" | "person" | "policy" | "squad" | "boss" | "action"
  | "decision" | "needs_you" | "run" | "outcome" | "learning";
export type EdgeKind = "raised" | "claimed" | "proposed" | "critique" | "vote" | "handoff" | "escalate"
  | "decided" | "settles" | "overrides" | "acted" | "asks" | "resulted" | "applied";

export type Ref = { kind: string; id?: string | null; work_item_id?: string };
export type FlowNode = { id: string; type: NodeType; label: string; stage: number; badges: string[]; ref?: Ref; detail?: string };
export type FlowEdge = { from: string; to: string; kind: EdgeKind; label: string; dissent: boolean; ref?: Ref };

export type Outcome = "needs_you" | "running" | "resolved_by_agents" | "resolved_by_person" | "overridden"
  | "failed" | "dismissed" | string;

export type ItemBadges = {
  problem_type: string; task_class: string; outcome: Outcome; needs_you: boolean;
  autonomy_level: string | null; severity: string | null; cost_usd: number; dissent: boolean; overridden: boolean;
};

export type ItemSummary = {
  id: string; title: string; lifecycle: string; task_class: string; autonomy_level: string | null;
  severity: string | null; subject_ref: string | null; app: string | null; lease_owner: string | null;
  needs_you_decision_id: string | null; resolved_by: string | null; created_at: string | null;
  updated_at: string | null; resolved_at: string | null;
};

export type StripNode = { type: NodeType; label: string; badges: string[]; /** A detector's icon (Phosphor name). */ icon?: string };
export type ListItem = ItemSummary & { badges: ItemBadges; strip: StripNode[];
  /** What the build it started waits for, and what the Shepherd found (stuck_work.item_status). */
  why?: WaitReason | null; diagnosis?: Diagnosis | null; run_id?: string | null };

export const TABS = ["needs_you", "running", "resolved_by_agents", "overridden", "failed"] as const;
export type Tab = (typeof TABS)[number];
export const TAB_WORDS: Record<Tab, { label: string; tip: string }> = {
  needs_you: { label: "Needs you", tip: "Agents want one answer from a person" },
  running: { label: "Running", tip: "Agents are working on it now" },
  resolved_by_agents: { label: "Resolved by agents", tip: "Closed with no person deciding" },
  overridden: { label: "Overridden", tip: "A person changed what an agent decided" },
  failed: { label: "Failed", tip: "Agents tried and it did not work" },
};

export type ListView = { filter: string; items: ListItem[]; counts: Record<string, number>; next_before: string | null };
export type Link = { kind: string; ref: string; note?: string | null };
export type Graph = { item: ItemSummary; badges: ItemBadges; nodes: FlowNode[]; edges: FlowEdge[]; links: Link[] };

export type Evidence = { kind: string; ref: string; note?: string | null };
export type Message = { id: string; kind: string; sender: string; body: Record<string, unknown>; reply_to: string | null; created_at: string | null };
export type Consensus = {
  mode?: string; result?: string; needs_person?: boolean; participants?: string[];
  votes?: Array<{ party?: string; vote?: string; reason?: string }>;
  dissent?: Array<{ party: string; position: string; reason?: string }>;
};
export type Decision = {
  act_id: string; work_item_id: string; question: string; decided_by: string; decider_kind: string;
  chosen: string; options: Array<{ key: string; label: string }>; rejected: Array<{ option: string; why?: string }>;
  rationale: string; evidence: Evidence[]; confidence: number | null; autonomy_level: string | null;
  task_class: string | null; action: string | null; reversibility: string | null; undo: string | null;
  consensus: Consensus | null; cost_usd_micros: number; outcome: string | null; overrides_act_id: string | null;
  created_at: string | null;
};
export type Signal = { id: string; kind: string; title: string; severity: string; source: string; occurrences: number; last_seen_at: string | null; status?: string };

export type ClassRow = {
  task_class: string; title: string; level: string; ceiling: string; max_ceiling: string; default_level: string;
  risk: string; owner_role: string | null; level_name: string; suggested_level: string | null; source: string;
};
export type AutonomyView = { classes: ClassRow[]; levels: Array<{ level: string; name: string }> };
export type Envelope = { cap_usd_micros: number; spent_usd_micros: number; held_usd_micros: number; available_usd_micros: number; resets: string };
export type StopState = { stopped: boolean; scope: string | null; by: string | null; at: string | null; reason: string };
/** The always-on mode (install-wide): env pins it (BOSS_AMBIENT_COS), else a person chose it in Govern, else off. */
export type AlwaysOnMode = "off" | "propose" | "route";
export type ModeState = { mode: AlwaysOnMode; source: "env" | "console" | "default"; pinned: boolean; by: string | null; at: string | null };
export const MODE_WORDS: Record<AlwaysOnMode, { label: string; tip: string }> = {
  off: { label: "Off", tip: "No agent picks up work by itself" },
  propose: { label: "Ask first", tip: "Agents triage and suggest; a person approves handovers on one card per project" },
  route: { label: "Act within levels", tip: "Hand-offs go straight to the owning agent from A1; each kind of work then runs at its own level below" },
};
export type SettingsView = { settings: Record<string, unknown>; envelope: Envelope; stop: StopState; mode: ModeState };

export type Summary = { n: number; median: number | null; p90: number | null };
export type DayRow = { day: string; a0: number; a1: number; a2: number; a3: number; agent: number; policy: number; squad: number; person: number; total: number };
export type Scorecard = {
  days: number; decisions_per_day: DayRow[];
  by_class: Array<{ task_class: string; decided: number; accepted_pct: number; overridden_pct: number; reverted_pct: number }>;
  time_to_human_decision_minutes: Summary; mttr_minutes: Summary; incident_mttr_minutes: Summary;
  items_ended: Record<string, number>; noise_rate: number | null;
  cost: { spent_usd: number; per_resolved_item_usd: number | null; envelope_today: Envelope };
  guards: Record<string, number>; stopped: StopState;
  /** What happened AFTER agents decided (BOSSAPP-912, outcomes.metrics + roadmap.metrics); null on an older API. */
  outcomes: Outcomes | null;
};
export type Outcomes = {
  decided: number; with_outcome: number; merged: number; reverted: number; overridden: number;
  acceptance_pct: number | null; revert_pct: number | null; override_pct: number | null;
  /** The Recommended roadmap: how much of what agents suggested a person took, and what came back. */
  roadmap: { shown: number; accepted: number; rejected: number; ignored: number; recurred: number; acceptance_pct: number | null };
  /** The share of work items BOSS started itself. */
  boss_started: { boss: number; all: number; pct: number | null };
};

// ── the recommended roadmap (BOSSAPP-911; engine services/ambient/roadmap.py list_roadmap/get) ──

export type RecKind = "hardening" | "test_gap" | "refactor" | "feature" | "design_fix" | "cost" | "process";
export type Level3 = "low" | "medium" | "high";
export type RecStatus = "open" | "snoozed" | "accepted" | "rejected" | "resolved_by_change";
/** One ranked recommendation as list_roadmap returns it: a summary, its badges and a ≤ 6-node
 *  strip (sources → detector → recommendation → decision → artifact → outcome). Value / effort /
 *  risk are the engine's numbers (value 0-100, effort 1-13 units, risk 1-1.5) bucketed to dots. */
export type Recommendation = {
  id: string; title: string; kind: RecKind | string; kindLabel: string; detector: string; detectorTitle: string;
  proposed_shape: "ticket" | "epic" | "prd" | string; status: RecStatus | string; rank: number | null; score: number;
  value: Level3; effort: Level3; risk: Level3; evidence: number; sources: number; single_source: boolean;
  resurfaced: boolean; snoozed_until: string | null; strip: StripNode[];
  /** The item's estimate in dollars, who would do it (role keys) and why (roadmap screen). */
  cost_usd?: number | null; squad?: string[]; why?: string; subject?: string;
  /** THE CARD'S OWN DIAGRAM (screen 07), from the engine (ambient/roadmap_card.py): concrete
   *  evidence ("2 incidents", "sla.py hot") → the detector ("Incident hotspot") → what the agent
   *  does, and the ≈ cost with what it is based on. Absent on an older engine. */
  card?: RecCard | null;
  /** The work it became (the run or build its artifact names), once started. */
  work_id?: string;
};
export type RecCard = {
  evidence: { label: string; icon: string }[]; detector: { label: string; icon: string } | null;
  does: string; cost_usd: number | null; cost_basis: string;
};
export type RoadmapView = { status: string; recommendations: Recommendation[]; counts: Record<string, number> };
/** The drawer's text (GET /v1/ambient/roadmap/{id} → detail): only shown on ⓘ. */
export type RecDetail = { why: string; evidence: Array<Evidence & { label: string }>; squad: string[] };

/** The reasons the engine accepts for a reject (roadmap.REJECT_CATEGORIES); each teaches its detector. */
export const REJECT_CATEGORIES = ["not_valuable", "wrong", "duplicate", "already_planned", "too_small", "other"] as const;
export const REJECT_WORDS: Record<(typeof REJECT_CATEGORIES)[number], { label: string; tip: string }> = {
  not_valuable: { label: "Not valuable", tip: "Worth less than it costs; the detector weighs it lower" },
  wrong: { label: "Wrong", tip: "The evidence does not say this" },
  duplicate: { label: "Duplicate", tip: "Already a ticket or recommendation" },
  already_planned: { label: "Already planned", tip: "On the roadmap already" },
  too_small: { label: "Too small", tip: "Raise this detector's threshold" },
  other: { label: "Other", tip: "Something else — say why" },
};

// ── labels: ≤ 4 words on a node, ≤ 3 on an edge (design §11.0 rule 2) ──────────────────────

export const NODE_WORDS = 4;
export const EDGE_WORDS = 3;

/** At most `n` words; a cut label ends in "…" so it never pretends to be whole. */
export function clampWords(label: string, n: number): string {
  const words = String(label ?? "").replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
  if (words.length <= n) return words.join(" ");
  return `${words.slice(0, n).join(" ")}…`;
}

const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const n = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const o = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const sn = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

const NODE_TYPES = new Set<NodeType>(["signal", "agent", "person", "policy", "squad", "boss", "action", "decision", "needs_you", "run", "outcome", "learning"]);

export function parseNode(w: unknown): FlowNode {
  const x = o(w);
  const type = (NODE_TYPES.has(x.type as NodeType) ? x.type : "boss") as NodeType;
  const ref = o(x.ref);
  return { id: s(x.id), type, label: clampWords(s(x.label), NODE_WORDS), stage: Math.max(0, Math.min(5, n(x.stage))),
           badges: a(x.badges).map((b) => s(b)).filter(Boolean),
           ref: Object.keys(ref).length ? (ref as Ref) : undefined,
           ...(sn(x.detail) ? { detail: s(x.detail) } : {}) };
}

export function parseEdge(w: unknown): FlowEdge {
  const x = o(w);
  const ref = o(x.ref);
  return { from: s(x.from), to: s(x.to), kind: s(x.kind) as EdgeKind, label: clampWords(s(x.label || x.kind), EDGE_WORDS),
           dissent: x.dissent === true, ref: Object.keys(ref).length ? (ref as Ref) : undefined };
}

export function parseBadges(w: unknown): ItemBadges {
  const x = o(w);
  return { problem_type: s(x.problem_type), task_class: s(x.task_class), outcome: s(x.outcome),
           needs_you: x.needs_you === true, autonomy_level: sn(x.autonomy_level), severity: sn(x.severity),
           cost_usd: n(x.cost_usd), dissent: x.dissent === true, overridden: x.overridden === true };
}

export function parseSummary(w: unknown): ItemSummary {
  const x = o(w);
  return { id: s(x.id), title: s(x.title), lifecycle: s(x.lifecycle), task_class: s(x.task_class),
           autonomy_level: sn(x.autonomy_level), severity: sn(x.severity), subject_ref: sn(x.subject_ref), app: sn(x.app),
           lease_owner: sn(x.lease_owner), needs_you_decision_id: sn(x.needs_you_decision_id), resolved_by: sn(x.resolved_by),
           created_at: sn(x.created_at), updated_at: sn(x.updated_at), resolved_at: sn(x.resolved_at) };
}

export function parseList(w: unknown): ListView {
  const x = o(w);
  const counts: Record<string, number> = {};
  for (const [k, v] of Object.entries(o(x.counts))) counts[k] = n(v);
  return {
    filter: s(x.filter), counts, next_before: sn(x.next_before),
    items: a(x.items).map((i) => ({ ...parseSummary(i), badges: parseBadges(o(i).badges),
                                    strip: a(o(i).strip).slice(0, 6).map((sn_) => {
                                      const p = parseNode({ ...o(sn_), id: "s" });
                                      return { type: p.type, label: p.label, badges: p.badges.slice(0, 2) };
                                    }),
                                    why: parseWaitReason(o(i).why), diagnosis: parseDiagnosis(o(i).diagnosis),
                                    run_id: sn(o(i).run_id) })),
  };
}

export function parseGraph(w: unknown): Graph {
  const x = o(w);
  const nodes = a(x.nodes).map(parseNode).filter((nd) => nd.id);
  const ids = new Set(nodes.map((nd) => nd.id));
  return { item: parseSummary(x.item), badges: parseBadges(x.badges), nodes,
           edges: a(x.edges).map(parseEdge).filter((e) => ids.has(e.from) && ids.has(e.to)),
           links: a(x.links).map((l) => ({ kind: s(o(l).kind), ref: s(o(l).ref), note: sn(o(l).note) })) };
}

// ── words for badges ────────────────────────────────────────────────────────────────────────

export const OUTCOME_WORDS: Record<string, { label: string; tone: "amber" | "blue" | "green" | "red" | "violet" | "grey" }> = {
  needs_you: { label: "Needs you", tone: "amber" },
  running: { label: "Running", tone: "blue" },
  resolved_by_agents: { label: "Resolved by agents", tone: "green" },
  resolved_by_person: { label: "Resolved by a person", tone: "green" },
  overridden: { label: "Overridden", tone: "violet" },
  failed: { label: "Failed", tone: "red" },
  dismissed: { label: "Dismissed", tone: "grey" },
};
export const outcomeWords = (o_: string) => OUTCOME_WORDS[o_] ?? { label: o_ || "Open", tone: "grey" as const };

export { LEVEL_MEANINGS, LEVEL_WORDS } from "./autonomy-levels";

export function money(usd: number | null | undefined): string {
  if (usd == null || !Number.isFinite(usd)) return "—";
  if (usd === 0) return "$0";
  if (usd < 0.01) return "<$0.01";
  return usd < 10 ? `$${usd.toFixed(2)}` : `$${Math.round(usd)}`;
}
export const microsUsd = (m: number) => m / 1_000_000;

export function minutesWords(m: number | null | undefined): string {
  if (m == null) return "—";
  if (m < 1) return `${Math.round(m * 60)}s`;
  if (m < 90) return `${Math.round(m)}m`;
  if (m < 48 * 60) return `${Math.round(m / 60)}h`;
  return `${Math.round(m / 1440)}d`;
}

/** The party a node stands for: `agent:sre` → role key `sre` (its face); a person → null. */
export function roleOfParty(who: string | null | undefined): string | null {
  const w = s(who);
  return w.startsWith("agent:") ? w.slice(6) : null;
}
export function partyOfNode(node: FlowNode): string | null {
  if (node.ref?.kind === "party" && node.ref.id) return node.ref.id;
  return node.id.startsWith("party:") ? node.id.slice(6) : null;
}

// ── URLs ────────────────────────────────────────────────────────────────────────────────────

export const AREA = "agent_ops";
const q = (params: Record<string, string | number | null | undefined>) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") u.set(k, String(v));
  return u.toString();
};
export const listPath = (projectId: string, filter: string, limit = 30) => `/v1/ambient/work-items?${q({ project_id: projectId, filter, limit })}`;
export const itemPath = (projectId: string, id: string) => `/v1/ambient/work-items/${encodeURIComponent(id)}?${q({ project_id: projectId })}`;
export const messagesPath = (projectId: string, id: string) => `/v1/ambient/work-items/${encodeURIComponent(id)}/messages?${q({ project_id: projectId })}`;
export const decisionPath = (projectId: string, actId: string) => `/v1/ambient/decisions/${encodeURIComponent(actId)}?${q({ project_id: projectId })}`;
export const signalsPath = (projectId: string) => `/v1/ambient/signals?${q({ project_id: projectId, limit: 500 })}`;
export const autonomyPath = (projectId: string) => `/v1/ambient/autonomy?${q({ project_id: projectId })}`;
export const settingsPath = (projectId: string) => `/v1/ambient/settings?${q({ project_id: projectId })}`;
export const scorecardPath = (projectId: string, days = 7) => `/v1/ambient/scorecard?${q({ project_id: projectId, days })}`;
export const roadmapPath = (projectId: string, status = "open", limit = 30) => `/v1/ambient/roadmap?${q({ project_id: projectId, status, limit })}`;
export const recPath = (projectId: string, id: string) => `/v1/ambient/roadmap/${encodeURIComponent(id)}?${q({ project_id: projectId })}`;
export const demoStatusPath = (projectId: string) => `/v1/ambient/demo/status?${q({ project_id: projectId })}`;

/** The console link to one item's flow chart (`act` resolves to its item on arrival). */
export function flowHref(target: { item?: string; act?: string }, search = typeof window === "undefined" ? "" : window.location.search): string {
  const now = new URLSearchParams(search);
  const out = new URLSearchParams();
  for (const k of ["org", "project", "aa_demo"]) { const v = now.get(k); if (v) out.set(k, v); }
  out.set("area", AREA);
  if (target.item) out.set("item", target.item);
  if (target.act) out.set("act", target.act);
  return `/?${out.toString()}`;
}

export function isDemo(search = typeof window === "undefined" ? "" : window.location.search): boolean {
  return new URLSearchParams(search).get("aa_demo") === "1";
}

// ── reads ───────────────────────────────────────────────────────────────────────────────────

export type ReadResult<T> = { ok: true; data?: T; notModified?: boolean; etag: string | null } | { ok: false; error: string; status?: number };

/** `missing` when the route is not on this install yet (404/405), so a screen can say so. */
export const MISSING = "missing";

export async function aaRead<T>(path: string, parse: (w: unknown) => T, etag: string | null = null, signal?: AbortSignal): Promise<ReadResult<T>> {
  if (isDemo()) {
    const body = demoAnswer(path);
    return body === undefined ? { ok: false, error: MISSING, status: 404 } : { ok: true, data: parse(body), etag: null };
  }
  try {
    const r = await fetch(path, { cache: "no-store", signal, headers: etag ? { "If-None-Match": etag } : undefined });
    if (r.status === 304) return { ok: true, notModified: true, etag };
    if (r.status === 404 || r.status === 405) return { ok: false, error: MISSING, status: r.status };
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = o(body).detail;
      return { ok: false, status: r.status, error: typeof d === "string" ? d : `BOSS answered ${r.status}` };
    }
    return { ok: true, data: parse(body), etag: r.headers.get("etag") };
  } catch (e) {
    if ((e as Error)?.name === "AbortError") return { ok: false, error: "aborted" };
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

/** For usePolledRead. */
export const polled = <T,>(path: string, parse: (w: unknown) => T) =>
  (etag: string | null, signal: AbortSignal) => aaRead(path, parse, etag, signal);

export const parseMessages = (w: unknown): Message[] => a(o(w).messages).map((m) => {
  const x = o(m);
  return { id: s(x.id), kind: s(x.kind), sender: s(x.sender), body: o(x.body), reply_to: sn(x.reply_to), created_at: sn(x.created_at) };
});
export const parseDecision = (w: unknown): Decision => {
  const x = o(o(w).decision ?? w);
  return {
    act_id: s(x.act_id), work_item_id: s(x.work_item_id), question: s(x.question), decided_by: s(x.decided_by),
    decider_kind: s(x.decider_kind), chosen: s(x.chosen),
    options: a(x.options).map((p) => ({ key: s(o(p).key), label: s(o(p).label) })),
    rejected: a(x.rejected).map((p) => ({ option: s(o(p).option), why: sn(o(p).why) ?? undefined })),
    rationale: s(x.rationale), evidence: a(x.evidence).map((e) => ({ kind: s(o(e).kind), ref: s(o(e).ref), note: sn(o(e).note) })),
    confidence: typeof x.confidence === "number" ? x.confidence : null, autonomy_level: sn(x.autonomy_level),
    task_class: sn(x.task_class), action: sn(x.action), reversibility: sn(x.reversibility), undo: sn(x.undo),
    consensus: Object.keys(o(x.consensus)).length ? (o(x.consensus) as Consensus) : null,
    cost_usd_micros: n(x.cost_usd_micros), outcome: sn(x.outcome), overrides_act_id: sn(x.overrides_act_id), created_at: sn(x.created_at),
  };
};
export const parseSignals = (w: unknown): Signal[] => a(o(w).signals).map((x) => {
  const y = o(x);
  return { id: s(y.id), kind: s(y.kind), title: s(y.title), severity: s(y.severity), source: s(y.source),
           occurrences: n(y.occurrences, 1), last_seen_at: sn(y.last_seen_at), status: sn(y.status) ?? undefined };
});
export const parseAutonomy = (w: unknown): AutonomyView => {
  const x = o(w);
  return {
    classes: a(x.classes).map((c) => {
      const y = o(c);
      return { task_class: s(y.task_class), title: s(y.title || y.task_class), level: s(y.level, "A0"), ceiling: s(y.ceiling, "A0"),
               max_ceiling: s(y.max_ceiling || y.ceiling, "A0"), default_level: s(y.default_level, "A0"), risk: s(y.risk, "low"),
               owner_role: sn(y.owner_role), level_name: s(y.level_name), suggested_level: sn(y.suggested_level), source: s(y.source) };
    }),
    levels: a(x.levels).map((l) => ({ level: s(o(l).level), name: s(o(l).name) })),
  };
};
const parseEnvelope = (w: unknown): Envelope => {
  const x = o(w);
  return { cap_usd_micros: n(x.cap_usd_micros), spent_usd_micros: n(x.spent_usd_micros), held_usd_micros: n(x.held_usd_micros),
           available_usd_micros: n(x.available_usd_micros), resets: s(x.resets, "00:00 UTC") };
};
const parseStop = (w: unknown): StopState => {
  const x = o(w);
  return { stopped: x.stopped === true, scope: sn(x.scope), by: sn(x.by), at: sn(x.at), reason: s(x.reason) };
};
export const parseMode = (w: unknown): ModeState => {
  const x = o(w);
  const m = s(x.mode);
  const src = s(x.source);
  return {
    mode: (m === "propose" || m === "route" ? m : "off"),
    source: (src === "env" || src === "console" ? src : "default"),
    pinned: x.pinned === true, by: sn(x.by), at: sn(x.at),
  };
};
export const parseSettings = (w: unknown): SettingsView => {
  const x = o(w);
  return { settings: o(x.settings), envelope: parseEnvelope(x.envelope), stop: parseStop(x.stop), mode: parseMode(x.mode) };
};
const parseSum = (w: unknown): Summary => {
  const x = o(w);
  return { n: n(x.n), median: typeof x.median === "number" ? x.median : null, p90: typeof x.p90 === "number" ? x.p90 : null };
};
export const parseScorecard = (w: unknown): Scorecard => {
  const x = o(w);
  const cost = o(x.cost);
  const ended: Record<string, number> = {};
  for (const [k, v] of Object.entries(o(x.items_ended))) ended[k] = n(v);
  const guards: Record<string, number> = {};
  for (const [k, v] of Object.entries(o(x.guards))) guards[k] = n(v);
  return {
    days: n(x.days, 7),
    decisions_per_day: a(x.decisions_per_day).map((d) => {
      const y = o(d);
      return { day: s(y.day), a0: n(y.a0), a1: n(y.a1), a2: n(y.a2), a3: n(y.a3), agent: n(y.agent), policy: n(y.policy),
               squad: n(y.squad), person: n(y.person), total: n(y.total) };
    }),
    by_class: a(x.by_class).map((c) => {
      const y = o(c);
      return { task_class: s(y.task_class), decided: n(y.decided), accepted_pct: n(y.accepted_pct),
               overridden_pct: n(y.overridden_pct), reverted_pct: n(y.reverted_pct) };
    }),
    time_to_human_decision_minutes: parseSum(x.time_to_human_decision_minutes), mttr_minutes: parseSum(x.mttr_minutes),
    incident_mttr_minutes: parseSum(x.incident_mttr_minutes), items_ended: ended,
    noise_rate: typeof x.noise_rate === "number" ? x.noise_rate : null,
    cost: { spent_usd: n(cost.spent_usd), per_resolved_item_usd: typeof cost.per_resolved_item_usd === "number" ? cost.per_resolved_item_usd : null,
            envelope_today: parseEnvelope(cost.envelope_today) },
    guards, stopped: parseStop(x.stopped), outcomes: parseOutcomes(x.outcomes),
  };
};
const pctOrNull = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
export const parseOutcomes = (w: unknown): Outcomes | null => {
  if (!w || typeof w !== "object") return null;
  const x = o(w);
  const r = o(x.recommendations);
  const share = o(o(x.ambient_share).work_items);
  return {
    decided: n(x.decided), with_outcome: n(x.with_outcome), merged: n(x.merged), reverted: n(x.reverted), overridden: n(x.overridden),
    acceptance_pct: pctOrNull(x.acceptance_pct), revert_pct: pctOrNull(x.revert_pct), override_pct: pctOrNull(x.override_pct),
    roadmap: { shown: n(r.shown), accepted: n(r.accepted), rejected: n(r.rejected), ignored: n(r.ignored), recurred: n(r.recurred),
               acceptance_pct: pctOrNull(r.acceptance_pct) },
    boss_started: { boss: n(share.boss), all: n(share.all), pct: pctOrNull(share.pct) },
  };
};
/** The engine's numbers as three dots (value 0-100; effort 1-13 units; risk 1-1.5). */
export const valueLevel = (v: number): Level3 => (v >= 70 ? "high" : v >= 40 ? "medium" : "low");
export const effortLevel = (u: number): Level3 => (u <= 2 ? "low" : u <= 5 ? "medium" : "high");
export const riskLevel = (r: number): Level3 => (r < 1.1 ? "low" : r < 1.3 ? "medium" : "high");

const parseStrip = (w: unknown): StripNode[] => a(w).slice(0, 6).map((x) => {
  const p = parseNode({ ...o(x), id: "s" });
  return { type: p.type, label: p.label, badges: p.badges.slice(0, 2), ...(typeof o(x).icon === "string" ? { icon: String(o(x).icon) } : {}) };
});

const parseCard = (v: unknown): RecCard | null => {
  if (!v || typeof v !== "object") return null;
  const c = o(v);
  const node = (x: unknown) => ({ label: s(o(x).label), icon: s(o(x).icon) });
  return {
    evidence: a(c.evidence).map(node).filter((e) => e.label).slice(0, 2),
    detector: c.detector && s(o(c.detector).label) ? node(c.detector) : null,
    does: s(c.does), cost_usd: typeof c.cost_usd === "number" && c.cost_usd > 0 ? c.cost_usd : null, cost_basis: s(c.cost_basis),
  };
};
export const parseRec = (w: unknown): Recommendation => {
  const y = o(w);
  const b = o(y.badges);
  const rank = typeof y.rank === "number" ? y.rank : null;
  return {
    id: s(y.id || y.work_item_id), title: s(y.title), kind: s(y.kind, "hardening"), kindLabel: s(b.kind || y.kind),
    detector: s(y.detector), detectorTitle: s(b.detector || y.detector), proposed_shape: s(y.proposed_shape || b.shape, "ticket"),
    status: s(y.status || b.status, "open"), rank, score: n(y.score, n(b.score)),
    value: valueLevel(n(b.value)), effort: effortLevel(n(b.effort, 1)), risk: riskLevel(n(b.risk, 1)),
    evidence: n(b.evidence), sources: n(b.sources), single_source: b.single_source === true, resurfaced: b.resurfaced === true,
    snoozed_until: sn(y.snoozed_until), strip: parseStrip(y.strip),
    cost_usd: typeof b.cost_usd === "number" ? b.cost_usd : null, squad: a(y.squad).map((q) => s(q)).filter(Boolean), why: s(y.why), subject: s(y.subject),
    card: parseCard(y.card),
    work_id: s(o(y.artifact).workflow_run_id || o(y.artifact).code_task_run_id) || undefined,
  };
};

export const parseRoadmap = (w: unknown): RoadmapView => {
  const x = o(w);
  const counts: Record<string, number> = {};
  for (const [k, v] of Object.entries(o(x.counts))) counts[k] = n(v);
  return { status: s(x.status, "open"), recommendations: a(x.items).map(parseRec), counts };
};

export const evidenceLabel = (e: { kind: string; ref: string; note?: string | null }): string =>
  clampWords(e.note || `${e.kind.replace(/_/g, " ")} ${e.ref}`, 3);

export const parseRecDetail = (w: unknown): RecDetail => {
  const d = o(o(w).detail);
  return {
    why: s(d.why),
    evidence: a(d.evidence).map((e) => {
      const ev = { kind: s(o(e).kind), ref: s(o(e).ref), note: sn(o(e).note) };
      return { ...ev, label: evidenceLabel(ev) };
    }),
    squad: a(d.squad).map((p) => s(p)).filter(Boolean),
  };
};

/** Aggregate acceptance across classes, weighted by decisions (null with none decided). */
export function acceptance(card: Scorecard | null): number | null {
  if (!card) return null;
  const decided = card.by_class.reduce((t, c) => t + c.decided, 0);
  if (!decided) return null;
  return card.by_class.reduce((t, c) => t + c.accepted_pct * c.decided, 0) / decided;
}

// ── writes ──────────────────────────────────────────────────────────────────────────────────

export async function aaWrite(path: string, body: Record<string, unknown>, label: string, method: "POST" | "PUT" = "POST"): Promise<WriteResult> {
  if (isDemo()) return demoWrite(path, body);
  return obsWrite(path, body, { scope: `aa:${path}`, label }, method);
}

export const takeOver = (projectId: string, id: string, reason = "") =>
  aaWrite(`/v1/ambient/work-items/${encodeURIComponent(id)}/take-over`, { project_id: projectId, reason }, "Take over");
export const dismissItem = (projectId: string, id: string, reason: string) =>
  aaWrite(`/v1/ambient/work-items/${encodeURIComponent(id)}/dismiss`, { project_id: projectId, reason }, "Dismiss");
export const overrideDecision = (projectId: string, actId: string, chosen: string, rationale: string) =>
  aaWrite(`/v1/ambient/decisions/${encodeURIComponent(actId)}/override`, { project_id: projectId, chosen, rationale }, "Override");
export const sweepNow = (projectId: string) => aaWrite("/v1/ambient/signals/sweep", { project_id: projectId }, "Check now");
export const setLevel = (projectId: string, taskClass: string, level: string, reason = "") =>
  aaWrite(`/v1/ambient/autonomy/${encodeURIComponent(taskClass)}`, { project_id: projectId, level, reason }, "Set level", "PUT");
export const stopAgents = (projectId: string, reason: string, everywhere = false) =>
  everywhere ? aaWrite("/v1/ambient/stop-all", { reason }, "Stop all")
             : aaWrite("/v1/ambient/stop", { project_id: projectId, reason }, "Stop");
export const resumeAgents = (projectId: string, reason = "", everywhere = false) =>
  everywhere ? aaWrite("/v1/ambient/resume-all", { reason }, "Resume all")
             : aaWrite("/v1/ambient/resume", { project_id: projectId, reason }, "Resume");
/** Turn the always-on agents on or off for the install (autonomy:manage; 409 while .env pins it). */
export const setAlwaysOn = (mode: AlwaysOnMode, reason = "") => aaWrite("/v1/ambient/mode", { mode, reason }, "Always-on", "PUT");
export type AcceptAs = "ticket" | "epic" | "prd";
export type RejectCategory = (typeof REJECT_CATEGORIES)[number];
const recWrite = (id: string, verb: string) => `/v1/ambient/roadmap/${encodeURIComponent(id)}/${verb}`;
export const acceptRec = (projectId: string, id: string, as: AcceptAs, note = "") =>
  aaWrite(recWrite(id, "accept"), { project_id: projectId, as, ...(note ? { note } : {}) }, "Accept");
export const rejectRec = (projectId: string, id: string, reason: string, category: RejectCategory = "other") =>
  aaWrite(recWrite(id, "reject"), { project_id: projectId, reason, category }, "Reject");
/** Not now: hidden until `until` (now + days, an ISO datetime; the engine allows ≤ 180 days). */
export const snoozeRec = (projectId: string, id: string, days = 14, nowMs = Date.now(), reason = "") =>
  aaWrite(recWrite(id, "snooze"), { project_id: projectId, until: new Date(nowMs + days * 86_400_000).toISOString(), ...(reason ? { reason } : {}) }, "Not now");

// ── the demo loader (a demo project only; autonomy:manage) ─────────────────────────────────

export type DemoLoader = "morning" | "order_api_incident" | "squad_cve" | "roadmap";
export type DemoStatus = { available: boolean; reason: string; loaders: string[] };
export const parseDemoStatus = (w: unknown): DemoStatus => {
  const x = o(w);
  return { available: x.available === true, reason: s(x.reason), loaders: a(x.loaders).map((l) => s(l)).filter(Boolean) };
};
export type DemoLoad = { loaded: string[]; skipped: Record<string, string> };
export const parseDemoLoad = (w: unknown): DemoLoad => {
  const x = o(w);
  const skipped: Record<string, string> = {};
  for (const [k, v] of Object.entries(o(x.skipped))) skipped[k] = s(v);
  return { loaded: Object.entries(o(x.loaded)).filter(([, v]) => v != null).map(([k]) => k), skipped };
};
export const loadDemo = (projectId: string) => aaWrite("/v1/ambient/demo/load", { project_id: projectId }, "Load demo");
