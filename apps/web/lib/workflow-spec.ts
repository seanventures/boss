/**
 * The builder's canvas, to and from a workflow spec — as a MERGE over what was loaded.
 *
 * THE INCIDENT. The builder rebuilt the whole spec from its canvas on every save: `{entry_step,
 * steps}` and, per step, only the fields it had a control for. Everything else was dropped —
 * `input_schema`, so the start form had no fields and Start fell back to a random sample; an
 * agent step's `produces`, `agents` and `max_steps`, so the video example's publish step lost
 * its title after one save; a typed decision's binding, so the workflow could no longer be saved
 * at all; and `max_attempts`, `on_failure` and every other key the canvas does not draw. Opening
 * a workflow, changing one step's agent and pressing Save broke every bundled example.
 *
 * So the canvas OWNS a short list of fields — id, kind, order, which capability/step/agent-work
 * it binds, its inputs, its agent, its approval mode, an agent step's scope, its title — and
 * everything else on the loaded step, on its binding and on the spec is carried through
 * untouched.
 *
 * ORDER OWNS `next` ONLY ON A STRAIGHT LINE. Rewriting every step's `next` from the order the
 * steps were drawn in turned a parallel review into a sequence and dropped a condition's
 * routing on the next save. `next` is rewritten from order only when the loaded spec is linear
 * (lib/workflow-graph.ts `isLinear`); otherwise each step keeps its own `next` and branch fields.
 *
 * The Workflow Studio editor does not go through the canvas model at all: it edits the spec in
 * place with `applyStepPatch` below, which touches only the keys a patch names.
 */
import { isLinear } from "./workflow-graph";
import type { InputSource, SpecStep, StepPatch } from "./workflow-editor-types";

export type DraftStep = {
  id: string;
  kind: string;
  /** The operator's own name for the step (≤80), shown instead of the capability label. */
  title?: string;
  capability?: string;
  customStep?: string;
  inputs?: Record<string, unknown>;
  captain?: string;
  agentWork?: boolean;
  instructions?: unknown;
  /** An agent step's scope: the skills it may use and the MCP servers it may reach. */
  skills?: string[];
  mcp?: string[];
  approval?: string;
  config?: Record<string, unknown>;
  /** The step exactly as it was loaded, so a save can carry what the canvas does not draw. */
  original?: Record<string, unknown>;
};

type Obj = Record<string, unknown>;

const asObj = (value: unknown): Obj =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.map((v) => String(v).trim()).filter(Boolean) : [];

/** Step keys the canvas owns; every other top-level key on a loaded step is carried through. */
const OWNED_STEP_KEYS = ["id", "kind", "binding", "approval", "next", "captain", "title"];

/** Step keys a `config` patch may never write: identity, graph structure, and the keys other
 *  patch fields own. Everything else is a block setting (question, wait_seconds, a condition's
 *  expression, …). */
const STRUCTURAL_STEP_KEYS = new Set([
  ...OWNED_STEP_KEYS, "branches", "join", "wait_for", "on_true", "on_false", "body",
  "max_attempts", "retry_backoff", "timeout_seconds", "on_error",
]);

/** Turn one stored step into a canvas step. */
export function draftFromSpecStep(step: Obj): DraftStep {
  const binding = asObj(step.binding);
  const approval = asObj(step.approval);
  const agentWork = Boolean(binding.agent_work) || undefined;
  return {
    id: String(step.id),
    kind: String(step.kind ?? "capability"),
    title: typeof step.title === "string" && step.title ? step.title : undefined,
    capability: binding.capability ? String(binding.capability) : undefined,
    customStep: binding.custom_step ? String(binding.custom_step) : undefined,
    approval: approval.mode ? String(approval.mode) : "auto",
    // Carried through untouched. Silently discarding wiring somebody else authored is a bug.
    inputs: (binding.inputs ?? undefined) as Obj | undefined,
    captain: step.captain ? String(step.captain) : undefined,
    agentWork,
    instructions: agentWork ? asObj(binding.inputs).instructions : undefined,
    skills: agentWork ? strings(binding.skills) : undefined,
    mcp: agentWork ? strings(binding.mcp ?? binding.mcp_servers) : undefined,
    // A control block's own settings (wait_seconds, question, guidance, approver_role, …).
    config: Object.fromEntries(
      Object.entries(step).filter(([k]) => !OWNED_STEP_KEYS.includes(k)),
    ),
    original: step,
  };
}

/** What the step binds, so a merge only reuses a loaded step that binds the same thing. */
function identity(kind: string, binding: Obj): string {
  if (kind !== "capability") return `block:${kind}`;
  if (binding.agent_work) return "agent_work";
  if (binding.typed_decision) return "typed_decision";
  if (binding.custom_step) return `custom:${String(binding.custom_step)}`;
  return `capability:${String(binding.capability ?? "")}`;
}

function draftIdentity(step: DraftStep): string {
  if (step.kind !== "capability") return `block:${step.kind}`;
  if (step.agentWork) return "agent_work";
  if (step.customStep) return `custom:${step.customStep}`;
  if (step.capability) return `capability:${step.capability}`;
  // Neither: a step kind the canvas cannot draw (a typed decision). It is whatever was loaded.
  const original = asObj(step.original);
  return identity(String(original.kind ?? step.kind), asObj(original.binding));
}

export type SpecContext = {
  /** The spec as it was loaded (or imported). Absent for a workflow drawn from nothing. */
  loaded?: Obj | null;
  capabilities: { key: string; required_inputs?: string[] }[];
  customSteps: { step_key: string; inputs?: { name: string }[] }[];
};

/** One canvas step as a spec step, merged over the step it was loaded from. `next` is null
 *  when order does not own it, and the step keeps the `next` it was loaded with. */
function specStep(step: DraftStep, next: string[] | null, ctx: SpecContext): Obj {
  const original = asObj(step.original);
  const same = Object.keys(original).length > 0
    && identity(String(original.kind ?? ""), asObj(original.binding)) === draftIdentity(step);
  const base: Obj = same ? { ...original } : {};
  const baseBinding = same ? asObj(original.binding) : {};
  const baseApproval = same ? asObj(original.approval) : {};
  const out: Obj = { ...base, id: step.id, kind: step.kind,
                     next: next ?? (original.next !== undefined ? original.next : []) };
  delete out.captain;
  delete out.title;
  if (step.title) out.title = step.title;

  if (step.kind !== "capability") {
    // A control block keeps its settings at the top level, where the runtime reads
    // `wait_seconds`, `question` and `guidance` — EXCEPT "Run another workflow", which the
    // validator reads from `binding.workflow_version_id`. The builder wrote it at the top level,
    // so that block could never be saved.
    const config = { ...(step.config ?? {}) };
    delete config.captain;
    const binding: Obj = { ...baseBinding };
    if (step.kind === "subworkflow") {
      const chosen = config.workflow_version_id ?? binding.workflow_version_id;
      delete config.workflow_version_id;
      if (chosen) binding.workflow_version_id = chosen;
    }
    Object.assign(out, config);
    // Written as loaded: a stored step always has one (the validator adds it), and a new block
    // gets the empty one the runtime expects.
    if (Object.keys(binding).length || "binding" in base || !same) out.binding = binding;
    if (step.captain) out.captain = step.captain;
    return out;
  }

  const approval = { ...baseApproval, mode: step.approval ?? baseApproval.mode ?? "auto" };
  out.approval = approval;
  if (step.captain) out.captain = step.captain;

  if (step.agentWork) {
    const binding: Obj = {
      ...baseBinding,
      agent_work: true,
      inputs: {
        ...(step.inputs ?? {}),
        // Defaulted to the run's own `instructions` field: "say what you want when you start it".
        instructions: step.instructions ?? { from_input: "instructions" },
      },
    };
    delete binding.mcp_servers;
    if (step.skills?.length) binding.skills = [...step.skills];
    else delete binding.skills;
    if (step.mcp?.length) binding.mcp = [...step.mcp];
    else delete binding.mcp;
    out.binding = binding;
    return out;
  }
  if (step.customStep) {
    const defined = ctx.customSteps.find((c) => c.step_key === step.customStep);
    out.binding = {
      ...baseBinding,
      custom_step: step.customStep,
      inputs: step.inputs ?? Object.fromEntries(
        (defined?.inputs ?? []).map((i) => [i.name, { from_input: i.name }]),
      ),
    };
    return out;
  }
  if (step.capability) {
    const capability = ctx.capabilities.find((c) => c.key === step.capability);
    out.binding = {
      ...baseBinding,
      capability: step.capability,
      inputs: step.inputs ?? Object.fromEntries(
        (capability?.required_inputs ?? []).map((name) => [name, { from_input: name }]),
      ),
    };
    return out;
  }
  // A typed decision, or any binding the canvas has no control for: carried as loaded.
  out.binding = { ...baseBinding, ...(step.inputs ? { inputs: step.inputs } : {}) };
  return out;
}

/** Every run input the steps read with `from_input`, in order of first use. */
export function inputsReadBy(steps: Obj[]): string[] {
  const names: string[] = [];
  for (const step of steps) {
    for (const value of Object.values(asObj(asObj(step.binding).inputs))) {
      const name = asObj(value).from_input;
      if (typeof name === "string" && name && !names.includes(name)) names.push(name);
    }
  }
  return names;
}

const titleise = (name: string) => {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : name;
};

/**
 * The loaded input_schema, with any input a step now reads added to it.
 *
 * Kept in whichever of the two shapes it was written in. A name the canvas newly reads is
 * OFFERED on the start form (a property), not DEMANDED (not added to `required`): only the
 * workflow's author can say a run must not start without it, and guessing would refuse runs
 * that work today. A workflow drawn from nothing gets JSON Schema, so Start shows a form instead
 * of falling back to a sample.
 */
export function mergeInputSchema(loaded: unknown, reads: string[]): Obj {
  const schema = asObj(loaded);
  const jsonSchema = "properties" in schema || "required" in schema;
  if (Object.keys(schema).length && !jsonSchema) {
    const merged: Obj = { ...schema };
    for (const name of reads) if (!(name in merged)) merged[name] = "string";
    return merged;
  }
  if (!Object.keys(schema).length && !reads.length) return {};
  const properties: Obj = { ...asObj(schema.properties) };
  for (const name of reads) {
    if (!(name in properties)) {
      properties[name] = {
        type: "string",
        title: titleise(name),
        ...(name === "instructions" ? { format: "textarea" } : {}),
      };
    }
  }
  return { ...schema, properties };
}

/** The canvas as a spec, merged over the loaded one. */
export function specFromDraft(draft: DraftStep[], ctx: SpecContext): Obj {
  const loaded = asObj(ctx.loaded);
  const linear = !Array.isArray(loaded.steps) || isLinear(loaded as Parameters<typeof isLinear>[0]);
  const steps = draft.map((step, index) =>
    specStep(step, linear ? (index + 1 < draft.length ? [draft[index + 1].id] : []) : null, ctx));
  const entry = linear ? draft[0]?.id
    : draft.some((s) => s.id === loaded.entry_step) ? loaded.entry_step : draft[0]?.id;
  const out: Obj = { ...loaded, entry_step: entry, steps };
  const schema = mergeInputSchema(loaded.input_schema, inputsReadBy(steps));
  if (Object.keys(schema).length) out.input_schema = schema;
  else delete out.input_schema;
  return out;
}

/* ── The editor's step patch ─────────────────────────────────────────────────────── */

/** The longest title a step may carry; the API refuses longer. */
export const TITLE_MAX = 80;

/** How a stored input value reads as a source: a reference, or a literal pinned in the spec. */
export function inputSourceOf(value: unknown): InputSource | null {
  if (value === undefined || value === null || value === "") return null;
  const obj = asObj(value);
  if (typeof obj.from_input === "string") return { from_input: obj.from_input };
  if (typeof obj.from_step === "string") {
    return { from_step: obj.from_step, field: String(obj.field ?? "") };
  }
  return { value };
}

/** A source as the spec stores it. A literal is the bare value — what the runner reads. */
function storedSource(source: InputSource, previous: unknown): unknown {
  const before = asObj(previous);
  // Same kind of source: keep what else it carried (a `default`, an `override_from_input`).
  if ("from_input" in source) {
    return typeof before.from_input === "string"
      ? { ...before, from_input: source.from_input } : { from_input: source.from_input };
  }
  if ("from_step" in source) {
    return typeof before.from_step === "string"
      ? { ...before, from_step: source.from_step, field: source.field }
      : { from_step: source.from_step, field: source.field };
  }
  return source.value;
}

/** Structural equality of JSON values; key order does not matter. */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    const other = b as unknown[];
    return a.length === other.length && a.every((v, i) => deepEqual(v, other[i]));
  }
  const ka = Object.keys(a as Obj).filter((k) => (a as Obj)[k] !== undefined);
  const kb = Object.keys(b as Obj).filter((k) => (b as Obj)[k] !== undefined);
  return ka.length === kb.length && ka.every((k) => deepEqual((a as Obj)[k], (b as Obj)[k]));
}

const sameJson = deepEqual;

/** `obj` with `key` set, or removed when `value` is undefined or null. */
function setOrDrop(obj: Obj, key: string, value: unknown): Obj {
  const out = { ...obj };
  if (value === undefined || value === null) delete out[key];
  else out[key] = value;
  return out;
}

/**
 * One inspector edit, applied to the stored step. Only the keys the patch names are touched,
 * and a value equal to what is already there changes nothing — so opening a step and leaving
 * it as it was can never alter the spec (the incident at the top of this file).
 *
 * Returns the SAME object when nothing changed, so a caller can skip an undo entry.
 */
export function applyStepPatch(step: SpecStep, patch: StepPatch): SpecStep {
  let out: Obj = { ...step };
  let binding = asObj(step.binding);
  let bindingTouched = false;
  const agentWork = step.kind === "capability" && Boolean(binding.agent_work);

  if (patch.title !== undefined) {
    const title = patch.title.trim().slice(0, TITLE_MAX);
    out = setOrDrop(out, "title", title || undefined);
  }
  if (patch.captain !== undefined) {
    const captain = (patch.captain ?? "").trim();
    out = setOrDrop(out, "captain", captain || undefined);
  }
  if (patch.externalAgent !== undefined) {
    const externalAgentId = (patch.externalAgent ?? "").trim();
    if (externalAgentId) {
      // AN OUTSIDE AGENT'S STEP IS ITS OWN KIND, WITH A CONTRACT. Setting only the captain left
      // kind "capability" and no contract, and publishing failed ("not an agent key"), so a
      // bank's agent could not be assigned from the console (2026-10-31). The default contract
      // records one decision named after the step; what it may read is set in the JSON view.
      const contract = asObj(out.contract);
      out = {
        ...out, kind: "external_agent", captain: { external_agent: externalAgentId },
        contract: Object.keys(contract).length ? contract : {
          reads: [],
          writes: [{ kind: "agent_decision", required: true, decision_type: String(out.id ?? "review") }],
        },
      };
      delete out.binding;
    } else if (typeof out.captain === "object") {
      out = setOrDrop(out, "captain", undefined);
    }
  }
  if (patch.instructions && agentWork) {
    const inputs = asObj(binding.inputs);
    const current = inputs.instructions;
    const next = patch.instructions.mode === "ask"
      ? (inputSourceOf(current) && "from_input" in inputSourceOf(current)!
        ? current : { from_input: "instructions" })
      : (patch.instructions.text ?? "");
    binding = { ...binding, inputs: { ...inputs, instructions: next } };
    bindingTouched = true;
  }
  // Scope exists only on an agent task: the engine refuses it anywhere else.
  if (patch.skills && agentWork) {
    binding = setOrDrop(binding, "skills", patch.skills.length ? [...patch.skills] : undefined);
    bindingTouched = true;
  }
  if (patch.mcp && agentWork) {
    // `mcp_servers` is the older spelling of the same list; one list is written, the new one.
    const legacy = Array.isArray(binding.mcp_servers) ? binding.mcp_servers : undefined;
    const current = Array.isArray(binding.mcp) ? binding.mcp : legacy;
    if (!sameJson(current ?? [], patch.mcp)) {
      binding = setOrDrop(setOrDrop(binding, "mcp_servers", undefined), "mcp",
                          patch.mcp.length ? [...patch.mcp] : undefined);
      bindingTouched = true;
    }
  }
  if (patch.produces && agentWork) {
    binding = setOrDrop(binding, "produces", patch.produces.length ? [...patch.produces] : undefined);
    bindingTouched = true;
  }
  if (patch.input) {
    const inputs = asObj(binding.inputs);
    const { name, source } = patch.input;
    const value = source ? storedSource(source, inputs[name]) : undefined;
    binding = { ...binding, inputs: setOrDrop(inputs, name, value) };
    bindingTouched = true;
  }
  if (patch.approval || patch.approvalMore) {
    const approval = asObj(step.approval);
    let next: Obj = { ...approval };
    // "auto" on a step with no approval block is what it already is: write nothing.
    if (patch.approval && (patch.approval !== (approval.mode ?? "auto") || "mode" in approval)) {
      next.mode = patch.approval;
    }
    for (const [key, value] of Object.entries(patch.approvalMore ?? {})) {
      next = setOrDrop(next, key, value === "" ? undefined : value);
    }
    if (!sameJson(next, approval)) {
      out.approval = next;
    }
  }
  if (patch.config) {
    for (const [key, raw] of Object.entries(patch.config)) {
      // A block setting only. Identity, structure, binding and the keys this patch owns have
      // their own edits (renameStepId, the linear-only graph edits, the fields above); a config
      // key that wrote them would bypass every rule those keep.
      if (STRUCTURAL_STEP_KEYS.has(key)) continue;
      const value = raw === "" ? undefined : raw;
      // "Run another workflow" is read from the binding by the validator, not the top level.
      if (key === "workflow_version_id" && step.kind === "subworkflow") {
        binding = setOrDrop(binding, key, value);
        bindingTouched = true;
      } else {
        out = setOrDrop(out, key, value);
      }
    }
  }
  if (patch.max_attempts !== undefined) {
    out = setOrDrop(out, "max_attempts", patch.max_attempts === null ? undefined
      : Math.min(25, Math.max(1, Math.round(patch.max_attempts))));
  }
  if (patch.retry_backoff !== undefined) {
    const backoff = Object.fromEntries(Object.entries(patch.retry_backoff ?? {})
      .filter(([, v]) => typeof v === "number" && Number.isFinite(v)));
    out = setOrDrop(out, "retry_backoff", Object.keys(backoff).length ? backoff : undefined);
  }
  if (patch.timeout_seconds !== undefined) {
    out = setOrDrop(out, "timeout_seconds", patch.timeout_seconds === null ? undefined
      : Math.min(86400, Math.max(1, Math.round(patch.timeout_seconds))));
  }
  if (bindingTouched && !sameJson(binding, asObj(step.binding))) out.binding = binding;
  return sameJson(out, step) ? step : (out as SpecStep);
}
