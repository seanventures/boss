/**
 * How a step reads on screen: its title, its type, who runs it and its badges.
 *
 * ONE ANSWER EVERYWHERE. The canvas node, the library card's avatars and the run lane all ask
 * this function, so a step cannot say "Auto" on one screen and "engineering_captain" on
 * another — which is what three naming schemes and two agent fallbacks produced before.
 *
 * WHO RUNS IT (the agent rule, in order):
 *   1. `captain: "human"`              → A person
 *   2. any other captain               → that agent, by its display name
 *   3. an approval gate                → A person (a decision step, or a built-in step whose
 *                                        capability `is_gate`s, e.g. "UX sign-off": the engine's
 *                                        workflow_run_actions.is_gate, the one test)
 *   4. an agent task or your own step  → Auto (BOSS picks), or "Auto · <name>" when exactly one
 *                                        agent holds every skill the step needs
 *   5. a built-in step                 → You (it runs as whoever starts the workflow)
 * Steps the rule does not name (wait, branches, joins, loops, a typed decision) are run by BOSS
 * itself and read "Auto"; "Run another workflow" starts it as the starter and reads "You".
 */
import type { Palette, PaletteItem, Spec, SpecStep, StepView } from "./workflow-editor-types";
import { contractOf } from "./workflow-graph";

type Obj = Record<string, unknown>;

const asObj = (value: unknown): Obj =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.map(String).filter(Boolean) : [];

/** "publish_gate" → "Publish gate". What a step with no label of its own is called. */
export function humanise(id: string): string {
  const words = String(id ?? "").replace(/[_.-]+/g, " ").replace(/\s+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : String(id ?? "");
}

/** What a stored step is, for its chip, icon and agent rule. */
export function stepType(step: SpecStep): StepView["type"] {
  const binding = asObj(step.binding);
  switch (step.kind) {
    case "capability":
      if (binding.agent_work) return "agent";
      if (binding.custom_step) return "custom";
      if (binding.typed_decision) return "other";
      return binding.capability ? "builtin" : "other";
    case "decision": return "gate";
    case "timer": return "wait";
    case "subworkflow": return "subworkflow";
    case "parallel": return "parallel";
    case "condition": return "condition";
    case "join": return "join";
    case "bounded_loop": return "loop";
    case "for_each": return "foreach";
    default: return "other";
  }
}

/** Generic names, used only when a step has no title and no label of its own. */
const TYPE_LABELS: Partial<Record<StepView["type"], string>> = {
  gate: "Approval gate", wait: "Wait", subworkflow: "Run another workflow",
  parallel: "Parallel", condition: "If / else", join: "Wait for branches", loop: "Repeat",
  foreach: "For each",
};

/** The type chip's words (spec 2.6). */
export const TYPE_CHIPS: Record<StepView["type"], string> = {
  builtin: "Built-in", agent: "Agent task", custom: "Your step", gate: "Approval gate",
  wait: "Wait", subworkflow: "Run a workflow", parallel: "Parallel", condition: "If / else",
  join: "Join", loop: "Loop", foreach: "For each", other: "Step",
};

/**
 * The step's title: its own `title`, else the label of what it binds, else its id made
 * readable. Never the raw id — a fresh agent task is "Agent task", an id like "qa_check" is
 * "Qa check" only when nothing better exists.
 */
export function stepTitle(step: SpecStep, palette: Palette | null): string {
  const own = typeof step.title === "string" ? step.title.trim() : "";
  if (own) return own;
  const binding = asObj(step.binding);
  if (binding.custom_step) {
    const def = palette?.customSteps.find((c) => c.step_key === binding.custom_step);
    if (def?.label) return def.label;
  }
  if (binding.capability && !binding.agent_work && !binding.custom_step) {
    const cap = palette?.capabilities.find((c) => c.key === binding.capability);
    if (cap?.label) return cap.label;
  }
  return humanise(step.id);
}

/** Skills a step needs its agent to hold: an agent task's scope, or the skill a step binds. */
function neededSkills(step: SpecStep, palette: Palette): string[] {
  const binding = asObj(step.binding);
  if (binding.agent_work) return strings(binding.skills);
  if (binding.custom_step) {
    const def = palette.customSteps.find((c) => c.step_key === binding.custom_step);
    return def?.binding_kind === "skill" && def.skill_id ? [def.skill_id] : [];
  }
  return [];
}

/** MCP servers (by name, as agents hold them) a step needs its agent to hold. */
function neededMcp(step: SpecStep, palette: Palette): string[] {
  const binding = asObj(step.binding);
  if (binding.agent_work) return strings(binding.mcp ?? binding.mcp_servers);
  if (binding.custom_step) {
    const def = palette.customSteps.find((c) => c.step_key === binding.custom_step);
    if (def?.binding_kind !== "mcp_tool" || !def.mcp_server_id) return [];
    const server = palette.bindable.mcp_servers.find((s) => s.id === def.mcp_server_id);
    return [server?.name ?? def.mcp_server_id];
  }
  return [];
}

/**
 * Everything a step needs its NAMED agent to hold — skills, then MCP servers — and nothing for
 * a built-in step, whose tools are BOSS's own (it runs as whoever starts the workflow, so no
 * grant is involved). `badges.lacks` is this list minus what the agent holds; the agent picker
 * reads it to say `✓ has …` so the two can never disagree.
 */
export function stepNeeds(step: SpecStep, palette: Palette): string[] {
  return [...neededSkills(step, palette), ...neededMcp(step, palette)];
}

/** Whether a person answers this step: the engine's one test (workflow_run_actions.is_gate) —
 *  a decision step, or a built-in step whose capability is declared a gate. */
export function isGateStep(step: SpecStep, palette: Palette | null): boolean {
  const type = stepType(step);
  if (type === "gate") return true;
  if (type !== "builtin") return false;
  const key = asObj(step.binding).capability;
  return Boolean(palette?.capabilities.find((c) => c.key === key)?.is_gate);
}

function agentOf(step: SpecStep, type: StepView["type"], palette: Palette): StepView["agent"] {
  const human = palette.humanRole || "human";
  const captain = typeof step.captain === "string" ? step.captain.trim() : "";
  if (captain === human) return { kind: "person", label: "A person", tip: "Waits for a person to answer." };
  if (captain) {
    const agent = palette.agents.find((a) => a.role_key === captain);
    return { kind: "named", role_key: captain, label: agent?.name || captain,
             tip: "Named by this workflow." };
  }
  if (isGateStep(step, palette)) return { kind: "person", label: "A person", tip: "Waits for a person to answer." };
  if (type === "agent" || type === "custom") {
    const skills = neededSkills(step, palette);
    const mcp = neededMcp(step, palette);
    if (skills.length || mcp.length) {
      const holders = palette.agents.filter((a) =>
        skills.every((s) => a.skills.includes(s)) && mcp.every((m) => a.mcp.includes(m)));
      if (holders.length === 1) {
        return { kind: "auto", role_key: holders[0].role_key, label: `Auto · ${holders[0].name}`,
                 tip: `Only ${holders[0].name} holds ${[...skills, ...mcp].join(", ")}.` };
      }
    }
    return { kind: "auto", label: "Auto",
             tip: type === "agent" ? "BOSS picks an agent holding every allowed skill."
               : "BOSS picks the agent holding this step's skill." };
  }
  if (type === "builtin") {
    return { kind: "you", label: "You", tip: "Built-in step: runs as whoever starts the workflow." };
  }
  if (type === "subworkflow") {
    return { kind: "you", label: "You", tip: "Starts that workflow as whoever started this one." };
  }
  return { kind: "auto", label: "Auto", tip: "BOSS handles this step itself." };
}

export function stepView(step: SpecStep, palette: Palette): StepView {
  const type = stepType(step);
  const binding = asObj(step.binding);
  const approval = asObj(step.approval);
  const inputs = asObj(binding.inputs);
  const { required } = contractOf(step, palette);
  const cap = type === "builtin"
    ? palette.capabilities.find((c) => c.key === binding.capability) : undefined;
  const def = type === "custom"
    ? palette.customSteps.find((c) => c.step_key === binding.custom_step) : undefined;
  const forced = Boolean(cap?.approval_forced_manual || def?.approval_forced_manual
    || approval.forced_manual === true);
  const unset = (name: string) => [undefined, null, ""].includes(inputs[name] as never);
  const agent = agentOf(step, type, palette);

  // What the named agent lacks of what this step needs — the warning badge.
  let lacks: string[] = [];
  if (agent.kind === "named" && agent.role_key) {
    const held = palette.agents.find((a) => a.role_key === agent.role_key);
    lacks = [
      ...neededSkills(step, palette).filter((s) => !held?.skills.includes(s)),
      ...neededMcp(step, palette).filter((m) => !held?.mcp.includes(m)),
    ];
  }
  return {
    id: step.id,
    title: stepTitle(step, palette),
    type,
    icon: type === "builtin" ? String(binding.capability) : type,
    agent,
    badges: {
      approval: isGateStep(step, palette) || forced || approval.mode === "manual",
      forced,
      byPolicy: binding.capability === "deploy" && approval.follows_policy === true,
      inputs: new Set([...Object.keys(inputs), ...required]).size,
      unwired: required.filter(unset),
      skills: type === "agent" ? strings(binding.skills).length : 0,
      mcp: type === "agent" ? strings(binding.mcp ?? binding.mcp_servers).length : 0,
      lacks,
    },
  };
}

/** An id no step in `spec` has yet: `base`, else `base_2`, `base_3`, … */
export function freeStepId(spec: Spec, base: string): string {
  const clean = String(base || "step").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "")
    || "step";
  const taken = new Set((spec.steps ?? []).map((s) => s.id));
  if (!taken.has(clean)) return clean;
  for (let n = 2; ; n += 1) if (!taken.has(`${clean}_${n}`)) return `${clean}_${n}`;
}

/**
 * A new step for a palette pick, ready to INSERT. Its inputs start empty: the store wires them
 * where it places the step (the INSERT action's `palette`), because only the position decides
 * which earlier step makes what it needs.
 */
export function newStepFor(item: PaletteItem, spec: Spec, palette: Palette): SpecStep {
  if (item.capability) {
    const cap = palette.capabilities.find((c) => c.key === item.capability);
    return {
      id: freeStepId(spec, item.capability), kind: "capability", next: [],
      binding: { capability: item.capability, inputs: {} },
      ...(cap?.approval_forced_manual ? { approval: { mode: "manual" } } : {}),
    };
  }
  if (item.customStep) {
    const def = palette.customSteps.find((c) => c.step_key === item.customStep);
    return {
      id: freeStepId(spec, item.customStep), kind: "capability", next: [],
      binding: { custom_step: item.customStep, inputs: {} },
      ...(def?.approval_forced_manual ? { approval: { mode: "manual" } } : {}),
    };
  }
  if (item.agentTask) {
    // "Ask at start" by default: say what you want when you start it.
    return {
      id: freeStepId(spec, "agent_task"), kind: "capability", next: [],
      binding: { agent_work: true, inputs: { instructions: { from_input: "instructions" } } },
    };
  }
  const block = palette.blocks.find((b) => b.kind === item.block);
  if (item.block === "decision") {
    return { id: freeStepId(spec, "approval"), kind: "decision", next: [], question: "",
             guidance: "", approver_role: palette.humanRole || "human",
             ...(block?.defaults ?? {}) };
  }
  if (item.block === "timer") {
    return { id: freeStepId(spec, "wait"), kind: "timer", next: [], wait_seconds: 3600,
             ...(block?.defaults ?? {}) };
  }
  if (item.block === "subworkflow") {
    return { id: freeStepId(spec, "run_workflow"), kind: "subworkflow", next: [], binding: {},
             ...(block?.defaults ?? {}) };
  }
  throw new Error(`newStepFor: nothing to make from ${JSON.stringify(item)}`);
}
