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
/** A step's `contract` block (§4.1), whatever of it the step has. */
export type StageContract = {
  reads: ContractKind[]; writes: ContractKind[]; calls: ContractCall[];
  trigger?: string; handoff?: string; human_review?: string; llm?: string;
  policy?: [string, string][]; decisions?: string[]; open_questions?: string[];
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
  return {
    reads: kinds(raw.reads), writes: kinds(raw.writes), calls,
    trigger: text(raw.trigger), handoff: text(raw.handoff), human_review: text(raw.human_review),
    llm: typeof raw.llm === "string" ? raw.llm : undefined,
    ...(policy.length ? { policy } : {}),
    ...(strs(raw.decisions).length ? { decisions: strs(raw.decisions) } : {}),
    ...(strs(raw.open_questions).length ? { open_questions: strs(raw.open_questions) } : {}),
  };
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

export function buildPipeline(spec: Spec, palette: Palette | null,
                              outsideAgents: OutsideAgentRef[] = [],
                              run: RunContext | null = null): PipelineStage[] {
  const steps = spec.steps ?? [];
  const byId = new Map(steps.map((s) => [s.id, s]));
  const { order, groupOf } = displayOrder(spec);
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
    if (!isControl) n += 1;
    stages.push({
      id, kind: step.kind, number: isControl ? null : n, title, description, runsAs, status, contract,
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
