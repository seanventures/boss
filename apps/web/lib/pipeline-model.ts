/**
 * THE PIPELINE VIEW'S MODEL (design §4.7): a workflow spec, read as the numbered stage diagram a
 * reference design draws — who runs each stage, what it reads and writes, whether it ran — in words.
 *
 * Pure: the spec, the palette (capabilities, role names, graph-step descriptions), the
 * connected outside agents and the last run's trace in; stages out. Nothing here fetches, and
 * nothing here shows an id: every name comes from a title, a label or a humanised key.
 */
import type { Palette, Spec, SpecStep } from "./workflow-editor-types";
import type { RunContext, StepRunTrace } from "./workflow-api";
import { humanise } from "./step-view";

type Obj = Record<string, unknown>;
const asObj = (value: unknown): Obj =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};
const str = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

/** A Knowledge or Context Graph kind a contract names. */
export type ContractKind = { kind: string; required?: boolean; via?: string; decision_type?: string };
/** An external system a contract calls, over MCP. */
export type ContractCall = { mcp: string; tool: string };
/** A Context Graph edge between two kinds a contract names; `from === to` links a kind to itself. */
export type ContractLink = { from: string; to: string; rel: string };
/** A decision, policy or open question with its ID ("CTX-1", "CTX-P1", "HIS-Q1"). */
export type DesignNote = { id: string; text: string };
/** One of the agent's numbered internal steps (`contract.steps`). */
export type AgentStepDef = {
  n: number; name: string; reads: string[]; writes: string[]; calls: ContractCall[]; explain?: string;
};
/** The design cards a contract carries (`contract.design`). */
export type StageDesignCards = {
  status?: DesignStatus; reference?: string; underRevision?: string;
  /** The running example the design is read against ("DQX-0588 v2 on COB 2026-10-05"). */
  example?: string;
  decisions: DesignNote[]; policy: DesignNote[]; openQuestions: DesignNote[];
  appliedIn?: { kg?: string; cg?: string; items: string[] };
};
/** A step's `contract` block (§4.1), whatever of it the step has. */
export type StageContract = {
  reads: ContractKind[]; writes: ContractKind[]; calls: ContractCall[];
  trigger?: string; handoff?: string; human_review?: string; llm?: string;
  policy?: [string, string][]; decisions?: string[]; open_questions?: string[];
  /** The step the handoff leads to, by id, to point "To <next stage>" at. */
  handoffTo?: string;
  /** The step that starts this one, by id: the design view's entry pill. */
  invokedBy?: string;
  /** Context Graph edges the agent writes between kinds it names (CORRELATED_WITH…). */
  links?: ContractLink[];
  steps?: AgentStepDef[];
  design?: StageDesignCards;
};

/** A stage design's review state (design §4.6), as the design legend names it. */
export type DesignStatus = "locked" | "in_review" | "not_reviewed" | "human_step" | "shared_service";
export const DESIGN_STATUSES: DesignStatus[] = ["locked", "in_review", "not_reviewed", "human_step", "shared_service"];
export const DESIGN_LABEL: Record<DesignStatus, string> = {
  locked: "Locked", in_review: "In review", not_reviewed: "Not yet reviewed",
  human_step: "Human step", shared_service: "Shared service",
};
/** What a stage's design is now: from its review record, else what its contract declares. */
export type StageDesignState = {
  status: DesignStatus;
  reference: string;
  underRevision?: string;
  /** The version a person locked, while a later one is in review. */
  locked?: { reference: string; by?: string; at?: string };
  can: { send_for_review: boolean; lock: boolean; revise: boolean };
  source: "review" | "contract" | "default";
};
/** One kind's words, domain colour and stored layer (the designs view's `kinds`). */
export type KindMeta = { label: string; color: string | null; layer: string };
/** `view=designs`: every stage's design, the graph versions and the kinds' colours. */
export type DesignsInfo = {
  stages: Record<string, StageDesignState>;
  /** The Knowledge Graph version the bands name: the version the designs are written against
   *  (the pack's reference) when declared, else the project's live version. One number, so a
   *  band never says v1 while the design cards say KG v16/v17. */
  kgVersion: number | null;
  cgVersion: number | null;
  kinds: Record<string, KindMeta>;
};

export type RunsAsTone = "deterministic" | "agent" | "person" | "outside" | "control" | "builtin";
export type StageStatus =
  | "succeeded" | "running" | "waiting" | "pending" | "failed" | "skipped" | "cancelled" | "not_run";

export type PipelineStage = {
  id: string;
  kind: string;
  /** 1… for a stage that does work; null for a loop or lane frame, which only groups. */
  number: number | null;
  title: string;
  /** One plain line: what this stage does. */
  description: string;
  runsAs: { label: string; tone: RunsAsTone; tip: string };
  status: StageStatus;
  contract: StageContract | null;
  /** The loop or lane frame this stage sits in, by id. */
  group?: string;
  /** A step that only marks where a branch ends or passes on: drawn on its condition, unnumbered. */
  outcome?: true;
  /** A frame's members, in order. */
  members?: string[];
  /** A frame's own words: "Repeats Context enrichment up to 2× while it reports missing kinds". */
  frameNote?: string;
  maxIterations?: number;
  /** KG P6b: the captain switcher's state. */
  agent?: string;
  captainKind?: "agent" | "outside_agent";
  endpointHost?: string;
  /** What the last run recorded for this stage, when it ran. */
  trace?: StepRunTrace;
  /** Its design status and reference (design §4.6). */
  design: StageDesignState;
  /** Where the diagram draws it: the main flow, the shared-services row, or a side path. */
  placement: "main" | "shared" | "side";
  /** A side path's area ("Rule authoring"), from its spec's `side_path`. */
  sideLabel?: string;
  /** How the flow reaches a side path ("From Lanes: False Positive lane: rule proposal"). */
  sideLink?: string;
  /** The stage before and after it on the flow, by title: the read/write view's anchors. */
  from?: string;
  to?: string;
};

export type OutsideAgentRef = { id: string; display_name: string; endpoint_host: string };

const CONTROL = new Set(["bounded_loop", "for_each", "parallel", "condition", "join"]);

const STATUS: Record<string, StageStatus> = {
  succeeded: "succeeded", completed: "succeeded", done: "succeeded",
  running: "running", claimed: "running", in_progress: "running",
  waiting: "waiting", blocked: "waiting", paused: "waiting",
  pending: "pending", queued: "pending", ready: "pending",
  failed: "failed", error: "failed", dead: "failed",
  skipped: "skipped", not_applicable: "skipped",
  cancelled: "cancelled", canceled: "cancelled",
};

export const STATUS_LABEL: Record<StageStatus, string> = {
  succeeded: "Succeeded", running: "Running", waiting: "Waiting", pending: "Pending",
  failed: "Failed", skipped: "Skipped", cancelled: "Cancelled", not_run: "Not run yet",
};

/** "agent_decision", "AgentDecision" → "Agent decision"; "CDE" stays "CDE". */
export function kindLabel(kind: string): string {
  const raw = String(kind ?? "").trim();
  if (!raw) return "";
  const spaced = raw.replace(/[_.-]+/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").trim();
  const words = spaced.split(/\s+/).map((w, i) => {
    if (w.length > 1 && w === w.toUpperCase()) return w; // an acronym
    const lower = w.toLowerCase();
    return i === 0 ? lower[0].toUpperCase() + lower.slice(1) : lower;
  });
  return words.join(" ");
}

/** The same kind however it is spelled, for comparing a contract with a run. */
export function kindKey(kind: string): string {
  return String(kind ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

/** "incident_id" → "incident ID"; "rca_verdict" → "RCA verdict": a carried field in words. */
function fieldWords(field: string): string {
  return String(field ?? "").split(/[_\s]+/).filter(Boolean).map((w) => {
    const lower = w.toLowerCase();
    return ["id", "rca", "cde", "kg", "cg", "mcp", "service-desk", "cob", "dq"].includes(lower) ? lower.toUpperCase() : lower;
  }).join(" ");
}

/** "a, b and c". */
function andList(items: string[]): string {
  return items.length <= 1 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** A policy value in words: "standard_profile" → "Standard profile"; numbers and text kept. */
function policyValue(value: unknown): string {
  if (value && typeof value === "object") {
    return Array.isArray(value) ? value.map(policyValue).join(", ")
      : Object.entries(value as Obj).map(([k, v]) => `${humanise(k).toLowerCase()} ${policyValue(v)}`).join(", ");
  }
  const text = String(value);
  return /^[a-z0-9]+(_[a-z0-9]+)+$/.test(text) ? humanise(text) : text;
}

function contractOf(step: SpecStep, titleOf: (id: string) => string = humanise): StageContract | null {
  const raw = asObj(step.contract);
  if (!Object.keys(raw).length) return null;
  const kinds = (value: unknown): ContractKind[] => list(value).map((v) => {
    const row = typeof v === "string" ? { kind: v } : asObj(v);
    return {
      kind: str(row.kind),
      ...(row.required === true ? { required: true } : {}),
      ...(str(row.via) ? { via: str(row.via) } : {}),
      ...(str(row.decision_type) ? { decision_type: str(row.decision_type) } : {}),
    };
  }).filter((k) => k.kind);
  const calls = list(raw.calls).map((v) => {
    const row = asObj(v);
    return { mcp: str(row.mcp) || str(row.server), tool: str(row.tool) };
  }).filter((c) => c.mcp || c.tool);
  const text = (value: unknown): string | undefined => {
    if (typeof value === "string") return value.trim() || undefined;
    const row = asObj(value);
    if (!Object.keys(row).length) return undefined;
    // The handoff in a full sentence: the contract's own words, else composed from the stage
    // it returns to (by its display name) and what it carries.
    if (str(row.text)) return str(row.text);
    const to = str(row.to);
    const next = str(row.next);
    const carries = andList(list(row.carries).map((c) => fieldWords(String(c))).filter(Boolean));
    if (!to && !next) return carries ? `Leaves the ${carries} for what comes next.` : undefined;
    const target = titleOf(to || next);
    let sentence = carries ? `Hands the ${carries} to ${target}.` : `Hands on to ${target}.`;
    if (to && next && next !== to) sentence += ` ${target} then starts ${titleOf(next)}.`;
    return sentence;
  };
  const policy = Object.entries(asObj(raw.policy))
    .map(([k, v]) => [humanise(k), policyValue(v)] as [string, string]);
  const strs = (value: unknown) => list(value).map((v) => String(v).trim()).filter(Boolean);
  const steps = list(raw.steps).map((v) => {
    const row = asObj(v);
    const names = (value: unknown) => list(value).map((k) => String(k).trim()).filter(Boolean);
    return {
      n: Number(row.n) || 0, name: str(row.name),
      reads: names(row.reads), writes: names(row.writes),
      calls: list(row.calls).map((c) => ({ mcp: str(asObj(c).mcp), tool: str(asObj(c).tool) })).filter((c) => c.mcp),
      ...(str(row.explain) ? { explain: str(row.explain) } : {}),
    };
  }).filter((s) => s.n > 0 && s.name).sort((a, b) => a.n - b.n);
  const handoffTo = str(asObj(raw.handoff).next) || str(asObj(raw.handoff).to);
  const links = list(raw.links).map((v) => asObj(v))
    .map((l) => ({ from: str(l.from), to: str(l.to), rel: str(l.rel) })).filter((l) => l.from && l.to && l.rel);
  return {
    reads: kinds(raw.reads), writes: kinds(raw.writes), calls,
    trigger: text(raw.trigger), handoff: text(raw.handoff), human_review: text(raw.human_review),
    llm: typeof raw.llm === "string" ? raw.llm : undefined,
    ...(policy.length ? { policy } : {}),
    ...(strs(raw.decisions).length ? { decisions: strs(raw.decisions) } : {}),
    ...(strs(raw.open_questions).length ? { open_questions: strs(raw.open_questions) } : {}),
    ...(handoffTo ? { handoffTo } : {}),
    ...(str(raw.invoked_by) ? { invokedBy: str(raw.invoked_by) } : {}),
    ...(links.length ? { links } : {}),
    ...(steps.length ? { steps } : {}),
    ...(raw.design && typeof raw.design === "object" ? { design: designCardsOf(asObj(raw.design)) } : {}),
  };
}

const notes = (value: unknown): DesignNote[] => list(value)
  .map((v) => ({ id: str(asObj(v).id), text: str(asObj(v).text) })).filter((n) => n.id || n.text);

function designCardsOf(raw: Obj): StageDesignCards {
  const applied = asObj(raw.applied_in);
  const version = (v: unknown) => (typeof v === "number" ? `v${v}` : str(v));
  const status = str(raw.status) as DesignStatus;
  return {
    ...(DESIGN_STATUSES.includes(status) ? { status } : {}),
    ...(str(raw.reference) ? { reference: str(raw.reference) } : {}),
    ...(str(raw.under_revision) ? { underRevision: str(raw.under_revision) } : {}),
    ...(str(raw.example) ? { example: str(raw.example) } : {}),
    decisions: notes(raw.decisions), policy: notes(raw.policy), openQuestions: notes(raw.open_questions),
    ...(Object.keys(applied).length ? { appliedIn: {
      ...(version(applied.kg) ? { kg: version(applied.kg) } : {}),
      ...(version(applied.cg) ? { cg: version(applied.cg) } : {}),
      items: list(applied.items).map((i) => String(i).trim()).filter(Boolean),
    } } : {}),
  };
}

/** A stage's design state: its review record when there is one, else what its contract declares,
 *  else a person's step is a human step and every other stage is not yet reviewed. */
function designOf(step: SpecStep, contract: StageContract | null, record: StageDesignState | undefined,
                  isPerson: boolean): StageDesignState {
  if (record) return record;
  const declared = contract?.design;
  const status: DesignStatus = declared?.status ?? (isPerson ? "human_step" : "not_reviewed");
  const reviewable = !!contract && ["not_reviewed", "in_review", "locked"].includes(status);
  return {
    status, reference: declared?.reference ?? "",
    ...(declared?.underRevision ? { underRevision: declared.underRevision } : {}),
    ...(status === "locked" ? { locked: { reference: declared?.reference ?? "" } } : {}),
    can: { send_for_review: reviewable && status === "not_reviewed", lock: reviewable && status === "in_review",
           revise: reviewable && status === "locked" },
    source: declared?.status ? "contract" : "default",
  };
}

/** The designs view (`view=designs`) as the pipeline reads it. */
export function designsFrom(body: unknown): DesignsInfo {
  const root = asObj(body);
  const stages: Record<string, StageDesignState> = {};
  for (const [id, raw] of Object.entries(asObj(root.stages))) {
    const row = asObj(raw);
    const status = str(row.status) as DesignStatus;
    if (!DESIGN_STATUSES.includes(status)) continue;
    const locked = asObj(row.locked);
    const can = asObj(row.can);
    stages[id] = {
      status, reference: str(row.reference),
      ...(str(row.under_revision) ? { underRevision: str(row.under_revision) } : {}),
      ...(Object.keys(locked).length ? { locked: {
        reference: str(locked.reference),
        ...(str(locked.locked_by) ? { by: str(locked.locked_by) } : {}),
        ...(str(locked.locked_at) ? { at: str(locked.locked_at) } : {}),
      } } : {}),
      can: { send_for_review: can.send_for_review === true, lock: can.lock === true, revise: can.revise === true },
      source: (["review", "contract", "default"].includes(str(row.source)) ? str(row.source) : "default") as StageDesignState["source"],
    };
  }
  const graph = asObj(root.graph);
  const kinds: Record<string, KindMeta> = {};
  for (const [kind, raw] of Object.entries(asObj(root.kinds))) {
    const row = asObj(raw);
    kinds[kind] = { label: str(row.label) || kindLabel(kind), color: str(row.color) || null, layer: str(row.layer) };
  }
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  return { stages, kgVersion: num(graph.kg_reference) ?? num(graph.kg_version), cgVersion: num(graph.cg_version), kinds };
}

/** A role's display name alone ("Compliance Officer"), or its key made readable. */
export function roleName(roleKey: string, palette: Palette | null): string {
  const key = String(roleKey ?? "").trim();
  return palette?.roleNames?.[key]?.name || palette?.agents.find((a) => a.role_key === key)?.name
    || key.split(/[_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}

/** "Tally · Compliance Analyst", or the role's name, or its key made readable. */
export function roleLabel(roleKey: string, palette: Palette | null): string {
  const key = String(roleKey ?? "").trim();
  if (!key) return "";
  const named = palette?.roleNames?.[key];
  const name = named?.name || palette?.agents.find((a) => a.role_key === key)?.name || "";
  if (named?.callsign && name) return `${named.callsign} · ${name}`;
  if (name) return name;
  return key.split(/[_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}

/** Where the flow goes after a step: its `next`, and a condition's two branches. */
const nexts = (step: SpecStep): string[] => {
  const own = (Array.isArray(step.next) ? step.next : step.next ? [step.next] : []).map(String);
  if (step.kind !== "condition") return own;
  const branches = [str((step as Obj).on_true), str((step as Obj).on_false)].filter(Boolean);
  return [...branches, ...own.filter((n) => !branches.includes(n))];
};

/** A step that does no work and only marks how the flow ends or passes on ("Attached to an
 *  open incident — stop"): a zero wait with nothing bound. It is drawn as the outcome of the
 *  condition that leads to it, never as a numbered stage. */
export function isOutcome(step: SpecStep): boolean {
  if (step.kind !== "timer") return false;
  const wait = Number((step as Obj).wait_seconds ?? 0) || 0;
  const binding = asObj(step.binding);
  return wait === 0 && !str(binding.capability) && !Object.keys(asObj(binding.inputs)).length;
}
const bodiesOf = (step: SpecStep): string[] => {
  const body = (step as Obj).body;
  return (Array.isArray(body) ? body : body ? [body] : []).map(String);
};

/** Display order: the main path from the entry, each loop or lane frame followed at once by
 *  its members, then anything the path never reached. */
function displayOrder(spec: Spec): { order: string[]; groupOf: Map<string, string> } {
  const steps = spec.steps ?? [];
  const byId = new Map(steps.map((s) => [s.id, s]));
  const groupOf = new Map<string, string>();
  for (const s of steps) {
    if (s.kind !== "bounded_loop" && s.kind !== "for_each") continue;
    // A frame holds its body and the body's own chain, until the chain leaves it.
    const queue = bodiesOf(s);
    while (queue.length) {
      const id = queue.shift()!;
      if (groupOf.has(id) || !byId.has(id) || id === s.id) continue;
      groupOf.set(id, s.id);
      queue.push(...nexts(byId.get(id)!));
    }
  }
  const incoming = new Set(steps.flatMap((s) => nexts(s)));
  const entry = str(spec.entry_step) && byId.has(str(spec.entry_step)) ? str(spec.entry_step)
    : steps.find((s) => !groupOf.has(s.id) && !incoming.has(s.id))?.id ?? steps[0]?.id;
  const order: string[] = [];
  const seen = new Set<string>();
  const place = (id: string) => {
    if (seen.has(id) || !byId.has(id)) return;
    seen.add(id);
    order.push(id);
    const step = byId.get(id)!;
    if (step.kind === "bounded_loop" || step.kind === "for_each") {
      for (const member of steps.filter((s) => groupOf.get(s.id) === id)) {
        seen.add(member.id);
        order.push(member.id);
      }
    }
  };
  // RUN ORDER: the top-level steps the entry reaches, each placed only once every step that
  // leads to it is placed (a stage after a branch that rejoins comes after both arms), ties in
  // the spec's own order. A frame stands for its members; a step a member leads out to follows
  // the frame.
  const top = (id: string) => groupOf.get(id) ?? id;
  const outOf = (id: string): string[] => {
    const step = byId.get(id);
    if (!step) return [];
    const own = [...nexts(step), ...(step.kind === "parallel" ? list((step as Obj).branches).map(String) : [])];
    const fromMembers = steps.filter((s) => groupOf.get(s.id) === id).flatMap((s) => nexts(s));
    return [...new Set([...own, ...fromMembers].filter((t) => byId.has(t)).map(top))].filter((t) => t !== id);
  };
  const onFlow = new Set<string>();
  const walk = entry ? [top(entry)] : [];
  while (walk.length) {
    const id = walk.shift()!;
    if (onFlow.has(id)) continue;
    onFlow.add(id);
    walk.push(...outOf(id));
  }
  const preds = new Map<string, Set<string>>([...onFlow].map((id) => [id, new Set<string>()]));
  for (const id of onFlow) for (const t of outOf(id)) if (onFlow.has(t)) preds.get(t)!.add(id);
  const index = new Map(steps.map((s, i) => [s.id, i]));
  const pending = [...onFlow].sort((a, b) => (index.get(a) ?? 0) - (index.get(b) ?? 0));
  while (pending.length) {
    // The first ready step in spec order; on a cycle, the earliest step left.
    const ready = pending.findIndex((id) => [...preds.get(id)!].every((p) => seen.has(p)));
    const [id] = pending.splice(ready >= 0 ? ready : 0, 1);
    place(id);
  }
  for (const s of steps) if (!groupOf.has(s.id)) place(s.id);
  for (const s of steps) if (!seen.has(s.id)) { seen.add(s.id); order.push(s.id); }
  return { order, groupOf };
}

/** "steps.context_enrichment.output.missing_kinds" → ["context_enrichment", "missing_kinds"]. */
function pathParts(path: string): [string, string] {
  const parts = String(path ?? "").split(".");
  const at = parts.indexOf("steps");
  if (at >= 0 && parts.length > at + 1) {
    const field = parts.slice(parts.indexOf("output") >= 0 ? parts.indexOf("output") + 1 : at + 2).join(" ");
    return [parts[at + 1], field];
  }
  return ["", parts[parts.length - 1] ?? ""];
}

function singular(words: string): string {
  const w = words.trim();
  if (/ies$/.test(w)) return w.replace(/ies$/, "y");
  if (/(ss|us)$/.test(w)) return w;
  return w.replace(/s$/, "");
}

/** The steps reached from the entry along `next`, branches and frame bodies: the main flow. */
function reachable(spec: Spec): Set<string> {
  const steps = spec.steps ?? [];
  const byId = new Map(steps.map((s) => [s.id, s]));
  const incoming = new Set(steps.flatMap((s) => [...nexts(s), ...bodiesOf(s),
    ...(s.kind === "parallel" ? list((s as Obj).branches).map(String) : [])]));
  const entry = str(spec.entry_step) && byId.has(str(spec.entry_step)) ? str(spec.entry_step)
    : steps.find((s) => !incoming.has(s.id))?.id ?? steps[0]?.id;
  const seen = new Set<string>();
  const queue = entry ? [entry] : [];
  while (queue.length) {
    const id = queue.shift()!;
    if (seen.has(id) || !byId.has(id)) continue;
    seen.add(id);
    const step = byId.get(id)!;
    queue.push(...nexts(step), ...bodiesOf(step),
      ...(step.kind === "parallel" ? list((step as Obj).branches).map(String) : []));
  }
  return seen;
}

export function buildPipeline(spec: Spec, palette: Palette | null,
                              outsideAgents: OutsideAgentRef[] = [],
                              run: RunContext | null = null,
                              designs: DesignsInfo | null = null): PipelineStage[] {
  const steps = spec.steps ?? [];
  const byId = new Map(steps.map((s) => [s.id, s]));
  const { order, groupOf } = displayOrder(spec);
  const onMain = reachable(spec);
  const titleOf = (id: string): string => {
    const step = byId.get(id);
    if (!step) return humanise(id);
    const own = str(step.title);
    if (own) return own;
    const binding = asObj(step.binding);
    const cap = palette?.capabilities.find((c) => c.key === binding.capability);
    if (cap?.label) return cap.label;
    const graph = palette?.graphSteps?.[step.id];
    if (step.kind === "deterministic" && graph?.label) return graph.label;
    return humanise(id);
  };

  // How the flow reaches each side path, in the spec's own words ("False Positive lane: rule
  // proposal", from Lanes): `pipeline.side_paths`.
  const sideLinks = new Map<string, string>();
  for (const raw of list(asObj(spec.pipeline).side_paths)) {
    const row = asObj(raw);
    const to = str(row.to);
    if (!to || !str(row.label)) continue;
    const from = str(row.from);
    sideLinks.set(to, `${from && byId.has(from) ? `From ${titleOf(from)}: ` : ""}${str(row.label)}`);
  }

  const stages: PipelineStage[] = [];
  let n = 0;
  for (const id of order) {
    const step = byId.get(id)!;
    const binding = asObj(step.binding);
    const cap = palette?.capabilities.find((c) => c.key === binding.capability);
    const contract = contractOf(step, titleOf);
    const captain = step.captain;
    const outsideId = captain && typeof captain === "object" ? str(captain.external_agent) : "";
    const outside = outsideId ? outsideAgents.find((a) => a.id === outsideId) : undefined;
    const outcome = isOutcome(step);
    const isControl = CONTROL.has(step.kind) || outcome;
    const title = titleOf(id);

    // WHO RUNS IT, in the chip's words.
    let runsAs: PipelineStage["runsAs"];
    const approval = asObj(step.approval);
    if (outsideId) {
      runsAs = { label: `Outside agent · ${outside?.display_name || "not connected"}`, tone: "outside",
                 tip: "The organisation's own agent, called over HTTP under this stage's contract." };
    } else if (step.kind === "deterministic") {
      runsAs = { label: "Deterministic · no model", tone: "deterministic",
                 tip: "A fixed graph query: no model, no cost, the same answer every time." };
    } else if (step.kind === "decision" || cap?.is_gate) {
      const role = str(approval.approver_role) || str((step as Obj).approver_role);
      const human = palette?.humanRole || "human";
      runsAs = { label: role && role !== human ? `Person · ${roleName(role, palette)}` : "Person",
                 tone: "person", tip: "Waits for a person to decide." };
    } else if (step.kind === "external_agent") {
      runsAs = { label: "Outside agent", tone: "outside",
                 tip: "Name the outside agent on this stage's captain to run it." };
    } else if (step.kind === "for_each") {
      runsAs = { label: "For each lane", tone: "control", tip: "Runs its stage once per item, side by side." };
    } else if (step.kind === "bounded_loop") {
      const max = Number((step as Obj).max_iterations) || 0;
      runsAs = { label: max ? `Bounded loop (up to ${max})` : "Bounded loop", tone: "control",
                 tip: "Repeats its stage while its condition holds, never more than the cap." };
    } else if (outcome) {
      runsAs = { label: "Outcome", tone: "control", tip: "Where a branch of the flow ends or passes on; does no work itself." };
    } else if (isControl) {
      runsAs = { label: humanise(step.kind), tone: "control", tip: "Shapes the flow; does no work itself." };
    } else {
      const named = typeof captain === "string" ? captain.trim() : "";
      const human = palette?.humanRole || "human";
      if (named && named !== human && (!cap?.role_agent || named !== cap?.captain)) {
        runsAs = { label: roleLabel(named, palette), tone: "agent", tip: "Named on this stage." };
      } else if (cap?.role_agent) {
        runsAs = { label: roleLabel(cap.role_agent, palette), tone: "agent",
                   tip: "The role agent whose judgement this stage's work is." };
      } else if (cap && cap.needs_model && cap.captain) {
        runsAs = { label: roleLabel(cap.captain, palette), tone: "agent", tip: "Answers for this stage." };
      } else if (cap) {
        // One wording for every step that runs no model, built-in or graph query alike.
        runsAs = { label: "Deterministic · no model", tone: "deterministic",
                   tip: "BOSS does this itself, without a model: no cost, the same answer every time." };
      } else if (named === human) {
        runsAs = { label: "Person", tone: "person", tip: "Waits for a person." };
      } else {
        runsAs = { label: "Agent", tone: "agent", tip: "BOSS picks the agent that holds what it needs." };
      }
    }

    // WHAT IT DOES, in one line.
    // Only a loop or a lane frame holds stages; a condition routes, it holds nothing.
    const members = step.kind === "bounded_loop" || step.kind === "for_each"
      ? steps.filter((s) => groupOf.get(s.id) === id).map((s) => s.id) : undefined;
    let frameNote: string | undefined;
    let description = str((step as Obj).description) || str((step as Obj).summary);
    if (step.kind === "bounded_loop") {
      const max = Number((step as Obj).max_iterations) || 0;
      const expression = asObj((step as Obj).expression);
      const [from, field] = pathParts(str(expression.path));
      const whose = from && byId.has(from) ? (members?.includes(from) ? "it" : titleOf(from)) : "";
      const what = field ? humanise(field.replace(/ /g, "_")).toLowerCase() : "";
      const value = typeof expression.value === "string" || typeof expression.value === "number"
        ? String(expression.value) : "";
      const when = whose && value && ["eq", "equals", "=="].includes(str(expression.op))
        ? ` while ${whose} reports ${value}`
        : what && whose
          ? (str(expression.op) === "falsy" ? ` until ${whose} reports ${what}` : ` while ${whose} reports ${what}`)
          : "";
      frameNote = `Repeats ${(members ?? []).map(titleOf).join(", ") || "its stage"}`
        + `${max ? ` up to ${max}×` : ""}${when}`;
      description = description || `${frameNote}.`;
    } else if (step.kind === "for_each") {
      const [from, field] = pathParts(str((step as Obj).items));
      // What it fans out over, in the author's words when given ("root-cause family"); a field
      // named like its own stage ("lanes" for the Lane stage) says nothing, so it is an item.
      const memberWords = new Set((members ?? []).map((m) => singular(titleOf(m).toLowerCase())));
      const fieldWord = field ? singular(humanise(field.replace(/ /g, "_")).toLowerCase()) : "";
      const per = str((step as Obj).item_label)
        || (fieldWord && !memberWords.has(fieldWord) ? fieldWord : "item");
      const at = Number((step as Obj).concurrency) || 0;
      frameNote = `In parallel, one per ${per}${from && byId.has(from) ? `, from ${titleOf(from)}` : ""}`
        + `${at ? ` · up to ${at} at once` : ""}`;
      description = description || `Runs ${(members ?? []).map(titleOf).join(", ") || "its stage"} once per ${per}.`;
    }
    if (step.kind === "condition") {
      // A condition says what it routes on and where each answer goes, by stage name.
      const yes = str((step as Obj).on_true);
      const no = str((step as Obj).on_false);
      const where = (target: string) => (target && byId.has(target) ? titleOf(target) : "");
      frameNote = [where(yes) ? `Yes: ${where(yes)}` : "", where(no) ? `No: ${where(no)}` : ""]
        .filter(Boolean).join(" · ") || undefined;
      description = description || [title, frameNote ? `${frameNote}.` : ""].filter(Boolean).join(" ");
    }
    if (!description && cap?.summary) description = cap.summary;
    if (!description && step.kind === "deterministic") description = palette?.graphSteps?.[step.id]?.summary ?? "";
    if (!description && step.kind === "decision") description = str((step as Obj).question);
    if (!description && contract && (contract.reads.length || contract.writes.length)) {
      const r = contract.reads.map((k) => kindLabel(k.kind)).join(", ");
      const w = contract.writes.map((k) => kindLabel(k.kind)).join(", ");
      description = [r ? `Reads ${r.toLowerCase()}` : "", w ? `writes ${w.toLowerCase()}` : ""]
        .filter(Boolean).join("; ");
      description = description[0].toUpperCase() + description.slice(1) + ".";
    }
    if (!description && step.kind === "deterministic") description = "A fixed graph query: no model, no cost.";
    if (!description && step.kind === "task") description = str(asObj(asObj(binding.inputs).instructions));
    if (!description) description = runsAs.tone === "control" ? "Shapes the flow." : title;

    const trace = run?.steps[id];
    const status: StageStatus = trace ? (STATUS[trace.status] ?? "pending") : "not_run";
    const design = designOf(step, contract, designs?.stages[id], runsAs.tone === "person");
    // A SHARED SERVICE sits in its own row; a SIDE PATH (`side_path`, or a stage the flow never
    // reaches) beside the flow, dashed. Neither takes a number on the main flow.
    const sidePath = (step as Obj).side_path;
    const placement: PipelineStage["placement"] = design.status === "shared_service" ? "shared"
      : (sidePath || (!onMain.has(id) && !groupOf.has(id))) && !isControl ? "side" : "main";
    stages.push({
      id, kind: step.kind, number: null, title, description, runsAs, status, contract,
      design, placement,
      ...(placement === "side" ? { sideLabel: typeof sidePath === "string" && sidePath.trim() ? sidePath.trim() : "Side paths" } : {}),
      ...(placement === "side" && sideLinks.get(id) ? { sideLink: sideLinks.get(id) } : {}),
      ...(outcome ? { outcome: true } : {}),
      ...(groupOf.has(id) ? { group: groupOf.get(id) } : {}),
      ...(members ? { members } : {}),
      ...(frameNote ? { frameNote } : {}),
      ...(step.kind === "bounded_loop" && Number((step as Obj).max_iterations)
        ? { maxIterations: Number((step as Obj).max_iterations) } : {}),
      agent: outsideId || (typeof captain === "string" ? captain : undefined),
      ...(outsideId ? { captainKind: "outside_agent" as const, endpointHost: outside?.endpoint_host } : {}),
      ...(trace ? { trace } : {}),
    });
  }
  // NUMBERS, in run order: every stage an agent or BOSS runs on the flow takes the next number;
  // a person's step is a human step ("—"), a frame or an outcome only groups or marks. A stage
  // may carry the number its design gives it (`stage_number`: the daily Escalation Agent is 9),
  // and the flow's own numbering then steps over it, so 8 · Fix is followed by 10 · Closure.
  const declared = (s: PipelineStage): number | null => {
    const raw = Number((byId.get(s.id) as Obj | undefined)?.stage_number);
    return Number.isInteger(raw) && raw > 0 ? raw : null;
  };
  const claimed = new Set(stages.map(declared).filter((v): v is number => v !== null));
  for (const s of stages) {
    const own = declared(s);
    if (own !== null) { s.number = own; continue; }
    if (s.placement !== "main" || s.runsAs.tone === "control" || s.runsAs.tone === "person") continue;
    n += 1;
    while (claimed.has(n)) n += 1;
    s.number = n;
  }
  // THE ANCHORS of the read/write view: the stage that starts it (its contract's `invoked_by`,
  // else the stage before it on the flow), and where it goes next (the handoff's stage, else
  // the next stage on the flow).
  const flow = stages.filter((s) => s.placement === "main" && s.runsAs.tone !== "control");
  const titleById = new Map(stages.map((s) => [s.id, s.title]));
  flow.forEach((s, i) => {
    const by = s.contract?.invokedBy;
    if (by && titleById.has(by) && by !== s.id) s.from = titleById.get(by);
    else if (i > 0) s.from = flow[i - 1].title;
    const to = s.contract?.handoffTo;
    if (to && titleById.has(to)) s.to = titleById.get(to);
    else if (i < flow.length - 1) s.to = flow[i + 1].title;
  });
  for (const s of stages.filter((x) => x.placement !== "main")) {
    const by = s.contract?.invokedBy;
    if (by && titleById.has(by) && by !== s.id) s.from = titleById.get(by);
    const to = s.contract?.handoffTo;
    if (to && titleById.has(to)) s.to = titleById.get(to);
  }
  return stages;
}

/** "reads 3 · writes 2 · calls 1", or null when the stage declares nothing. */
export function contractSummary(contract: StageContract | null): string | null {
  if (!contract) return null;
  const parts = [
    contract.reads.length ? `reads ${contract.reads.length}` : "",
    contract.writes.length ? `writes ${contract.writes.length}` : "",
    contract.calls.length ? `calls ${contract.calls.length}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}
