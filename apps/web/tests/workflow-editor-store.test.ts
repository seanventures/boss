/**
 * The editor's state: every edit is one action on the spec, and the spec is the only state.
 *
 * What an operator relies on without knowing it: undo puts back exactly what was there; a
 * half-typed JSON document never reaches the workflow; an edit that changes nothing does not
 * light Save; a placed step is wired where it lands; the JSON view can find each step's line.
 */
import { describe, expect, it } from "vitest";

import {
  editorReducer, emptyEditorState, isDirty, lineOfStep, newMeta, parseSpecText, specText,
  stepAtLine,
} from "../lib/workflow-editor-store";
import type { EditorAction, EditorState, Spec } from "../lib/workflow-editor-types";
import { newStepFor } from "../lib/step-view";
import { linearOrder } from "../lib/workflow-graph";
import { LINEAR, PALETTE, PARALLEL, clone } from "./_helpers/workflow-fixtures";

const meta = newMeta({ definition_id: "wfd_1", workflow_key: "ship", name: "Ship", version: 3 });
const loaded = (spec: Spec = LINEAR): EditorState =>
  editorReducer(emptyEditorState(), { type: "LOAD", spec: clone(spec), meta });
const run = (state: EditorState, ...actions: EditorAction[]) =>
  actions.reduce(editorReducer, state);
const step = (s: EditorState, id: string) => s.spec.steps.find((x) => x.id === id)!;

describe("loading", () => {
  it("is clean, with nothing to undo", () => {
    const s = loaded();
    expect(s.spec).toEqual(LINEAR);
    expect(isDirty(s)).toBe(false);
    expect(s.past).toEqual([]);
  });

  it("a workflow never saved has nothing loaded, so its first step is something to save", () => {
    const s = editorReducer(emptyEditorState(), { type: "LOAD", spec: { steps: [] }, meta: newMeta() });
    expect(s.loaded).toBeNull();
    expect(isDirty(s)).toBe(false);
    const added = editorReducer(s, { type: "INSERT", at: -1,
                                     step: newStepFor({ agentTask: true }, s.spec, PALETTE) });
    expect(isDirty(added)).toBe(true);
  });
});

describe("undo and redo", () => {
  it("return the exact prior spec, step by step", () => {
    const s0 = loaded();
    const s1 = run(s0, { type: "PATCH_STEP", id: "build", patch: { captain: "qa" } });
    const s2 = run(s1, { type: "MOVE", id: "deploy", to: 0 });
    const s3 = run(s2, { type: "REMOVE", id: "research" });
    const back = run(s3, { type: "UNDO" }, { type: "UNDO" }, { type: "UNDO" });
    expect(back.spec).toEqual(s0.spec);
    expect(back.spec).toBe(s0.spec);
    expect(isDirty(back)).toBe(false);
    const forward = run(back, { type: "REDO" }, { type: "REDO" });
    expect(forward.spec).toBe(s2.spec);
    expect(run(forward, { type: "REDO" }).spec).toBe(s3.spec);
  });

  it("a new edit drops the redo stack", () => {
    const s = run(loaded(), { type: "PATCH_STEP", id: "build", patch: { title: "Make" } },
                  { type: "UNDO" }, { type: "PATCH_STEP", id: "build", patch: { title: "Other" } });
    expect(s.future).toEqual([]);
  });

  it("keeps the last 100 states", () => {
    let s = loaded();
    for (let n = 0; n < 130; n += 1) {
      s = editorReducer(s, { type: "PATCH_STEP", id: "build", patch: { title: `T${n}` } });
    }
    expect(s.past).toHaveLength(100);
    expect(step(s, "build").title).toBe("T129");
    for (let n = 0; n < 100; n += 1) s = editorReducer(s, { type: "UNDO" });
    expect(step(s, "build").title).toBe("T29");
  });

  it("drops the selection when undo takes its step away", () => {
    const s = run(loaded(), { type: "INSERT", at: 1, step: newStepFor({ capability: "notify" },
                                                                         LINEAR, PALETTE) });
    expect(s.selection).toBe("notify");
    expect(run(s, { type: "UNDO" }).selection).toBeNull();
  });
});

describe("an edit that changes nothing", () => {
  it("returns the same state: no undo entry, Save stays off", () => {
    const s = loaded();
    for (const patch of [{}, { captain: "engineering_captain" }, { approval: "auto" as const },
                         { title: "" }]) {
      expect(editorReducer(s, { type: "PATCH_STEP", id: "build", patch })).toBe(s);
    }
    expect(editorReducer(s, { type: "MOVE", id: "build", to: 1 })).toBe(s);
    expect(editorReducer(s, { type: "SET_SPEC", spec: clone(LINEAR) })).toBe(s);
  });
});

describe("structural edits", () => {
  it("INSERT with a palette wires the new step where it lands, and remembers it did", () => {
    const smoke = newStepFor({ capability: "smoke_test" }, LINEAR, PALETTE);
    const s = run(loaded(), { type: "INSERT", at: -1, step: smoke, palette: PALETTE });
    expect((step(s, "smoke_test").binding as { inputs: unknown }).inputs)
      .toEqual({ url: { from_step: "deploy", field: "url" } });
    expect(s.wired).toEqual({ smoke_test: ["url"] });
    // Wiring it by hand takes the `auto` mark off.
    const manual = run(s, { type: "PATCH_STEP", id: "smoke_test",
                            patch: { input: { name: "url", source: { value: "https://x" } } } });
    expect(manual.wired).toEqual({ smoke_test: [] });
    expect((step(manual, "smoke_test").binding as { inputs: unknown }).inputs)
      .toEqual({ url: "https://x" });
  });

  it("a placed agent task's instructions are offered on the start form", () => {
    const s = run(loaded(), { type: "INSERT", at: 0,
                              step: newStepFor({ agentTask: true }, LINEAR, PALETTE) });
    expect(Object.keys((s.spec.input_schema as { properties: object }).properties))
      .toEqual(["directive", "instructions"]);
    expect((s.spec.input_schema as { required: string[] }).required).toEqual(["directive"]);
  });

  it("an id that is taken gets a free one rather than a broken spec", () => {
    const s = run(loaded(), { type: "INSERT", at: 0, step: { id: "build", kind: "timer",
                                                             wait_seconds: 5 } });
    expect(linearOrder(s.spec)).toEqual(["build_2", "research", "build", "deploy"]);
  });

  it("DUPLICATE puts a copy right after the original and selects it", () => {
    const s = run(loaded(), { type: "DUPLICATE", id: "research" });
    expect(linearOrder(s.spec)).toEqual(["research", "research_2", "build", "deploy"]);
    expect(s.selection).toBe("research_2");
    expect(step(s, "research_2").binding).toEqual(step(s, "research").binding);
  });

  it("an edit across a branch boundary is refused as a whole", () => {
    const s = loaded(PARALLEL);
    expect(editorReducer(s, { type: "REMOVE", id: "merge" })).toBe(s);
    // Between the two branches: neither belongs to the other.
    expect(editorReducer(s, { type: "MOVE", id: "tell", to: 4 })).toBe(s);
    // Into a linear stretch inside a branch is a plain move, and allowed.
    expect(linearOrder(editorReducer(s, { type: "MOVE", id: "tell", to: 3 }).spec))
      .toEqual(["intake", "fan", "review_a", "tell", "review_a2", "review_b", "merge", "ship"]);
  });

  it("RENAME_ID follows the selection and the problems to the new id", () => {
    const s = run(loaded(), { type: "SELECT", id: "build" },
                  { type: "SET_PROBLEMS", result: { ok: false, summary: null as never,
                    problems: [{ step_id: "build", field: "captain", message: "No such agent" }] } },
                  { type: "RENAME_ID", from: "build", to: "make" });
    expect(s.selection).toBe("make");
    expect(s.problems[0].step_id).toBe("make");
    expect(step(s, "research").next).toEqual(["make"]);
    expect(run(s, { type: "RENAME_ID", from: "make", to: "has space" })).toBe(s);
  });

  it("removing a step drops its problems and its selection", () => {
    const s = run(loaded(), { type: "SELECT", id: "build" },
                  { type: "SET_PROBLEMS", result: { ok: false, summary: null as never,
                    problems: [{ step_id: "build", field: null, message: "x" },
                               { step_id: null, field: null, message: "y" }] } },
                  { type: "REMOVE", id: "build" });
    expect(s.selection).toBeNull();
    expect(s.problems).toEqual([{ step_id: null, field: null, message: "y" }]);
  });

  it("SET_INPUT_SCHEMA edits the start form in the shape given", () => {
    const s = run(loaded(), { type: "SET_INPUT_SCHEMA", schema: { topic: "string" } });
    expect(s.spec.input_schema).toEqual({ topic: "string" });
    const none = loaded({ ...LINEAR, input_schema: undefined } as unknown as Spec);
    expect(editorReducer({ ...none, spec: { steps: none.spec.steps, entry_step: "research" } },
                         { type: "SET_INPUT_SCHEMA", schema: {} }).past).toEqual([]);
  });

  it("SAVED makes the saved spec the new baseline", () => {
    const edited = run(loaded(), { type: "PATCH_STEP", id: "build", patch: { title: "Make" } });
    expect(isDirty(edited)).toBe(true);
    const saved = run(edited, { type: "SAVED", spec: edited.spec, meta: { ...meta, version: 4 } });
    expect(isDirty(saved)).toBe(false);
    expect(saved.loadedMeta.version).toBe(4);
  });

  it("SAVED moves the baseline only: edits made after the request was sent stay, unsaved", () => {
    const sent = run(loaded(), { type: "PATCH_STEP", id: "build", patch: { title: "Make" } });
    const later = run(run(sent, { type: "PATCH_STEP", id: "build", patch: { captain: "dev" } }),
                      { type: "SET_META", patch: { description: "typed meanwhile" } });
    const saved = run(later, { type: "SAVED", spec: sent.spec,
                               meta: { ...sent.meta, name: "Server name", version: 4 }, sent: sent.meta });
    expect(saved.loaded).toEqual(sent.spec);
    expect(saved.spec).toBe(later.spec);
    expect(saved.meta).toMatchObject({ name: "Server name", description: "typed meanwhile", version: 4 });
    expect(saved.loadedMeta.description).toBe(sent.meta.description);
    expect(isDirty(saved)).toBe(true);
  });

  it("a name change alone is something to save", () => {
    expect(isDirty(run(loaded(), { type: "SET_META", patch: { name: "Ship faster" } }))).toBe(true);
  });
});

describe("JSON text", () => {
  it("a valid edit replaces the spec and keeps the text as typed", () => {
    const s = loaded();
    const text = specText(s.spec).replace("\"engineering_captain\"", "\"qa\"");
    const next = editorReducer(s, { type: "SET_JSON_TEXT", text });
    expect(step(next, "build").captain).toBe("qa");
    expect(next.jsonText).toBe(text);
    expect(next.jsonError).toBeNull();
    // A visual edit afterwards re-serialises.
    expect(run(next, { type: "PATCH_STEP", id: "build", patch: { title: "Make" } }).jsonText)
      .toBeNull();
  });

  it("broken text leaves the spec untouched and says which line", () => {
    const s = loaded();
    const lines = specText(s.spec).split("\n");
    const target = lines.findIndex((l) => l.includes("\"id\": \"build\""));
    lines[target] = lines[target].replace(",", "");
    const text = lines.join("\n");
    const next = editorReducer(s, { type: "SET_JSON_TEXT", text });
    expect(next.spec).toBe(s.spec);
    expect(next.jsonError).toEqual({ line: target + 2, column: expect.any(Number),
                                     message: "Unexpected token \"" });
    expect(isDirty(next)).toBe(true);
  });

  it("while it is broken, visual edits are refused so the typing is not lost", () => {
    const broken = editorReducer(loaded(), { type: "SET_JSON_TEXT", text: "{\"steps\": [" });
    expect(broken.jsonError?.message).toBe("Unexpected end of JSON");
    expect(editorReducer(broken, { type: "REMOVE", id: "build" })).toBe(broken);
    expect(editorReducer(broken, { type: "PATCH_STEP", id: "build", patch: { title: "x" } }))
      .toBe(broken);
    // Undo first throws the broken text away, back to the spec it shows.
    const undone = editorReducer(broken, { type: "UNDO" });
    expect(undone.jsonError).toBeNull();
    expect(undone.jsonText).toBeNull();
    expect(undone.spec).toBe(broken.spec);
  });

  it("text that parses but is not a workflow is an error too, at the step at fault", () => {
    expect(parseSpecText("[]")).toMatchObject({ error: { line: 1 } });
    expect(parseSpecText("{\"steps\": {}}")).toMatchObject({ error: { message: "steps must be a list" } });
    const dup = "{\n  \"steps\": [\n    {\"id\": \"a\", \"kind\": \"timer\"},\n"
      + "    {\"id\": \"a\", \"kind\": \"timer\"}\n  ]\n}";
    expect(parseSpecText(dup)).toEqual({ error: { line: 4, column: 6,
                                                  message: "Duplicate step id a" } });
    expect(parseSpecText("{\"steps\": [{\"kind\": \"timer\"}]}"))
      .toMatchObject({ error: { message: "Step 1 needs an id" } });
  });

  it("finds each step's id line, and the step around any line", () => {
    const text = specText(PARALLEL);
    const line = lineOfStep(text, "merge")!;
    expect(text.split("\n")[line - 1].trim()).toBe("\"id\": \"merge\",");
    expect(stepAtLine(text, line)).toBe("merge");
    expect(stepAtLine(text, line + 3)).toBe("merge");
    expect(stepAtLine(text, 1)).toBeNull();
    expect(lineOfStep(text, "nope")).toBeNull();
  });

  it("still finds the steps before a syntax error", () => {
    const text = specText(LINEAR);
    const cut = text.slice(0, text.indexOf("\"id\": \"deploy\""));
    expect(stepAtLine(cut, lineOfStep(cut, "build")! + 1)).toBe("build");
  });
});
