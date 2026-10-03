// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — enforced as a class, by rendering.
 *
 * The operator: "lots of text — make it tooltip / info, no one reads details, keep it concise".
 * So every surface of the Workflow Studio is rendered with fixtures and its VISIBLE text is
 * walked: no run of text longer than six words outside a tooltip, and no <p> inside the editor.
 * Explanations belong in a HelpTip or a `title`, which this walk does not count.
 *
 * One file for the whole studio (spec 3.8 item 6). This part covers the inspector (agent task,
 * built-in, gate, workflow settings) and the JSON view; the lane, palette, top bar and library
 * add their own `describe` blocks here.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorState, Spec } from "../lib/workflow-editor-types";

vi.mock("../lib/workflow-editor-types", async (orig) => {
  const real = await orig<typeof import("../lib/workflow-editor-types")>();
  const f = await import("./_helpers/workflow-editor-fakes");
  return {
    ...real,
    stepView: f.fakeStepView, upstreamOf: f.fakeUpstreamOf, wireInputs: f.fakeWireInputs,
    lineOfStep: f.fakeLineOfStep, stepAtLine: f.fakeStepAtLine, FlowLane: f.FakeFlowLane,
    fetchLibrary: vi.fn(async () => []),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Inspector } = await import("../app/workflows/editor/inspector/inspector");
const { JsonView } = await import("../app/workflows/editor/json/json-view");
const { PALETTE, makeState } = await import("./_helpers/workflow-editor-fakes");

const MAX_WORDS = 6;

/** Visible text runs (an element's own text), outside tooltips, text boxes and the JSON text. */
export function overBudget(root: Element): string[] {
  const out: string[] = [];
  const skip = (el: Element) => el.closest('[data-tip], [role="tooltip"], textarea, script, style, option, datalist');
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (skip(el)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("");
    const words = own.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean);
    if (words.length > MAX_WORDS) out.push(own.trim());
  }
  return out;
}

const paragraphs = (root: Element) => [...root.querySelectorAll(".wf-editor p")].map((p) => p.textContent);

const SPEC: Spec = {
  entry_step: "research",
  input_schema: { properties: { topic: { type: "string" } }, required: ["topic"] },
  steps: [
    { id: "research", kind: "capability", next: ["write"], binding: { capability: "research", inputs: {} } },
    { id: "write", kind: "capability", next: ["deploy"], captain: "qa",
      binding: { agent_work: true, inputs: { instructions: "Write the release notes" }, skills: ["github.create_pr"] } },
    { id: "deploy", kind: "capability", next: ["gate"], binding: { capability: "deploy", inputs: {} } },
    { id: "gate", kind: "decision", next: [], question: "", binding: {} },
  ],
};

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<div className="wf-editor">{node}</div>));
  // Open every disclosure, so collapsed text is counted too.
  await act(async () => {
    host!.querySelectorAll("details").forEach((d) => { d.open = true; });
    host!.querySelectorAll<HTMLButtonElement>('[data-testid="wf-inputs-optional"]').forEach((b) => b.click());
  });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null;
});

const state = (selection: string | null, over: Partial<EditorState> = {}): EditorState =>
  ({ ...makeState(JSON.parse(JSON.stringify(SPEC))), selection, ...over });

describe("the inspector says it in six words or fewer", () => {
  for (const [label, selection] of [["an agent task", "write"], ["a built-in step", "research"],
                                     ["a forced-approval step", "deploy"], ["a gate", "gate"],
                                     ["the workflow settings", null]] as const) {
    it(label, async () => {
      const el = await render(<Inspector state={state(selection, {
        problems: [{ step_id: "gate", field: "question", message: "Question is required" }],
      })} palette={PALETTE} dispatch={() => {}} projectId="p1" actor="ceo" onClose={() => {}} />);
      expect(overBudget(el)).toEqual([]);
      expect(paragraphs(el)).toEqual([]);
    });
  }
});

describe("the JSON view says it in six words or fewer", () => {
  it("valid, with a problem", async () => {
    const el = await render(<JsonView state={state("write", {
      problems: [{ step_id: "research", field: "topic", message: "Missing input topic" }],
    })} palette={PALETTE} dispatch={() => {}} />);
    expect(overBudget(el)).toEqual([]);
    expect(paragraphs(el)).toEqual([]);
  });

  it("broken", async () => {
    const el = await render(<JsonView state={state(null, {
      jsonText: "{ \"steps\": [ }", jsonError: { line: 1, message: "Unexpected token }" },
    })} palette={PALETTE} dispatch={() => {}} />);
    expect(overBudget(el)).toEqual([]);
  });
});

describe("the walk itself", () => {
  it("catches a sentence and ignores one inside a tooltip", () => {
    const div = document.createElement("div");
    div.innerHTML = '<span>This is a sentence that runs far too long</span><span role="tooltip">This one is allowed to be long, it is help</span>';
    expect(overBudget(div)).toEqual(["This is a sentence that runs far too long"]);
  });
});
