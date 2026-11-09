// @vitest-environment happy-dom
/**
 * JSON ↔ Visual: the JSON view is the same workflow as the lane, not a copy of it.
 *
 *  - an edit in the text reaches the lane at once (the node's agent changes as you type);
 *  - broken JSON never touches the workflow, and blocks Visual and Save with the line to fix;
 *  - the caret and the selection follow each other, and an edit made elsewhere re-serialises
 *    the text without losing the caret's step;
 *  - problems point at a line.
 */
import React, { act, useReducer } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorAction, EditorState, Spec } from "../lib/workflow-editor-types";

vi.mock("../lib/workflow-editor-types", async (orig) => {
  const real = await orig<typeof import("../lib/workflow-editor-types")>();
  const f = await import("./_helpers/workflow-editor-fakes");
  return {
    ...real,
    FlowLane: f.FakeFlowLane,
    checkSpec: vi.fn(), publishSpec: vi.fn(),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { JsonView, jsonGate } = await import("../app/workflows/editor/json/json-view");
const { useSaveFlow } = await import("../app/workflows/editor/save/use-save-flow");
const { lineDiff, foldDiff } = await import("../app/workflows/editor/json/line-diff");
const types = await import("../lib/workflow-editor-types");
const { PALETTE, reducer, makeState, setValue, q } = await import("./_helpers/workflow-editor-fakes");

const SPEC: Spec = {
  entry_step: "research",
  steps: [
    { id: "research", kind: "capability", next: ["build"], binding: { capability: "research", inputs: {} } },
    { id: "build", kind: "capability", next: [], captain: "qa", binding: { capability: "build", inputs: {} } },
  ],
};

let root: Root | null = null;
let host: HTMLElement | null = null;
const box: { state: EditorState | null; dispatch: ((a: EditorAction) => void) | null } = { state: null, dispatch: null };

function SaveButton({ state, dispatch }: { state: EditorState; dispatch: (a: EditorAction) => void }) {
  const flow = useSaveFlow({ state, dispatch, projectId: "p1", onNavigate: () => {} });
  return <button type="button" ref={flow.saveRef} data-testid="save" disabled={!flow.canSave} title={flow.saveTip}
                 onClick={() => void flow.save()}>{flow.label}</button>;
}

function Harness({ initial }: { initial: EditorState }) {
  const [state, dispatch] = useReducer(reducer, initial);
  box.state = state;
  box.dispatch = dispatch;
  const gate = jsonGate(state);
  return (
    <>
      <button type="button" data-testid="visual" disabled={gate.blocked} title={gate.tip}>Visual</button>
      <SaveButton state={state} dispatch={dispatch} />
      <JsonView state={state} palette={PALETTE} dispatch={dispatch} />
    </>
  );
}

async function mount(over: Partial<EditorState> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<Harness initial={{ ...makeState(JSON.parse(JSON.stringify(SPEC))), ...over }} />));
  return host;
}
const text = () => q<HTMLTextAreaElement>(host!, "wf-json-text")!;
const type = async (value: string) => { await act(async () => { setValue(text(), value); }); };

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null;
  vi.clearAllMocks();
});

describe("JSON → Visual", () => {
  it("editing a captain in the text updates the node's agent at once", async () => {
    const el = await mount();
    expect(q(el, "node-build")!.textContent).toBe("QA Lead");
    const started = performance.now();
    await type(text().value.replace('"captain": "qa"', '"captain": "dev"'));
    expect(q(el, "node-build")!.textContent).toBe("Developer");
    expect(performance.now() - started).toBeLessThan(300);
    expect(box.state!.spec.steps[1].captain).toBe("dev");
  });

  it("broken JSON leaves the workflow untouched and blocks Visual and Save with the line", async () => {
    const el = await mount();
    const before = JSON.stringify(box.state!.spec);
    // A real edit first, so Save would otherwise be enabled.
    await type(text().value.replace('"captain": "qa"', '"captain": "dev"'));
    const good = JSON.stringify(box.state!.spec);
    expect(q<HTMLButtonElement>(el, "save")!.disabled).toBe(false);

    const broken = text().value.replace('"captain": "dev",', '"captain": "dev"');
    await type(broken);
    expect(JSON.stringify(box.state!.spec)).toBe(good);
    expect(good).not.toBe(before);
    const line = box.state!.jsonError!.line;
    expect(line).toBeGreaterThan(1);
    expect(q(el, "wf-json-error")!.textContent).toMatch(new RegExp(`^Line ${line}: `));
    expect(q(el, "wf-json-badge")!.textContent).toBe("JSON error");
    for (const id of ["visual", "save"]) {
      const b = q<HTMLButtonElement>(el, id)!;
      expect(b.disabled).toBe(true);
      expect(b.title).toBe(`Fix the JSON first (line ${line})`);
    }
    // The text the operator is fixing stays on screen.
    expect(text().value).toBe(broken);
    await act(async () => { q<HTMLButtonElement>(el, "save")!.click(); });
    expect(types.checkSpec).not.toHaveBeenCalled();
    expect(types.publishSpec).not.toHaveBeenCalled();
  });
});

describe("caret and selection", () => {
  it("a caret inside step k selects node k", async () => {
    await mount();
    const at = text().value.indexOf('"capability": "build"');
    await act(async () => {
      text().focus();
      text().setSelectionRange(at, at);
      text().dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowDown", bubbles: true }));
    });
    expect(box.state!.selection).toBe("build");
  });

  it("a node click elsewhere moves the caret to that step", async () => {
    const el = await mount();
    await act(async () => { q<HTMLButtonElement>(el, "node-build")!.click(); });
    expect(box.state!.selection).toBe("build");
    const caretLine = text().value.slice(0, text().selectionStart).split("\n").length;
    expect(caretLine).toBe(types.lineOfStep(text().value, "build"));
  });

  it("an edit made elsewhere re-serialises the text and keeps the caret on the same step", async () => {
    await mount();
    const at = text().value.indexOf('"capability": "build"');
    await act(async () => {
      text().focus();
      text().setSelectionRange(at, at);
      text().dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowDown", bubbles: true }));
    });
    expect(box.state!.selection).toBe("build");
    // An inspector edit to an EARLIER step adds lines above the caret.
    await act(async () => {
      box.dispatch!({ type: "PATCH_STEP", id: "research", patch: { title: "Look around", captain: "qa" } });
    });
    expect(text().value).toContain('"title": "Look around"');
    const caretLine = text().value.slice(0, text().selectionStart).split("\n").length;
    expect(caretLine).toBe(text().value.split("\n").findIndex((l) => l.includes('"capability": "build"')) + 1);
  });
});

describe("the editor", () => {
  it("Tab inserts two spaces and Shift+Tab takes them away", async () => {
    await mount();
    const at = text().value.indexOf('"steps"');
    await act(async () => {
      text().setSelectionRange(at, at);
      text().dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    });
    expect(box.state!.jsonText!.slice(at, at + 9)).toBe('  "steps"');
    await act(async () => {
      text().dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }));
    });
    expect(box.state!.jsonText).toBe(JSON.stringify(SPEC, null, 2));
  });

  it("problems are listed by line and dotted in the gutter", async () => {
    const el = await mount({ problems: [{ step_id: "build", field: "captain", message: "Missing input spec" }] });
    const line = text().value.split("\n").findIndex((l) => l.includes('"captain"')) + 1;
    expect(q(el, "wf-json-problems")!.textContent).toBe(`Line ${line}, build, Missing input spec`);
    const marked = el.querySelector('.wf-json-ln[data-problem]')!;
    expect(marked.textContent).toBe(String(line));
    expect(marked.getAttribute("title")).toBe("Missing input spec");
  });

  it("Format re-indents; Changes diffs against the published version", async () => {
    const el = await mount();
    await type(JSON.stringify({ ...SPEC, steps: [SPEC.steps[0], { ...SPEC.steps[1], captain: "dev" }] }));
    await act(async () => { q<HTMLButtonElement>(el, "wf-json-format")!.click(); });
    expect(text().value).toBe(JSON.stringify({ ...SPEC, steps: [SPEC.steps[0], { ...SPEC.steps[1], captain: "dev" }] }, null, 2));
    await act(async () => { q<HTMLButtonElement>(el, "wf-json-changes")!.click(); });
    const diff = q(el, "wf-json-diff")!;
    const added = [...diff.querySelectorAll('[data-kind="add"]')].map((d) => d.textContent);
    const removed = [...diff.querySelectorAll('[data-kind="del"]')].map((d) => d.textContent);
    expect(added.join()).toContain('"captain": "dev"');
    expect(removed.join()).toContain('"captain": "qa"');
  });
});

describe("lineDiff", () => {
  it("is exact on a small change and folds unchanged runs", () => {
    const a = ["a", "b", "c", "d", "e", "f", "g", "h", "i"].join("\n");
    const b = ["a", "b", "c", "d", "X", "f", "g", "h", "i", "j"].join("\n");
    const d = lineDiff(a, b);
    expect(d.filter((l) => l.kind !== "same").map((l) => `${l.kind}:${l.text}`)).toEqual(["del:e", "add:X", "add:j"]);
    expect(d.filter((l) => l.kind !== "del").map((l) => l.text).join("\n")).toBe(b);
    expect(d.filter((l) => l.kind !== "add").map((l) => l.text).join("\n")).toBe(a);
    const folded = foldDiff(lineDiff(Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n"),
                                     Array.from({ length: 40 }, (_, i) => (i === 20 ? "changed" : `l${i}`)).join("\n")), 2);
    expect(folded[0]).toEqual({ fold: 18 });
    expect(folded[folded.length - 1]).toEqual({ fold: 17 });
  });
});
