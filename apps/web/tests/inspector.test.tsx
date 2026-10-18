// @vitest-environment happy-dom
/**
 * The inspector: every property of a step, editable, with no paragraphs — and each edit ONE
 * dispatch, so undo, the node and the JSON follow without the inspector knowing about them.
 *
 * Mounted for real against the editor's own store (S1's reducer, step view and graph ops). What
 * is pinned is behaviour an operator sees: a locked approval says why, scope chips turn amber
 * when the agent cannot use them, Grant writes once, a gate cannot lose its question, a title
 * takes a space, and every problem the server names shows somewhere.
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
    FlowLane: f.FakeFlowLane,
    renameWorkflow: vi.fn(async () => undefined),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Inspector } = await import("../app/workflows/editor/inspector/inspector");
const types = await import("../lib/workflow-editor-types");
const { PALETTE, reducer, makeState, setValue, q } = await import("./_helpers/workflow-editor-fakes");
type Palette = typeof PALETTE;
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
const box: { state: EditorState | null; actions: EditorAction[]; palette: Palette; refreshes: number;
             refreshed: Palette | null; setOpen: (open: boolean) => void; dispatch: (a: EditorAction) => void } =
  { state: null, actions: [], palette: PALETTE, refreshes: 0, refreshed: null, setOpen: () => {}, dispatch: () => {} };
const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);

/** The editor host, as far as the drawer sees it: the real store, and a palette the host owns
 *  and refetches on request (`refreshed` is what the next fetch returns). */
function Harness({ initial, projectId }: { initial: EditorState; projectId: string }) {
  const [state, raw] = useReducer(reducer, initial);
  const [palette, setPalette] = React.useState(box.palette);
  const [open, setOpen] = React.useState(true);
  box.state = state;
  box.setOpen = setOpen;
  const dispatch = (a: EditorAction) => { box.actions.push(a); raw(a); };
  box.dispatch = dispatch;
  const refreshPalette = async () => { box.refreshes += 1; if (box.refreshed) setPalette(box.refreshed); };
  return !open ? null : (
    <Inspector state={state} palette={palette} dispatch={dispatch} projectId={projectId} actor="ceo"
               refreshPalette={refreshPalette} onClose={() => dispatch({ type: "SELECT", id: null })} />
  );
}

async function mount(selection: string | null, over: Partial<EditorState> = {}, spec: Spec = SPEC,
                     stateOver: Partial<EditorState> = {}, projectId = "p1") {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const initial = { ...makeState(JSON.parse(JSON.stringify(spec))), selection, ...over, ...stateOver };
  await act(async () => root!.render(<Harness initial={initial} projectId={projectId} />));
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
  box.palette = PALETTE; box.refreshes = 0; box.refreshed = null;
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
    expect(sw.title).toBe("Risk 3 — always needs a person, whatever the environment's deploy policy");
  });

  it("a deploy says whether its environment's policy may answer, and the box sets it in one PATCH", async () => {
    const el = await mount("deploy");
    const box_ = q<HTMLInputElement>(el, "wf-approval-policy-box")!;
    expect(box_.checked).toBe(false);
    await click(box_);
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "deploy",
                                   patch: { approvalMore: { follows_policy: true } } }]);
    expect(step("deploy").approval).toMatchObject({ mode: "manual", follows_policy: true });
    // The lock no longer promises a person the policy could replace.
    expect(q<HTMLButtonElement>(el, "wf-approval-switch")!.title)
      .toBe("Asks a person unless this environment's deploy policy ships without asking");
  });

  it("only a deploy step offers the policy box", async () => {
    const el = await mount("research");
    expect(q(el, "wf-approval-policy")).toBeNull();
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

  it("keeps a name the project no longer has, or a person on a step that is not a gate, and says why", async () => {
    // Moved here from the retired builder picker (tests/a-step-says-who-ran-it.test.tsx): a
    // loaded captain the editor cannot offer stays visible rather than silently becoming Auto,
    // because Save would refuse it and the operator should see why first.
    const stale = (captain: string): Spec => ({ ...SPEC, steps: SPEC.steps.map((s) =>
      (s.id === "write" ? { ...s, captain } : s)) });
    let el = await mount("write", {}, stale("engineering_captain"));
    let trigger = q(el, "wf-agent-trigger")!;
    expect(trigger.textContent).toContain("engineering_captain");
    expect(trigger.getAttribute("data-stale")).toBe("true");
    await click(trigger);
    expect(document.querySelector('[data-testid="wf-agent-pop"]')!.textContent).toContain("✗ not on this project");
    act(() => root?.unmount()); host?.remove();
    el = await mount("write", {}, stale("human"));
    trigger = q(el, "wf-agent-trigger")!;
    expect(trigger.getAttribute("data-stale")).toBe("true");
    await click(trigger);
    expect(document.querySelector('[data-testid="wf-agent-pop"]')!.textContent).toContain("✗ only a gate");
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
    box.refreshed = { ...PALETTE, agents: PALETTE.agents.map((a) => (a.role_key === "qa"
      ? { ...a, skills: [...a.skills, "jira.file"] } : a)) };
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

  it("after Grant the HOST's palette is refetched, so Lacks stays gone when the drawer reopens", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: true })));
    // What the next palette fetch returns: QA Lead now holds the skill.
    box.refreshed = { ...PALETTE, agents: PALETTE.agents.map((a) => (a.role_key === "qa"
      ? { ...a, skills: [...a.skills, "jira.file"] } : a)) };
    const el = await mount("ticket");
    await click(q(el, "wf-grant-jira.file"));
    await settle();
    expect(box.refreshes).toBe(1);
    expect(q(el, "wf-grant")).toBeNull();
    await act(async () => box.setOpen(false));
    await act(async () => box.setOpen(true));
    expect(document.body.textContent).not.toContain("Lacks jira.file");
  });

  it("a Grant the server refuses does not refetch, and says so", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "no" }, { status: 403 })));
    const el = await mount("ticket");
    await click(q(el, "wf-grant-jira.file"));
    await settle();
    expect(box.refreshes).toBe(0);
    expect(el.textContent).toContain("Lacks jira.file");
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
    // The API's own row shape, read by the real fetchLibrary/toSummary — not a hand-made summary.
    vi.stubGlobal("fetch", vi.fn(async (url: unknown) => (String(url).includes("view=library")
      ? Response.json({ items: [
          { id: "d2", workflow_key: "other", name: "Other flow", is_catalogue: false, active_version: 3,
            active_version_id: "ver_other_3", step_count: 1, steps: [] },
          { id: "d3", workflow_key: "draft_only", name: "Draft only", is_catalogue: false,
            active_version: null, active_version_id: null, step_count: 0, steps: [] },
        ] })
      : Response.json({}))));
    const el = await mount("child", {}, SPEC, {}, "p-picker");
    await settle();
    const select = q<HTMLSelectElement>(el, "wf-subworkflow")!;
    expect(select.value).toBe("ver_other_3");
    expect(select.closest("label")!.textContent).toContain("v3");
    const options = [...select.options].map((o) => [o.textContent, o.disabled]);
    expect(options).toContainEqual(["Other flow", false]);
    expect(options).toContainEqual(["Draft only", true]);
  });
});

describe("inputs", () => {
  it("each source change is one PATCH_STEP; Auto-wire is the store's WIRE, and its marks are the store's", async () => {
    const el = await mount("deploy", {}, {
      ...SPEC,
      steps: SPEC.steps.map((s) => (s.id === "build" ? { ...s, binding: { capability: "build", inputs: {} } } : s)),
    });
    expect(q(el, "wf-input-pr_url")!.hasAttribute("data-unwired")).toBe(true);
    await click(q(el, "wf-autowire"));
    expect(box.actions.map((a) => a.type)).toEqual(["WIRE"]);
    expect(box.actions[0]).toMatchObject({ type: "WIRE", ids: ["deploy"] });
    expect((step("deploy").binding as { inputs: unknown }).inputs).toEqual({ pr_url: { from_step: "build", field: "pr_url" } });
    expect(box.state!.wired).toEqual({ deploy: ["pr_url"] });
    expect(q(el, "wf-input-pr_url")!.textContent).toContain("auto");

    // The mark is the workflow's, not the drawer's: it is still there after the drawer closes.
    await act(async () => box.setOpen(false));
    await act(async () => box.setOpen(true));
    expect(q(document.body, "wf-input-pr_url")!.textContent).toContain("auto");

    box.actions = [];
    await set(q<HTMLSelectElement>(el, "wf-input-source-pr_url")!, "fixed");
    await settle();
    expect(box.actions).toEqual([{ type: "PATCH_STEP", id: "deploy", patch: { input: { name: "pr_url", source: { value: "" } } } }]);
    expect(q(document.body, "wf-input-pr_url")!.textContent).not.toContain("auto");
  });

  it("an auto mark the CANVAS made (a step placed and wired where it landed) shows here too", async () => {
    const el = await mount("deploy", {}, SPEC, { wired: { deploy: ["pr_url"] } });
    expect(q(el, "wf-input-pr_url")!.textContent).toContain("auto");
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

  it("a step edit made while the rename is in flight stays unsaved, not marked saved or lost", async () => {
    let finish: () => void = () => {};
    vi.mocked(types.renameWorkflow).mockImplementationOnce(() => new Promise<void>((r) => { finish = r; }));
    const el = await mount(null);
    const name = q<HTMLInputElement>(el, "wf-settings-name")!;
    await set(name, "Better flow");
    await act(async () => { name.dispatchEvent(new FocusEvent("focusout", { bubbles: true })); });
    await act(async () => box.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Compile" } }));
    await act(async () => { finish(); });
    await settle();
    expect(step("build").title).toBe("Compile");
    expect(box.state!.loaded!.steps.find((s) => s.id === "build")!.title).toBeUndefined();
    expect(box.state!.loadedMeta.name).toBe("Better flow");
    expect(types.isDirty(box.state!)).toBe(true);
  });
});

describe("the title", () => {
  it("takes a space: typed key by key against the real store, \"Build it\" is stored as typed", async () => {
    const el = await mount("build");
    const input = q<HTMLInputElement>(el, "wf-step-title")!;
    // Each key is appended to what the box shows NOW, as a keyboard does.
    for (const key of "Build it") await set(input, input.value + key);
    expect(input.value).toBe("Build it");
    expect(step("build").title).toBe("Build it");
  });

  it("follows the step when it changes underneath (undo), and drops stray spaces when left", async () => {
    const el = await mount("build");
    const input = q<HTMLInputElement>(el, "wf-step-title")!;
    await set(input, "Ship ");
    expect(input.value).toBe("Ship ");
    await act(async () => { input.dispatchEvent(new FocusEvent("focusout", { bubbles: true })); });
    expect(input.value).toBe("Ship");
    // Undo through the store, as Ctrl+Z on the canvas would: the box shows the step again.
    await act(async () => box.dispatch({ type: "UNDO" }));
    expect(step("build").title).toBeUndefined();
    expect(input.value).toBe("");
  });
});

describe("problems", () => {
  it("a problem on a field this step does not draw goes to the top, never nowhere", async () => {
    // Scope on a built-in step (the engine refuses it there), approval on a gate, a captain on
    // a Wait: none of those has a control in the drawer.
    let el = await mount("build", { problems: [
      { step_id: "build", field: "skills", message: "Scope only on agent tasks" },
      { step_id: "build", field: "approval", message: "Approval needs a person" },
    ] });
    let top = q(el, "wf-step-problems")!;
    expect(top.textContent).toContain("Scope only on agent tasks");
    expect(top.textContent).not.toContain("Approval needs a person");
    expect(q(el, "wf-sec-approval")!.textContent).toContain("Approval needs a person");

    act(() => root?.unmount()); host?.remove();
    el = await mount("gate", { problems: [
      { step_id: "gate", field: "approval", message: "No approval on a gate" },
      { step_id: "gate", field: "mcp", message: "No MCP on a gate" },
    ] });
    top = q(el, "wf-step-problems")!;
    expect(top.textContent).toContain("No approval on a gate");
    expect(top.textContent).toContain("No MCP on a gate");

    act(() => root?.unmount()); host?.remove();
    el = await mount("pause", { problems: [{ step_id: "pause", field: "captain", message: "A Wait has no agent" }] });
    expect(q(el, "wf-step-problems")!.textContent).toContain("A Wait has no agent");
  });

  it("a problem on an optional input opens that input instead of hiding behind `n optional`", async () => {
    const el = await mount("research", { problems: [{ step_id: "research", field: "depth", message: "Depth must be 1-5" }] });
    expect(q(el, "wf-input-depth")).not.toBeNull();
    expect(el.textContent).toContain("Depth must be 1-5");
    expect(q(el, "wf-step-problems")).toBeNull();
  });
});

describe("when the drawer cannot edit", () => {
  const lockedControls = (el: HTMLElement) => ({
    approval: q<HTMLButtonElement>(el, "wf-approval-switch")!,
    title: q<HTMLInputElement>(el, "wf-step-title")!,
    close: q<HTMLButtonElement>(el, "wf-inspector-close")!,
    tips: [...el.querySelectorAll<HTMLButtonElement>("button")].filter((b) => b.closest(".wf-sec-head, .wf-insp-id, .wf-field-inline") && !b.closest("fieldset")),
  });
  // A control inside a disabled <fieldset> is disabled (HTML); happy-dom does not compute that.
  const disabled = (el: Element) => (el as HTMLButtonElement).disabled || el.closest("fieldset[disabled]") !== null;

  it("broken JSON locks every editing control, says why, and leaves × and the (i)s working", async () => {
    const el = await mount("research", { jsonText: "{", jsonError: { line: 14, message: "Unexpected token }" } });
    const c = lockedControls(el);
    expect(disabled(c.approval)).toBe(true);
    expect(disabled(c.title)).toBe(true);
    expect(c.approval.closest("fieldset")!.title).toBe("Fix the JSON first (line 14)");
    expect(disabled(c.close)).toBe(false);
    expect(c.tips.length).toBeGreaterThan(0);
    expect(c.tips.every((b) => !disabled(b))).toBe(true);
    await click(c.close);
    expect(box.state!.selection).toBeNull();
  });

  it("an old version is read-only the same way", async () => {
    const el = await mount("research", {}, SPEC, { meta: { ...makeState(SPEC).meta, readOnlyVersion: 2 } });
    const c = lockedControls(el);
    expect(disabled(c.approval)).toBe(true);
    expect(c.approval.closest("fieldset")!.title).toBe("Viewing v2");
    expect(disabled(c.close)).toBe(false);
    expect(c.tips.every((b) => !disabled(b))).toBe(true);
  });

  it("with valid JSON nothing is locked", async () => {
    const el = await mount("research");
    expect(disabled(q(el, "wf-approval-switch")!)).toBe(false);
    expect(el.querySelector("fieldset[disabled]")).toBeNull();
  });
});
