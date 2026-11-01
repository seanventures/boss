/**
 * THE PIPELINE VIEW'S MODEL (design §4.7): a workflow spec, read as the numbered stage diagram a
 * consultant draws — who runs each stage, what it reads and writes, whether it ran — in words.
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
/** A decision, policy or open question with its ID ("A-D30", "OP-ENR-01", "HP-Q1"). */
export type DesignNote = { id: string; text: string };
/** One of the agent's numbered internal steps (`contract.steps`). */
export type AgentStepDef = {
  n: number; name: string; reads: string[]; writes: string[]; calls: ContractCall[]; explain?: string;
};
/** The design cards a contract carries (`contract.design`). */
export type StageDesignCards = {
  status?: DesignStatus; reference?: string; underRevision?: string;
  decisions: DesignNote[]; policy: DesignNote[]; openQuestions: DesignNote[];
  appliedIn?: { kg?: string; cg?: string; items: string[] };
};
/** A step's `contract` block (§4.1), whatever of it the step has. */
export type StageContract = {
  reads: ContractKind[]; writes: ContractKind[]; calls: ContractCall[];
  trigger?: string; handoff?: string; human_review?: string; llm?: string;
  policy?: [string, string][]; decisions?: string[]; open_questions?: string[];
  /** The step the handoff names, by id, to point "To <next stage>" at. */
  handoffTo?: string;
  steps?: AgentStepDef[];
  design?: StageDesignCards;
};

/** A stage design's review state (design §4.6), as the consultant's legend names it. */
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

function contractOf(step: SpecStep): StageContract | null {
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
    const to = str(row.to);
    const carries = list(row.carries).map((c) => humanise(String(c))).join(", ");
    if (to) return `To ${humanise(to)}${carries ? `, carrying ${carries.toLowerCase()}` : ""}`;
    return undefined;
  };
  const policy = Object.entries(asObj(raw.policy))
    .map(([k, v]) => [humanise(k), typeof v === "object" ? JSON.stringify(v) : String(v)] as [string, string]);
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
  const handoffTo = str(asObj(raw.handoff).to);
  return {
    reads: kinds(raw.reads), writes: kinds(raw.writes), calls,
    trigger: text(raw.trigger), handoff: text(raw.handoff), human_review: text(raw.human_review),
    llm: typeof raw.llm === "string" ? raw.llm : undefined,
    ...(policy.length ? { policy } : {}),
    ...(strs(raw.decisions).length ? { decisions: strs(raw.decisions) } : {}),
    ...(strs(raw.open_questions).length ? { open_questions: strs(raw.open_questions) } : {}),
    ...(handoffTo ? { handoffTo } : {}),
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
  return { stages, kgVersion: num(graph.kg_version), cgVersion: num(graph.cg_version), kinds };
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

const nexts = (step: SpecStep): string[] =>
  (Array.isArray(step.next) ? step.next : step.next ? [step.next] : []).map(String);
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
  const queue = entry ? [entry] : [];
  while (queue.length) {
    const id = queue.shift()!;
    if (seen.has(id)) continue;
    place(id);
    const step = byId.get(id);
    if (!step) continue;
    const parallel = step.kind === "parallel" ? list((step as Obj).branches).map(String) : [];
    queue.push(...parallel, ...nexts(step).filter((n) => !groupOf.has(n)));
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

  const stages: PipelineStage[] = [];
  let n = 0;
  for (const id of order) {
    const step = byId.get(id)!;
    const binding = asObj(step.binding);
    const cap = palette?.capabilities.find((c) => c.key === binding.capability);
    const contract = contractOf(step);
    const captain = step.captain;
    const outsideId = captain && typeof captain === "object" ? str(captain.external_agent) : "";
    const outside = outsideId ? outsideAgents.find((a) => a.id === outsideId) : undefined;
    const isControl = CONTROL.has(step.kind);
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
    const members = isControl ? steps.filter((s) => groupOf.get(s.id) === id).map((s) => s.id) : undefined;
    let frameNote: string | undefined;
    let description = str((step as Obj).description) || str((step as Obj).summary);
    if (step.kind === "bounded_loop") {
      const max = Number((step as Obj).max_iterations) || 0;
      const expression = asObj((step as Obj).expression);
      const [from, field] = pathParts(str(expression.path));
      const whose = from && byId.has(from) ? (members?.includes(from) ? "it" : titleOf(from)) : "";
      const what = field ? humanise(field.replace(/ /g, "_")).toLowerCase() : "";
      const when = what && whose
        ? (str(expression.op) === "falsy" ? ` until ${whose} reports ${what}` : ` while ${whose} reports ${what}`)
        : "";
      frameNote = `Repeats ${(members ?? []).map(titleOf).join(", ") || "its stage"}`
        + `${max ? ` up to ${max}×` : ""}${when}`;
      description = description || `${frameNote}.`;
    } else if (step.kind === "for_each") {
      const [from, field] = pathParts(str((step as Obj).items));
      const per = field ? singular(humanise(field.replace(/ /g, "_")).toLowerCase()) : "item";
      const at = Number((step as Obj).concurrency) || 0;
      frameNote = `One lane per ${per}${from && byId.has(from) ? `, from ${titleOf(from)}` : ""}`
        + `${at ? ` · up to ${at} at once` : ""}`;
      description = description || `Runs ${(members ?? []).map(titleOf).join(", ") || "its stage"} once per ${per}.`;
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
    if (!isControl && placement === "main") n += 1;
    stages.push({
      id, kind: step.kind, number: isControl || placement !== "main" ? null : n, title, description, runsAs, status, contract,
      design, placement,
      ...(placement === "side" ? { sideLabel: typeof sidePath === "string" && sidePath.trim() ? sidePath.trim() : "Side paths" } : {}),
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
  // THE ANCHORS of the read/write view: the stage that hands work in, and where it goes next
  // (the handoff's named stage, else the next stage on the flow).
  const numbered = stages.filter((s) => s.number !== null);
  const titleById = new Map(stages.map((s) => [s.id, s.title]));
  numbered.forEach((s, i) => {
    if (i > 0) s.from = numbered[i - 1].title;
    const to = s.contract?.handoffTo;
    if (to && titleById.has(to)) s.to = titleById.get(to);
    else if (i < numbered.length - 1) s.to = numbered[i + 1].title;
  });
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
