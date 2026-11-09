// @vitest-environment happy-dom
/**
 * Save, Save as my copy and Start — the half of the editor that writes.
 *
 * THE INCIDENT THIS GUARDS. Saving a standard used to be "Fork to edit" (one write) then Save
 * (another): a failed or repeated save left orphans and "(copy)" definitions — 54 of them on one
 * install. Now Save runs `/check`, then ONE `publish`. These tests count the calls:
 *  - a standard with no copy: exactly one publish, `definition_id: null` + `from_workflow_key`;
 *  - a standard with a copy: the default updates it (`update_copy_id`), creating nothing new;
 *  - a double press, a failed check, a refused publish: no extra writes;
 *  - after saving a copy, the editor is ON the copy, so the next Save is a new version of it.
 */
import React, { act, useReducer } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  CheckResult, EditorAction, EditorMeta, EditorState, PublishRequest, PublishResult, Spec,
} from "../lib/workflow-editor-types";

vi.mock("../lib/workflow-editor-types", async (orig) => {
  const real = await orig<typeof import("../lib/workflow-editor-types")>();
  return {
    ...real,
    checkSpec: vi.fn(), publishSpec: vi.fn(),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { useSaveFlow } = await import("../app/workflows/editor/save/use-save-flow");
const { useAutoCheck, AUTO_CHECK_MS } = await import("../app/workflows/editor/save/use-auto-check");
const types = await import("../lib/workflow-editor-types");
const { reducer, makeState, q } = await import("./_helpers/workflow-editor-fakes");
const { TOAST_EVENT } = await import("../lib/api-write");

const checkSpec = vi.mocked(types.checkSpec);
const publishSpec = vi.mocked(types.publishSpec);

const SPEC: Spec = {
  entry_step: "build",
  steps: [
    { id: "build", kind: "capability", next: ["gate"], binding: { capability: "build", inputs: {} } },
    { id: "gate", kind: "decision", next: [], question: "Ship it?", binding: {} },
  ],
};
const SUMMARY: CheckResult["summary"] = { steps: 2, agents: [], approvals: [], typical_minutes: null, needs: [] };
const OK: CheckResult = { ok: true, problems: [], summary: SUMMARY };

const STANDARD: Partial<EditorMeta> = {
  definition_id: "def_std", workflow_key: "standard_delivery", name: "Standard Delivery",
  is_catalogue: true, version: 4,
};

let root: Root | null = null;
let host: HTMLElement | null = null;
const box: { state: EditorState | null; dispatch: ((a: EditorAction) => void) | null } = { state: null, dispatch: null };
const navigated: string[] = [];
const started: string[] = [];
const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);

function Harness({ initial, usedFor }: { initial: EditorState; usedFor?: string[] }) {
  const [state, dispatch] = useReducer(reducer, initial);
  box.state = state;
  box.dispatch = dispatch;
  const flow = useSaveFlow({
    state, dispatch, projectId: "p1", actor: "ceo", standardUsedFor: usedFor,
    // The editor's URL change to the copy: here, the store simply carries on as the copy.
    onNavigate: (key) => navigated.push(key),
    onStart: (key) => started.push(key),
  });
  return (
    <>
      <button type="button" ref={flow.saveRef} data-testid="save" disabled={!flow.canSave} title={flow.saveTip}
              onClick={() => void flow.save()}>{flow.label}</button>
      <button type="button" ref={flow.startRef} data-testid="start" onClick={flow.start}>Start</button>
      <button type="button" data-testid="check" onClick={() => void flow.check()}>Check</button>
      <button type="button" data-testid="back" onClick={() => flow.guardLeave(() => navigated.push("library"))}>Back</button>
      <span data-testid="name-error">{flow.nameError}</span>
      {flow.popover}
    </>
  );
}

async function mount(spec: Spec, meta: Partial<EditorMeta> = {}, over: Partial<EditorState> = {}, usedFor?: string[]) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Harness initial={{ ...makeState(JSON.parse(JSON.stringify(spec)), meta), ...over }}
                                              usedFor={usedFor} />));
  return host;
}
const settle = async () => { for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (testId: string) => {
  const el = document.querySelector<HTMLButtonElement>(`[data-testid="${testId}"]`);
  expect(el, testId).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};
const edit = async () => {
  await act(async () => { box.dispatch!({ type: "PATCH_STEP", id: "build", patch: { captain: "dev" } }); });
};
const published = (): PublishRequest[] => publishSpec.mock.calls.map((c) => c[1]);

beforeEach(() => {
  navigated.length = 0; started.length = 0; toasts.length = 0;
  checkSpec.mockReset(); publishSpec.mockReset();
  checkSpec.mockResolvedValue(OK);
  window.addEventListener(TOAST_EVENT, onToast);
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null;
  window.removeEventListener(TOAST_EVENT, onToast);
});

const created = (id: string, key: string, name: string, version = 1): PublishResult =>
  ({ definition: { id, workflow_key: key, name }, version, created: true });

describe("saving a standard", () => {
  it("with no copy: asks the name, then ONE publish that creates the copy and moves the editor onto it", async () => {
    publishSpec.mockResolvedValue(created("def_copy", "standard_delivery_mine", "Standard Delivery (mine)"));
    const el = await mount(SPEC, STANDARD);
    expect(q<HTMLButtonElement>(el, "save")!.textContent).toBe("Save as my copy");
    // Unchanged, and still savable: a copy of a standard IS the change.
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(false);
    await press("save");
    expect(checkSpec).toHaveBeenCalledTimes(1);
    expect(publishSpec).not.toHaveBeenCalled();
    const name = document.querySelector<HTMLInputElement>('[data-testid="wf-copy-name"]')!;
    expect(name.value).toBe("Standard Delivery (mine)");
    // Pressed twice: still one write.
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[data-testid="wf-copy-save"]')!.click();
      document.querySelector<HTMLButtonElement>('[data-testid="wf-copy-save"]')!.click();
    });
    await settle();
    expect(published()).toEqual([{
      definition_id: null, name: "Standard Delivery (mine)", description: "", spec: SPEC,
      from_workflow_key: "standard_delivery", update_copy_id: null,
    }]);
    expect(navigated).toEqual(["standard_delivery_mine"]);
    expect(toasts).toContain("Saved as your copy, v1");
    expect(box.state!.meta).toMatchObject({ definition_id: "def_copy", workflow_key: "standard_delivery_mine",
                                            is_catalogue: false, from_workflow_key: "standard_delivery", version: 1 });

    // The NEXT save is a new version of the copy, never a second copy.
    publishSpec.mockResolvedValue(created("def_copy", "standard_delivery_mine", "Standard Delivery (mine)", 2));
    await edit();
    expect(q<HTMLButtonElement>(el, "save")!.textContent).toBe("Save");
    await press("save");
    expect(published()[1]).toMatchObject({ definition_id: "def_copy", from_workflow_key: "standard_delivery" });
    expect(published()[1].update_copy_id).toBeUndefined();
    expect(toasts).toContain("Saved, v2");
  });

  it("with a copy: the default updates it — no new definition", async () => {
    publishSpec.mockResolvedValue(created("def_copy", "standard_delivery_mine", "Mine", 3));
    await mount(SPEC, { ...STANDARD, copy: { definition_id: "def_copy", workflow_key: "standard_delivery_mine", name: "Mine" } });
    await press("save");
    expect(document.querySelector<HTMLInputElement>('[data-testid="wf-copy-update"]')!.checked).toBe(true);
    expect(document.querySelector('[data-testid="wf-copy-save"]')!.closest("[data-testid=wf-save-copy]")!.textContent)
      .toContain("Update “Mine”");
    await press("wf-copy-save");
    expect(published()).toHaveLength(1);
    expect(published()[0]).toMatchObject({ definition_id: null, update_copy_id: "def_copy", name: "Mine",
                                           from_workflow_key: "standard_delivery" });
    expect(navigated).toEqual(["standard_delivery_mine"]);
  });

  it("a refused publish writes nothing more and lands on the bad step", async () => {
    publishSpec.mockRejectedValue(new types.ProblemsError([{ step_id: "gate", field: "question", message: "Question too long" }]));
    await mount(SPEC, STANDARD);
    await press("save");
    await press("wf-copy-save");
    expect(publishSpec).toHaveBeenCalledTimes(1);
    expect(navigated).toEqual([]);
    expect(box.state!.problems).toEqual([{ step_id: "gate", field: "question", message: "Question too long" }]);
    expect(box.state!.selection).toBe("gate");
    expect(document.querySelector('[data-testid="wf-issues"]')!.textContent).toContain("Question too long");
  });

  it("a mapped standard offers `Use for` on the copy, which is not routed until chosen", async () => {
    publishSpec.mockResolvedValue(created("def_copy", "standard_delivery", "Mine"));
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: true })));
    await mount(SPEC, STANDARD, {}, ["project.new"]);
    await press("save");
    await press("wf-copy-save");
    const chip = document.querySelector('[data-testid="wf-use-for"]')!;
    expect(chip.textContent).toBe("Use for new projects instead of the standard");
    await press("wf-use-for");
    expect(vi.mocked(fetch).mock.calls.map((c) => JSON.parse(String((c[1] as RequestInit).body)))).toEqual([
      { action: "apply", project_id: "p1", workflow_key: "standard_delivery", work_class: "project.new", actor: "ceo" },
    ]);
    vi.unstubAllGlobals();
  });
});

describe("saving your own workflow", () => {
  it("checks, then publishes the next version", async () => {
    publishSpec.mockResolvedValue(created("def_own", "my_flow", "My flow", 4));
    const el = await mount(SPEC);
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(true);
    expect(q<HTMLButtonElement>(el, "save")!.title).toBe("No changes");
    await edit();
    expect(q<HTMLButtonElement>(el, "save")!.title).toBe("Publish as the next version (Ctrl+S)");
    await press("save");
    expect(published()).toEqual([expect.objectContaining({ definition_id: "def_own", name: "My flow" })]);
    expect(toasts).toContain("Saved, v4");
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(true);
  });

  it("an edit made while the publish is in flight is kept, unsaved — the save does not swallow it", async () => {
    let finish: (r: PublishResult) => void = () => {};
    publishSpec.mockImplementation(() => new Promise((r) => { finish = r; }));
    const el = await mount(SPEC);
    await edit();
    await press("save");
    expect(publishSpec).toHaveBeenCalledTimes(1);
    const sent = published()[0].spec;
    // The editor stays live during a save: retitle a step and rename the workflow meanwhile.
    await act(async () => {
      box.dispatch!({ type: "PATCH_STEP", id: "gate", patch: { title: "Ship it" } });
      box.dispatch!({ type: "SET_META", patch: { name: "Renamed meanwhile" } });
    });
    await act(async () => { finish(created("def_own", "my_flow", "My flow", 4)); });
    await settle();
    expect(toasts).toContain("Saved, v4");
    // What was published is the new baseline...
    expect(box.state!.loaded).toEqual(sent);
    expect(box.state!.loadedMeta).toMatchObject({ name: "My flow", version: 4 });
    // ...and the later edits are still on screen, waiting for the next Save.
    expect(box.state!.spec.steps.find((s) => s.id === "gate")!.title).toBe("Ship it");
    expect(box.state!.spec.steps.find((s) => s.id === "build")!.captain).toBe("dev");
    expect(box.state!.meta).toMatchObject({ name: "Renamed meanwhile", version: 4 });
    expect(types.isDirty(box.state!)).toBe(true);
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(false);
  });

  it("with no edit in flight the saved state is clean, and takes the server's name", async () => {
    publishSpec.mockResolvedValue(created("def_own", "my_flow", "My flow", 4));
    await mount(SPEC);
    await edit();
    await press("save");
    expect(types.isDirty(box.state!)).toBe(false);
    expect(box.state!.meta.version).toBe(4);
  });

  it("an unchanged answer says No changes", async () => {
    publishSpec.mockResolvedValue({ unchanged: true });
    await mount(SPEC);
    await edit();
    await press("save");
    expect(toasts).toContain("No changes");
  });

  it("a failed check writes nothing and selects the first bad step", async () => {
    checkSpec.mockResolvedValue({ ok: false, summary: SUMMARY, problems: [
      { step_id: "gate", field: null, message: "Gate has no approver" },
      { step_id: "build", field: "spec", message: "Missing input spec" },
    ] });
    await mount(SPEC);
    await edit();
    await press("save");
    expect(publishSpec).not.toHaveBeenCalled();
    expect(box.state!.selection).toBe("build");
    expect(document.querySelector('[data-testid="wf-issues"]')!.textContent).toContain("2 issues");
  });

  it("a step that cannot RUN yet (Needs Deploy set up) still saves, and the warning names where to fix it", async () => {
    publishSpec.mockResolvedValue(created("def_own", "my_flow", "My flow", 4));
    checkSpec.mockResolvedValue({ ok: true, summary: SUMMARY, problems: [
      { step_id: "build", field: "ability", message: "Needs Deploy set up", severity: "warning",
        fix_label: "Set up where it deploys", fix_href: "/?area=infra&view=fleet&add=target" },
    ] });
    await mount(SPEC);
    await edit();
    await press("save");
    expect(publishSpec).toHaveBeenCalledTimes(1);
  });

  it("a gate with no question is stopped before any request", async () => {
    await mount({ ...SPEC, steps: [SPEC.steps[0], { ...SPEC.steps[1], question: "" }] });
    await edit();
    await press("save");
    expect(checkSpec).not.toHaveBeenCalled();
    expect(publishSpec).not.toHaveBeenCalled();
    expect(box.state!.problems).toEqual([{ step_id: "gate", field: "question", message: "Question is required" }]);
  });

  it("a name clash says Name in use", async () => {
    publishSpec.mockRejectedValue(new types.NameInUseError("My flow 2"));
    await mount(SPEC);
    await edit();
    await press("save");
    expect(q(host!, "name-error")!.textContent).toBe("Name in use");
  });

  it("a new workflow cannot be saved empty, and saving it moves to its new key", async () => {
    publishSpec.mockResolvedValue(created("def_new", "untitled_workflow", "Untitled workflow"));
    const meta = { definition_id: null, workflow_key: null, name: "Untitled workflow", version: null };
    const el = await mount({ steps: [] }, meta, { loaded: null });
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(true);
    await act(async () => { box.dispatch!({ type: "SET_SPEC", spec: SPEC }); });
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(false);
    await press("save");
    expect(published()).toEqual([expect.objectContaining({ definition_id: null, name: "Untitled workflow" })]);
    expect(navigated).toEqual(["untitled_workflow"]);
    expect(toasts).toContain("Saved, v1");
  });
});

describe("start and leave", () => {
  it("a clean workflow starts at once", async () => {
    await mount(SPEC);
    await press("start");
    expect(started).toEqual(["my_flow"]);
  });

  it("an unsaved one asks Save first?: Start v3 runs the saved version, Save and start saves then starts", async () => {
    publishSpec.mockResolvedValue(created("def_own", "my_flow", "My flow", 4));
    await mount(SPEC);
    await edit();
    await press("start");
    expect(document.querySelector('[data-testid="wf-start-pop"]')!.textContent).toContain("Save first?");
    await press("wf-start-saved");
    expect(started).toEqual(["my_flow"]);
    expect(publishSpec).not.toHaveBeenCalled();
    await press("start");
    await press("wf-save-and-start");
    expect(publishSpec).toHaveBeenCalledTimes(1);
    expect(started).toEqual(["my_flow", "my_flow"]);
  });

  it("leaving with broken JSON asks first, even when the workflow itself is unchanged", async () => {
    // The text being typed is the unsaved change: leaving silently would throw it away.
    await mount(SPEC);
    await act(async () => { box.dispatch!({ type: "SET_JSON_TEXT", text: "{ \"steps\": [" }); });
    expect(box.state!.jsonError).not.toBeNull();
    await press("back");
    expect(navigated).toEqual([]);
    expect(document.querySelector('[data-testid="wf-leave-confirm"]')).not.toBeNull();
  });

  it("leaving with unsaved changes asks first", async () => {
    await mount(SPEC);
    await press("back");
    expect(navigated).toEqual(["library"]);
    await edit();
    await press("back");
    expect(navigated).toEqual(["library"]);
    expect(document.querySelector('[data-testid="wf-leave-confirm"]')!.textContent).toContain("Discard changes?");
    await press("wf-discard");
    expect(navigated).toEqual(["library", "library"]);
  });
});

describe("auto-check", () => {
  function AutoHarness({ initial }: { initial: EditorState }) {
    const [state, dispatch] = useReducer(reducer, initial);
    box.state = state;
    box.dispatch = dispatch;
    useAutoCheck(state, dispatch, "p1");
    return null;
  }

  it("checks once after edits pause, and drops an answer for a spec no longer on screen", async () => {
    vi.useFakeTimers();
    try {
      let resolveFirst: (r: CheckResult) => void = () => {};
      checkSpec.mockImplementationOnce(() => new Promise((r) => { resolveFirst = r; }));
      checkSpec.mockResolvedValue(OK);
      host = document.createElement("div");
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => root!.render(<AutoHarness initial={makeState(JSON.parse(JSON.stringify(SPEC)))} />));
      await act(async () => { vi.advanceTimersByTime(AUTO_CHECK_MS); });
      expect(checkSpec).toHaveBeenCalledTimes(1);
      // Two quick edits: one check, after the pause.
      await act(async () => { box.dispatch!({ type: "PATCH_STEP", id: "build", patch: { captain: "dev" } }); });
      await act(async () => { vi.advanceTimersByTime(AUTO_CHECK_MS / 2); });
      await act(async () => { box.dispatch!({ type: "PATCH_STEP", id: "build", patch: { captain: "qa" } }); });
      await act(async () => { vi.advanceTimersByTime(AUTO_CHECK_MS - 1); });
      expect(checkSpec).toHaveBeenCalledTimes(1);
      // The first (stale) answer arrives late, with a problem: it is ignored.
      await act(async () => { resolveFirst({ ok: false, summary: SUMMARY, problems: [{ step_id: "build", field: null, message: "stale" }] }); });
      expect(box.state!.problems).toEqual([]);
      await act(async () => { vi.advanceTimersByTime(1); });
      expect(checkSpec).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
