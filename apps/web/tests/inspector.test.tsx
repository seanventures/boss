// @vitest-environment happy-dom
/**
 * The inspector: every property of a step, editable, with no paragraphs — and each edit ONE
 * dispatch, so undo, the node and the JSON follow without the inspector knowing about them.
 *
 * Mounted for real against a minimal store (tests/_helpers/workflow-editor-fakes.tsx stands in
 * for the S1 model until it lands). What is pinned is behaviour an operator sees: a locked
 * approval says why, scope chips turn amber when the agent cannot use them, Grant writes once,
 * a gate cannot lose its question.
 */
import React, { act, useReducer } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EditorAction, EditorState, Spec } from "../lib/workflow-editor-types";

vi.mock("../lib/workflow-editor-types", async (orig) => {
  const real = await orig<typeof import("../lib/workflow-editor-types")>();
  const f = await import("./_helpers/workflow-editor-fakes");
  return {
    ...real,
    stepView: f.fakeStepView, upstreamOf: f.fakeUpstreamOf, wireInputs: f.fakeWireInputs,
    lineOfStep: f.fakeLineOfStep, stepAtLine: f.fakeStepAtLine, FlowLane: f.FakeFlowLane,
    renameWorkflow: vi.fn(async () => undefined),
    fetchLibrary: vi.fn(async () => [
      { definition_id: "d2", workflow_key: "other", name: "Other flow", is_catalogue: false, version: 3,
        step_count: 1, steps: [], run_count: 0, active_version_id: "ver_other_3" },
    ]),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Inspector } = await import("../app/workflows/editor/inspector/inspector");
const types = await import("../lib/workflow-editor-types");
const { PALETTE, fakeReducer, makeState, setValue, q } = await import("./_helpers/workflow-editor-fakes");
const set = async (el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, v: string) => {
  await act(async () => { setValue(el, v); });
};
const { TOAST_EVENT } = await import("../lib/api-write");

const SPEC: Spec = {
  entry_step: "research",
  input_schema: { properties: { topic: { type: "string", title: "Topic", "x-keep": 1 } }, required: ["topic"] },
  steps: [
    { id: "research", kind: "capability", next: ["write"],
      binding: { capability: "research", inputs: { topic: { from_input: "topic" } } }, approval: { mode: "auto" } },
    { id: "write", kind: "capability", next: ["build"], captain: "qa",
      binding: { agent_work: true, inputs: { instructions: { from_input: "instructions" } },
                 skills: ["web.search", "github.create_pr"] } },
    { id: "build", kind: "capability", next: ["deploy"], binding: { capability: "build", inputs: {} } },
    { id: "deploy", kind: "capability", next: ["ticket"],
      binding: { capability: "deploy", inputs: {} }, approval: { mode: "manual" } },
    { id: "ticket", kind: "capability", next: ["gate"], captain: "qa",
      binding: { custom_step: "file_ticket", inputs: {} } },
    { id: "gate", kind: "decision", next: [], question: "", approver_role: "human", binding: {} },
    { id: "pause", kind: "timer", wait_seconds: 7200, binding: {} },
    { id: "child", kind: "subworkflow", binding: { workflow_version_id: "ver_other_3" } },
  ],
};

let root: Root | null = null;
let host: HTMLElement | null = null;
const box: { state: EditorState | null; actions: EditorAction[] } = { state: null, actions: [] };
const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);

function Harness({ initial }: { initial: EditorState }) {
  const [state, raw] = useReducer(fakeReducer, initial);
  box.state = state;
  const dispatch = (a: EditorAction) => { box.actions.push(a); raw(a); };
  return <Inspector state={state} palette={PALETTE} dispatch={dispatch} projectId="p1" actor="ceo"
                    onClose={() => dispatch({ type: "SELECT", id: null })} />;
}

async function mount(selection: string | null, over: Partial<EditorState> = {}, spec: Spec = SPEC) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const initial = { ...makeState(JSON.parse(JSON.stringify(spec))), selection, ...over };
  await act(async () => root!.render(<Harness initial={initial} />));
  box.actions = [];
  return host;
}

const click = async (el: Element | null) => {
  expect(el).not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
};
const settle = async () => { for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const step = (id: string) => box.state!.spec.steps.find((s) => s.id === id)!;

beforeEach(() => {
  box.state = null; box.actions = []; toasts.length = 0;
  window.addEventListener(TOAST_EVENT, onToast);
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null;
  window.removeEventListener(TOAST_EVENT, onToast);
  vi.unstubAllGlobals();
});

describe("approval", () => {
  it("a forced-manual step shows the switch on, locked, and says why", async () => {
    const el = await mount("deploy");
    const sw = q<HTMLButtonElement>(el, "wf-approval-switch")!;
    expect(sw.disabled).toBe(true);
    expect(sw.getAttribute("aria-checked")).toBe("true");
    expect(sw.title).toBe("Risk 3 — always needs a person");
  });

  it("a free step toggles with one PATCH_STEP", async () => {
    const el = await mount("research");
    await click(q(el, "wf-approval-switch"));
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "research", patch: { approval: "manual" } }]);
    expect(step("research").approval).toMatchObject({ mode: "manual" });
  });

  it("a gate has no approval switch — it IS the approval", async () => {
    const el = await mount("gate");
    expect(q(el, "wf-approval-switch")).toBeNull();
  });
});

describe("skills and MCP", () => {
  it("are offered on an agent task only; other steps show what they are fixed to use", async () => {
    let el = await mount("write");
    expect(q(el, "wf-sec-scope")).not.toBeNull();
    expect(q(el, "wf-sec-uses")).toBeNull();
    act(() => root?.unmount()); host?.remove();
    el = await mount("build");
    expect(q(el, "wf-sec-scope")).toBeNull();
    expect(q(el, "wf-sec-uses")!.textContent).toContain("github.create_pr");
  });

  it("a chip the agent does not hold is amber, and changing the agent re-marks the chips", async () => {
    const el = await mount("write");
    const chip = q(el, "wf-scope-chip-skill-github.create_pr")!;
    expect(chip.hasAttribute("data-amber")).toBe(true);
    expect(chip.title).toBe("Not held by QA Lead");
    expect(q(el, "wf-scope-chip-skill-web.search")!.hasAttribute("data-amber")).toBe(false);

    await click(q(el, "wf-agent-trigger"));
    await click(document.querySelector('[data-testid="wf-agent-option-dev"]'));
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "write", patch: { captain: "dev" } }]);
    expect(q(el, "wf-scope-chip-skill-github.create_pr")!.hasAttribute("data-amber")).toBe(false);
  });

  it("the + Skill picker offers only what the agent holds, and adds with one dispatch", async () => {
    const el = await mount("write");
    await click(q(el, "wf-scope-add-mcp"));
    const options = [...document.querySelectorAll('[data-testid^="wf-scope-opt-mcp-"]')].map((o) => o.getAttribute("data-testid"));
    expect(options).toEqual(["wf-scope-opt-mcp-github"]);
    await click(document.querySelector('[data-testid="wf-scope-opt-mcp-github"]'));
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "write", patch: { mcp: ["github"] } }]);
  });

  it("an empty scope says the agent may use anything it holds", async () => {
    const el = await mount("write");
    expect(q(el, "wf-scope-mcp")!.textContent).toContain("Any it holds");
  });
});

describe("the agent", () => {
  it("lists Auto first, then the roster with what each has or misses for this step", async () => {
    const el = await mount("ticket");
    await click(q(el, "wf-agent-trigger"));
    const pop = document.querySelector('[data-testid="wf-agent-pop"]')!;
    const rows = [...pop.querySelectorAll('[role="option"]')].map((o) => o.textContent);
    expect(rows[0]).toBe("Auto");
    expect(rows[1]).toContain("QA Lead");
    expect(rows[1]).toContain("✗ missing jira.file");
    expect(rows[2]).toContain("✓ has jira.file");
    expect(pop.textContent).toContain("Manage agents ↗");
  });

  it("a built-in step's default is You; a gate's is A person", async () => {
    let el = await mount("build");
    expect(q(el, "wf-agent-trigger")!.textContent).toContain("You");
    act(() => root?.unmount()); host?.remove();
    el = await mount("gate");
    expect(q(el, "wf-agent-trigger")!.textContent).toContain("A person");
  });

  it("Grant assigns the missing skill with ONE write, as the signed-in member", async () => {
    const calls: Record<string, unknown>[] = [];
    let release: () => void = () => {};
    vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
      if (String(url) === "/api/skills") {
        calls.push(JSON.parse(String(init?.body)));
        await new Promise<void>((r) => { release = r; });
        return Response.json({ ok: true });
      }
      return Response.json({});
    }));
    const el = await mount("ticket");
    const grant = q<HTMLButtonElement>(el, "wf-grant-jira.file")!;
    expect(el.textContent).toContain("Lacks jira.file");
    await act(async () => { grant.click(); grant.click(); });
    await settle();
    release();
    await settle();
    expect(calls).toEqual([{ action: "assign", project_id: "p1", role_key: "qa", skill_id: "jira.file", actor: "ceo" }]);
    expect(toasts).toContain("Granted jira.file to QA Lead");
    expect(q(el, "wf-grant")).toBeNull();
  });
});

describe("a gate", () => {
  it("cannot lose its question: empty is flagged, and the server's error lands on the field", async () => {
    const el = await mount("gate", {
      problems: [{ step_id: "gate", field: "question", message: "Say what is being approved" }],
    });
    const field = q(el, "wf-gate-question")!.closest("label")!;
    expect(field.textContent).toContain("Say what is being approved");
    expect(q(el, "wf-step-problems")).toBeNull();
    await set(q<HTMLTextAreaElement>(el, "wf-gate-question")!, "Ship it?");
    await settle();
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "gate", patch: { config: { question: "Ship it?" } } }]);
  });

  it("a Wait reads and writes its duration in a unit", async () => {
    const el = await mount("pause");
    expect(q<HTMLInputElement>(el, "wf-wait-amount")!.value).toBe("2");
    await set(q<HTMLInputElement>(el, "wf-wait-amount")!, "3");
    await settle();
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "pause", patch: { config: { wait_seconds: 10800 } } }]);
  });

  it("Run another workflow picks from the library, pinned to the active version", async () => {
    const el = await mount("child");
    await settle();
    const select = q<HTMLSelectElement>(el, "wf-subworkflow")!;
    expect(select.value).toBe("ver_other_3");
    expect(select.closest("label")!.textContent).toContain("v3");
  });
});

describe("inputs", () => {
  it("each source change is one PATCH_STEP; Auto-wire is one SET_SPEC and marks what it wired", async () => {
    const el = await mount("deploy", {}, {
      ...SPEC,
      steps: SPEC.steps.map((s) => (s.id === "build" ? { ...s, binding: { capability: "build", inputs: {} } } : s)),
    });
    expect(q(el, "wf-input-pr_url")!.hasAttribute("data-unwired")).toBe(true);
    await click(q(el, "wf-autowire"));
    expect(box.actions.map((a) => a.type)).toEqual(["SET_SPEC"]);
    expect((step("deploy").binding as { inputs: unknown }).inputs).toEqual({ pr_url: { from_step: "build", field: "pr_url" } });
    expect(q(el, "wf-input-pr_url")!.textContent).toContain("auto");

    box.actions = [];
    await set(q<HTMLSelectElement>(el, "wf-input-source-pr_url")!, "fixed");
    await settle();
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "deploy", patch: { input: { name: "pr_url", source: { value: "" } } } }]);
    expect(q(el, "wf-input-pr_url")!.textContent).not.toContain("auto");
  });

  it("optional inputs wait behind `n optional`", async () => {
    const el = await mount("research");
    expect(q(el, "wf-input-depth")).toBeNull();
    await click(q(el, "wf-inputs-optional"));
    expect(q(el, "wf-input-depth")).not.toBeNull();
  });
});

describe("the header", () => {
  it("renames the id with one RENAME_ID, refusing a clash or a bad shape", async () => {
    const el = await mount("build");
    await click(el.querySelector('[aria-label="Rename step id"]'));
    const input = q<HTMLInputElement>(el, "wf-step-id-input")!;
    await set(input, "research");
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(el.textContent).toContain("Id in use");
    await set(input, "has space");
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(el.textContent).toContain("Letters, numbers, . _ - only");
    expect(box.actions.filter((a) => a.type === "RENAME_ID")).toEqual([]);
    await set(input, "compile");
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(box.actions.filter((a) => a.type === "RENAME_ID")).toEqual([{ type: "RENAME_ID", from: "build", to: "compile" }]);
  });

  it("Esc closes the drawer", async () => {
    const el = await mount("build");
    await act(async () => {
      q(el, "wf-inspector")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(box.state!.selection).toBeNull();
  });
});

describe("workflow settings", () => {
  it("adds a start-form field in the shape the workflow was written in, keeping unknown keys", async () => {
    const el = await mount(null);
    await click(q(el, "wf-form-add"));
    const input = q<HTMLInputElement>(el, "wf-form-new")!;
    await set(input, "budget");
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(box.actions.map((a) => a.type)).toEqual(["SET_INPUT_SCHEMA"]);
    expect(box.state!.spec.input_schema).toEqual({
      properties: { topic: { type: "string", title: "Topic", "x-keep": 1 }, budget: { type: "string" } },
      required: ["topic"],
    });
  });

  it("a name-to-type start form stays that shape and cannot mark a field required", async () => {
    const el = await mount(null, {}, { ...SPEC, input_schema: { topic: "string" } });
    expect(q<HTMLInputElement>(el, "wf-form-required-topic")!.disabled).toBe(true);
    await set(el.querySelector<HTMLSelectElement>('[aria-label="Type of topic"]')!, "number");
    await settle();
    expect(box.state!.spec.input_schema).toEqual({ topic: "number" });
  });

  it("renames a published workflow without a version when nothing else changed", async () => {
    const el = await mount(null);
    const name = q<HTMLInputElement>(el, "wf-settings-name")!;
    await set(name, "Better flow");
    await act(async () => { name.dispatchEvent(new FocusEvent("focusout", { bubbles: true })); });
    await settle();
    expect(types.renameWorkflow).toHaveBeenCalledWith("p1", "def_own", { name: "Better flow" });
    expect(box.state!.loadedMeta.name).toBe("Better flow");
  });
});
