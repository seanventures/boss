/**
 * Stand-ins for the editor model (stream S1) and the lane (S2), for the inspector, JSON and
 * save-flow tests while those streams land.
 *
 * They follow the frozen contract in lib/workflow-editor-types.ts and the spec's rules (the agent
 * display rule, "broken JSON never touches the spec"), and nothing more. At integration the
 * `vi.mock` of the S1 functions can be dropped so the same tests run against the real store;
 * the FlowLane stand-in stays, because these tests are about the inspector, not the lane.
 */
import React from "react";

import type {
  EditorAction, EditorMeta, EditorState, InputSource, Palette, Spec, SpecStep, StepView,
} from "../../lib/workflow-editor-types";

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export const PALETTE: Palette = {
  capabilities: [
    { key: "research", label: "Research", summary: "Research a topic", risk_tier: 0,
      required_inputs: ["topic"], optional_inputs: ["depth"], produces: ["dossier"],
      skills: ["web.search"] },
    { key: "build", label: "Build", summary: "Build it", risk_tier: 1, required_inputs: ["spec"],
      optional_inputs: [], produces: ["pr_url"], skills: ["github.create_pr"] },
    { key: "deploy", label: "Deploy", summary: "Ship it", risk_tier: 3, required_inputs: ["pr_url"],
      optional_inputs: [], produces: ["url"], skills: ["deploy.run"], approval_forced_manual: true },
  ],
  customSteps: [
    { step_key: "file_ticket", label: "File a ticket", binding_kind: "skill", skill_id: "jira.file",
      inputs: [{ name: "ticket", required: true }], produces: ["ticket_url"], risk_tier: 0, usable: true },
  ],
  blocks: [
    { kind: "decision", label: "Approval gate", summary: "", defaults: { approver_role: "human" },
      configure: [{ key: "question", label: "What is being approved?", type: "text" },
                  { key: "guidance", label: "What should the reviewer check?", type: "text" },
                  { key: "approver_role", label: "Who approves", type: "text" }] },
    { kind: "timer", label: "Wait", summary: "", defaults: { wait_seconds: 3600 },
      configure: [{ key: "wait_seconds", label: "Wait for (seconds)", type: "number" }] },
    { kind: "subworkflow", label: "Run another workflow", summary: "", defaults: {},
      configure: [{ key: "workflow_version_id", label: "Which workflow", type: "workflow" }] },
  ],
  agents: [
    { role_key: "qa", name: "QA Lead", skills: ["web.search"], mcp: ["github"] },
    { role_key: "dev", name: "Developer", skills: ["web.search", "github.create_pr", "jira.file"], mcp: ["github", "linear"] },
  ],
  humanRole: "human",
  riskFloor: 2,
  bindable: { skills: [], mcp_servers: [] },
};

/** The spec's agent display rule and badges, as S1 will implement them. */
export function fakeStepView(step: SpecStep, palette: Palette): StepView {
  const b = asObj(step.binding);
  const cap = palette.capabilities.find((c) => c.key === b.capability);
  const mine = palette.customSteps.find((c) => c.step_key === b.custom_step);
  const type: StepView["type"] = step.kind === "decision" ? "gate" : step.kind === "timer" ? "wait"
    : step.kind === "subworkflow" ? "subworkflow" : step.kind === "parallel" ? "parallel"
    : step.kind === "condition" ? "condition" : step.kind === "join" ? "join"
    : step.kind === "bounded_loop" ? "loop"
    : b.agent_work ? "agent" : mine ? "custom" : cap ? "builtin" : "other";
  const title = (typeof step.title === "string" && step.title) || cap?.label || mine?.label
    || (type === "agent" ? "Agent task" : type === "gate" ? "Approval gate" : step.id);
  const captain = typeof step.captain === "string" ? step.captain : "";
  const roster = palette.agents.find((a) => a.role_key === captain);
  const agent: StepView["agent"] = captain === "human" ? { kind: "person", label: "A person", tip: "" }
    : captain ? { kind: "named", role_key: captain, label: roster?.name ?? captain, tip: "" }
    : type === "gate" ? { kind: "person", label: "A person", tip: "" }
    : type === "agent" || type === "custom" ? { kind: "auto", label: "Auto", tip: "" }
    : { kind: "you", label: "You", tip: "" };
  const needs = type === "agent" ? (Array.isArray(b.skills) ? b.skills.map(String) : [])
    : mine?.skill_id ? [mine.skill_id] : cap?.skills ?? [];
  const lacks = roster ? needs.filter((s) => !roster.skills.includes(s)) : [];
  const wired = asObj(b.inputs);
  const required = cap?.required_inputs ?? mine?.inputs.filter((i) => i.required).map((i) => i.name) ?? [];
  return {
    id: step.id, title, type, icon: String(b.capability ?? type), agent,
    badges: { approval: asObj(step.approval).mode === "manual", forced: Boolean(cap?.approval_forced_manual),
              inputs: required.length, unwired: required.filter((n) => !(n in wired)), skills: 0, mcp: 0, lacks },
  };
}

export const fakeUpstreamOf = (spec: Spec, id: string): string[] => {
  const i = spec.steps.findIndex((s) => s.id === id);
  return spec.steps.slice(0, Math.max(0, i)).map((s) => s.id).reverse();
};

export function fakeWireInputs(spec: Spec, palette: Palette, ids?: string[]) {
  const out = clone(spec);
  const wired: { step_id: string; input: string; source: InputSource }[] = [];
  out.steps.forEach((step, index) => {
    if (ids && !ids.includes(step.id)) return;
    const b = asObj(step.binding);
    const cap = palette.capabilities.find((c) => c.key === b.capability);
    if (!cap) return;
    const inputs = { ...asObj(b.inputs) };
    for (const name of cap.required_inputs) {
      if (name in inputs) continue;
      const from = [...out.steps.slice(0, index)].reverse().find((s) =>
        palette.capabilities.find((c) => c.key === asObj(s.binding).capability)?.produces.includes(name));
      if (!from) continue;
      inputs[name] = { from_step: from.id, field: name };
      wired.push({ step_id: step.id, input: name, source: { from_step: from.id, field: name } });
    }
    step.binding = { ...b, inputs };
  });
  return { spec: out, wired };
}

export function fakeLineOfStep(text: string, id: string): number | null {
  const lines = text.split("\n");
  const i = lines.findIndex((l) => new RegExp(`^\\s*"id"\\s*:\\s*"${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`).test(l));
  return i < 0 ? null : i + 1;
}

export function fakeStepAtLine(text: string, line: number): string | null {
  const lines = text.split("\n");
  // The nearest step "id" at or above the line, if the line is inside that step's object.
  for (let i = Math.min(line, lines.length) - 1; i >= 0; i -= 1) {
    const m = /^(\s*)"id"\s*:\s*"([^"]+)"/.exec(lines[i]);
    if (!m) continue;
    const indent = m[1].length;
    for (let j = i + 1; j < line; j += 1) {
      const t = lines[j];
      if (t.trim() && t.length - t.trimStart().length < indent) return null;
    }
    return m[2];
  }
  return null;
}

/* ── a minimal store ───────────────────────────────────────────────────────────── */

export const META: EditorMeta = {
  definition_id: "def_own", workflow_key: "my_flow", name: "My flow", description: "",
  is_catalogue: false, from_workflow_key: null, version: 3, copy: null, readOnlyVersion: null,
};

export function makeState(spec: Spec, meta: Partial<EditorMeta> = {}, over: Partial<EditorState> = {}): EditorState {
  const m = { ...META, ...meta };
  return { spec, loaded: clone(spec), meta: m, loadedMeta: m, selection: null, problems: [], summary: null,
           past: [], future: [], jsonText: null, jsonError: null, ...over };
}

function patchStep(step: SpecStep, patch: Obj): SpecStep {
  const s: SpecStep = clone(step);
  const b = { ...asObj(s.binding) };
  if ("title" in patch) { if (patch.title) s.title = String(patch.title); else delete s.title; }
  if ("captain" in patch) { if (patch.captain) s.captain = String(patch.captain); else delete s.captain; }
  if (patch.approval) s.approval = { ...asObj(s.approval), mode: patch.approval };
  if (patch.approvalMore) s.approval = { ...asObj(s.approval), ...asObj(patch.approvalMore) };
  if (patch.skills) b.skills = patch.skills;
  if (patch.mcp) b.mcp = patch.mcp;
  if (patch.produces) b.produces = patch.produces;
  if (patch.instructions) {
    const ins = asObj(patch.instructions);
    b.inputs = { ...asObj(b.inputs), instructions: ins.mode === "ask" ? { from_input: "instructions" } : String(ins.text ?? "") };
  }
  if (patch.input) {
    const { name, source } = patch.input as { name: string; source: InputSource | null };
    const inputs = { ...asObj(b.inputs) };
    if (source === null) delete inputs[name];
    else inputs[name] = "value" in source ? source.value : source;
    b.inputs = inputs;
  }
  if (patch.config) Object.assign(s, patch.config);
  if (patch.max_attempts) s.max_attempts = patch.max_attempts;
  s.binding = b;
  return s;
}

/** Line of a JSON.parse error, from the position the engine reports. */
function errorLine(text: string, e: unknown): { line: number; message: string } {
  const message = String((e as Error)?.message ?? e);
  const pos = /position (\d+)/.exec(message);
  const lineMatch = /line (\d+)/.exec(message);
  const line = pos ? text.slice(0, Number(pos[1])).split("\n").length : lineMatch ? Number(lineMatch[1]) : 1;
  return { line, message: message.replace(/^JSON\.parse: /, "").split(" in JSON")[0] };
}

export function fakeReducer(s: EditorState, a: EditorAction): EditorState {
  const edit = (spec: Spec): EditorState => ({ ...s, spec, past: [...s.past, s.spec], future: [], jsonText: null, jsonError: null });
  switch (a.type) {
    case "LOAD": return makeState(a.spec, a.meta);
    case "SET_SPEC": return { ...edit(a.spec) };
    case "SET_JSON_TEXT":
      try {
        const spec = JSON.parse(a.text) as Spec;
        return { ...s, spec, past: [...s.past, s.spec], jsonText: a.text, jsonError: null };
      } catch (e) {
        return { ...s, jsonText: a.text, jsonError: errorLine(a.text, e) };
      }
    case "PATCH_STEP":
      return edit({ ...s.spec, steps: s.spec.steps.map((st) => (st.id === a.id ? patchStep(st, a.patch as Obj) : st)) });
    case "RENAME_ID":
      return { ...edit({ ...s.spec, steps: s.spec.steps.map((st) => (st.id === a.from ? { ...st, id: a.to } : st)) }),
               selection: s.selection === a.from ? a.to : s.selection };
    case "REMOVE": return { ...edit({ ...s.spec, steps: s.spec.steps.filter((st) => st.id !== a.id) }), selection: null };
    case "DUPLICATE": return s;
    case "SET_META": return { ...s, meta: { ...s.meta, ...a.patch } };
    case "SET_INPUT_SCHEMA": return edit({ ...s.spec, input_schema: a.schema });
    case "SELECT": return { ...s, selection: a.id };
    case "SET_PROBLEMS": return { ...s, problems: a.result.problems, summary: a.result.summary };
    case "SAVED": return { ...s, spec: a.spec, loaded: clone(a.spec), meta: a.meta, loadedMeta: a.meta };
    case "UNDO": case "REDO": case "INSERT": case "MOVE": return s;
    default: return s;
  }
}

/** The lane, as far as these tests need it: one button per step, labelled with its agent. */
export function FakeFlowLane({ spec, palette, selection, onSelect }: {
  spec: Spec; palette: Palette | null; selection?: string | null; onSelect?(id: string | null): void;
}) {
  return (
    <div data-testid="lane">
      {spec.steps.map((s) => (
        <button key={s.id} type="button" data-testid={`node-${s.id}`} aria-pressed={selection === s.id}
                onClick={() => onSelect?.(s.id)}>
          {palette ? fakeStepView(s, palette).agent.label : s.id}
        </button>
      ))}
    </div>
  );
}

/* ── DOM helpers ───────────────────────────────────────────────────────────────── */

export function setValue(el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const proto = Object.getPrototypeOf(el) as object;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

export const q = <T extends Element = HTMLElement>(root: ParentNode, testId: string) =>
  root.querySelector<T & Element>(`[data-testid="${testId}"]`) as T | null;
