/**
 * What the inspector READS from a spec step. Writes go through `PATCH_STEP` (the store owns how
 * a patch lands in the spec); reads live here so every inspector section agrees on where a
 * field is kept: `binding.inputs`, `binding.skills`, `binding.mcp`, `approval.*`, and a block's
 * settings at the top level of the step.
 *
 * Nothing here decides who runs a step or what it lacks. That is `stepView` (S1), the one rule
 * the node, the library and the run lane share.
 */
import type {
  BlockDef, Capability, CustomStepDef, InputSource, Palette, Spec, SpecStep, StepView,
} from "../../../../lib/workflow-editor-types";

export type Obj = Record<string, unknown>;

export const asObj = (value: unknown): Obj =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};

export const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.map((v) => String(v).trim()).filter(Boolean) : [];

export const bindingOf = (step: SpecStep): Obj => asObj(step.binding);

export function capabilityOf(step: SpecStep, palette: Palette): Capability | undefined {
  const key = bindingOf(step).capability;
  return key ? palette.capabilities.find((c) => c.key === String(key)) : undefined;
}

export function customStepOf(step: SpecStep, palette: Palette): CustomStepDef | undefined {
  const key = bindingOf(step).custom_step;
  return key ? palette.customSteps.find((c) => c.step_key === String(key)) : undefined;
}

export function blockOf(step: SpecStep, palette: Palette): BlockDef | undefined {
  return palette.blocks.find((b) => b.kind === step.kind);
}

export const isAgentTask = (step: SpecStep): boolean => Boolean(bindingOf(step).agent_work);

/** Which step types carry an approval switch: the ones that do work. A gate IS an approval. */
export const APPROVABLE: StepView["type"][] = ["builtin", "agent", "custom"];

/** Which step types name an agent at all. Blocks that only wait or route have nobody to name. */
export const AGENTED: StepView["type"][] = ["builtin", "agent", "custom", "gate"];

/** A step whose approval the server will force to manual whatever the switch says. */
export function forcedApproval(step: SpecStep, palette: Palette): { forced: boolean; risk: number } {
  const cap = capabilityOf(step, palette);
  const mine = customStepOf(step, palette);
  const risk = cap?.risk_tier ?? mine?.risk_tier ?? 0;
  return { forced: Boolean(cap?.approval_forced_manual || mine?.approval_forced_manual), risk };
}

export const approvalOf = (step: SpecStep): Obj => asObj(step.approval);

/** A Deploy step that lets the environment's deploy policy answer for it
 *  (`approval.follows_policy`, deploy_policy.follows_policy). Without it a Deploy step promises
 *  a person, whatever any environment's policy allows. */
export const isDeploy = (step: SpecStep): boolean => bindingOf(step).capability === "deploy";
export const followsPolicy = (step: SpecStep): boolean =>
  isDeploy(step) && approvalOf(step).follows_policy === true;

export const approvalMode = (step: SpecStep): "auto" | "manual" =>
  String(approvalOf(step).mode ?? "auto").toLowerCase() === "manual" ? "manual" : "auto";

/** The agent task's instruction: read from the start form, or fixed here. */
export function instructionsOf(step: SpecStep): { mode: "ask" | "fixed"; text: string; field: string } {
  const wire = asObj(bindingOf(step).inputs).instructions;
  if (wire && typeof wire === "object" && !Array.isArray(wire)) {
    const o = wire as Obj;
    if (typeof o.from_input === "string") return { mode: "ask", text: "", field: o.from_input };
    if ("value" in o) return { mode: "fixed", text: String(o.value ?? ""), field: "instructions" };
  }
  if (typeof wire === "string") return { mode: "fixed", text: wire, field: "instructions" };
  return { mode: "ask", text: "", field: "instructions" };
}

export const scopedSkills = (step: SpecStep): string[] => strings(bindingOf(step).skills);
export const scopedMcp = (step: SpecStep): string[] => {
  const b = bindingOf(step);
  return strings(b.mcp ?? b.mcp_servers);
};

export type DeclaredInput = { name: string; required: boolean; extra?: boolean };

/** Every input the step reads: what it declares, then anything wired that it does not declare
 *  (shown rather than hidden, because a save carries it). An agent task's `instructions` has its
 *  own section, so it is not repeated here. */
export function declaredInputs(step: SpecStep, palette: Palette): DeclaredInput[] {
  const out: DeclaredInput[] = [];
  const cap = capabilityOf(step, palette);
  const mine = customStepOf(step, palette);
  if (cap) {
    for (const name of cap.required_inputs) out.push({ name, required: true });
    for (const name of cap.optional_inputs) out.push({ name, required: false });
  } else if (mine) {
    for (const input of mine.inputs) out.push({ name: input.name, required: input.required });
  }
  const wired = asObj(bindingOf(step).inputs);
  for (const name of Object.keys(wired)) {
    if (isAgentTask(step) && name === "instructions") continue;
    if (!out.some((i) => i.name === name)) out.push({ name, required: false, extra: true });
  }
  return out;
}

/** Where one input comes from, as an InputSource, or null when it is not set. */
export function sourceOf(step: SpecStep, name: string): InputSource | null {
  const wired = asObj(bindingOf(step).inputs);
  if (!(name in wired)) return null;
  const wire = wired[name];
  if (wire === undefined || wire === null) return null;
  if (typeof wire === "object" && !Array.isArray(wire)) {
    const o = wire as Obj;
    if (typeof o.from_input === "string") return { from_input: o.from_input };
    if (typeof o.from_step === "string") return { from_step: o.from_step, field: String(o.field ?? "") };
    if ("value" in o) return { value: o.value };
  }
  return { value: wire };
}

/** What a step makes, for wiring later steps and for the `Makes` chips. */
export function producesOf(step: SpecStep, palette: Palette): string[] {
  if (isAgentTask(step)) return strings(bindingOf(step).produces);
  const cap = capabilityOf(step, palette);
  if (cap) return [...cap.produces];
  const mine = customStepOf(step, palette);
  if (mine) return [...mine.produces];
  return strings(bindingOf(step).produces);
}

/** What a non-agent step is fixed to use, for the read-only `Uses` row. */
export function usesOf(step: SpecStep, palette: Palette): string[] {
  const cap = capabilityOf(step, palette);
  if (cap) return [...cap.skills];
  const mine = customStepOf(step, palette);
  if (mine) {
    if (mine.binding_kind === "skill" && mine.skill_id) return [mine.skill_id];
    const server = palette.bindable.mcp_servers.find((s) => s.id === mine.mcp_server_id);
    return [[server?.name ?? mine.mcp_server_id, mine.mcp_tool_name].filter(Boolean).join(" · ")];
  }
  return [];
}

/** The start form's fields, in whichever of the two shapes the workflow wrote it. */
export type FormField = { name: string; title: string; type: FieldType; required: boolean };
export type FieldType = "text" | "long" | "number" | "yesno";

export function isJsonSchema(schema: Obj): boolean {
  return "properties" in schema || "required" in schema;
}

export function formFields(spec: Spec): FormField[] {
  const schema = asObj(spec.input_schema);
  if (!Object.keys(schema).length) return [];
  if (isJsonSchema(schema)) {
    const props = asObj(schema.properties);
    const required = strings(schema.required);
    const names = [...Object.keys(props), ...required.filter((r) => !(r in props))];
    return names.map((name) => {
      const p = asObj(props[name]);
      const type: FieldType = p.type === "number" || p.type === "integer" ? "number"
        : p.type === "boolean" ? "yesno"
        : p.format === "textarea" ? "long" : "text";
      return { name, title: String(p.title ?? ""), type, required: required.includes(name) };
    });
  }
  return Object.entries(schema).map(([name, raw]) => {
    const t = String(raw ?? "string").toLowerCase();
    const type: FieldType = t === "number" || t === "integer" ? "number"
      : t === "boolean" ? "yesno" : t === "text" || t === "textarea" ? "long" : "text";
    return { name, title: "", type, required: false };
  });
}

/** The start form written back in the shape it was loaded in. Unknown keys on the schema and on
 *  each property survive: this edits the fields, it does not rebuild the schema. */
export function withFormFields(spec: Spec, fields: FormField[]): Obj {
  const schema = asObj(spec.input_schema);
  const flat = Object.keys(schema).length > 0 && !isJsonSchema(schema);
  if (flat) {
    const FLAT: Record<FieldType, string> = { text: "string", long: "text", number: "number", yesno: "boolean" };
    return Object.fromEntries(fields.map((f) => {
      const was = schema[f.name];
      const same = was !== undefined && formFields({ steps: [], input_schema: { [f.name]: was } })[0]?.type === f.type;
      return [f.name, same ? was : FLAT[f.type]];
    }));
  }
  const props = asObj(schema.properties);
  const properties: Obj = {};
  for (const f of fields) {
    const p: Obj = { ...asObj(props[f.name]) };
    if (f.type === "number") { p.type = p.type === "integer" ? "integer" : "number"; delete p.format; }
    else if (f.type === "yesno") { p.type = "boolean"; delete p.format; }
    else {
      p.type = "string";
      if (f.type === "long") p.format = "textarea";
      else if (p.format === "textarea") delete p.format;
    }
    if (f.title.trim()) p.title = f.title.trim(); else delete p.title;
    properties[f.name] = p;
  }
  const out: Obj = { ...schema, properties };
  const required = fields.filter((f) => f.required).map((f) => f.name);
  if (required.length) out.required = required; else delete out.required;
  return out;
}

/** Can a start form of this shape say a field is required? The name→type shape cannot. */
export function formCanRequire(spec: Spec): boolean {
  const schema = asObj(spec.input_schema);
  return !Object.keys(schema).length || isJsonSchema(schema);
}

export const STEP_ID = /^[A-Za-z0-9._-]+$/;

export const humanise = (key: string): string => {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : key;
};
