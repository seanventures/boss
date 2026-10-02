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
 * it binds, its inputs, its agent, its approval mode, an agent step's scope — and everything
 * else on the loaded step, on its binding and on the spec is carried through untouched.
 */

export type DraftStep = {
  id: string;
  kind: string;
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
const OWNED_STEP_KEYS = ["id", "kind", "binding", "approval", "next", "captain"];

/** Turn one stored step into a canvas step. */
export function draftFromSpecStep(step: Obj): DraftStep {
  const binding = asObj(step.binding);
  const approval = asObj(step.approval);
  const agentWork = Boolean(binding.agent_work) || undefined;
  return {
    id: String(step.id),
    kind: String(step.kind ?? "capability"),
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

/** One canvas step as a spec step, merged over the step it was loaded from. */
function specStep(step: DraftStep, next: string[], ctx: SpecContext): Obj {
  const original = asObj(step.original);
  const same = Object.keys(original).length > 0
    && identity(String(original.kind ?? ""), asObj(original.binding)) === draftIdentity(step);
  const base: Obj = same ? { ...original } : {};
  const baseBinding = same ? asObj(original.binding) : {};
  const baseApproval = same ? asObj(original.approval) : {};
  const out: Obj = { ...base, id: step.id, kind: step.kind, next };
  delete out.captain;

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
  const steps = draft.map((step, index) =>
    specStep(step, index + 1 < draft.length ? [draft[index + 1].id] : [], ctx));
  const out: Obj = { ...loaded, entry_step: draft[0]?.id, steps };
  const schema = mergeInputSchema(loaded.input_schema, inputsReadBy(steps));
  if (Object.keys(schema).length) out.input_schema = schema;
  else delete out.input_schema;
  return out;
}
