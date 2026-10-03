/**
 * Opening a workflow in the editor and pressing Save must not change anything you did not
 * change.
 *
 * THE INCIDENT. The builder rebuilt the spec from its canvas on every save, keeping only the
 * fields it drew. One save of any bundled example — say, to put a different agent on one step,
 * which is the whole of R4 — dropped:
 *   - `input_schema`, so the start form had no fields and Start fell back to a random sample,
 *     and the trend/video/newsletter examples failed mid-run on a missing `from_input`;
 *   - the video example's agent-step `produces: ["title", "description"]`, so its publish step
 *     failed at the very end of the run, after the human approval;
 *   - a typed decision's binding, after which the workflow could not be saved at all;
 *   - `max_attempts`, `on_failure` and any binding key the canvas has no control for.
 * And it rewrote every `next` from drawing order, so a parallel review saved as a straight line.
 *
 * THE SPEC IS NOW THE STATE (lib/workflow-editor-store.ts): an edit touches the keys it names
 * and nothing else. These load real specs — every standard BOSS ships, the video example, and
 * branched flows — edit them through the store exactly as the canvas and inspector do, and
 * compare what Save would send.
 */
import { describe, expect, it } from "vitest";

import {
  editorReducer, emptyEditorState, isDirty, newMeta, specText,
} from "../lib/workflow-editor-store";
import type { EditorAction, EditorState, Spec, SpecStep, StepPatch } from "../lib/workflow-editor-types";
import { insertStep, isLinear, linearOrder } from "../lib/workflow-graph";
import {
  draftFromSpecStep, inputsReadBy, mergeInputSchema, specFromDraft, type DraftStep,
} from "../lib/workflow-spec";
import { BRANCHES, PALETTE, PARALLEL, catalogueSpecs, clone, generatedBranched } from "./_helpers/workflow-fixtures";

type Obj = Record<string, unknown>;

/** The video example, as examples/video-workflow/setup.py publishes it (trimmed). */
const VIDEO: Spec = {
  entry_step: "fetch",
  input_schema: {
    required: ["video_url", "instructions"],
    properties: {
      video_url: { title: "Video link", description: "A Drive or YouTube link." },
      instructions: { title: "What to do", format: "textarea" },
    },
  },
  output_schema: { kind: "video" },
  steps: [
    { id: "fetch", kind: "capability", captain: "video_editor", max_attempts: 5,
      on_failure: { retry: "never" },
      binding: { custom_step: "fetch_link", inputs: { url: { from_input: "video_url" } } },
      approval: { mode: "auto", on_timeout: "block" }, next: ["edit"] },
    { id: "edit", kind: "capability",
      binding: { agent_work: true, produces: ["title", "description"], agents: ["video_editor"],
                 max_steps: 30, mcp_servers: ["drive"],
                 inputs: { media_artifact_id: { from_step: "fetch", field: "media_artifact_id" },
                           instructions: { from_input: "instructions" } } },
      approval: { mode: "auto", on_timeout: "block" }, next: ["judge"] },
    { id: "judge", kind: "capability",
      binding: { typed_decision: { question: "Is it ready?", options: ["yes", "no"] },
                 inputs: { state: { from_step: "edit", field: "summary" } } },
      approval: { mode: "auto", on_timeout: "block" }, next: ["gate"] },
    { id: "gate", kind: "decision", captain: "human", question: "Publish this?",
      guidance: "Watch it end to end.", approver_role: "human", next: ["publish"] },
    { id: "publish", kind: "capability", captain: "media_publisher",
      binding: { custom_step: "publish_youtube", vendor_hint: "youtube",
                 inputs: { media_artifact_id: { from_step: "edit", field: "media_artifact_id" },
                           title: { from_step: "edit", field: "title" } } },
      approval: { mode: "manual", on_timeout: "block", sla_hours: 24 }, next: [] },
  ],
};

const meta = newMeta({ definition_id: "wfd", workflow_key: "wf", name: "WF", version: 1 });
const load = (spec: Spec): EditorState =>
  editorReducer(emptyEditorState(), { type: "LOAD", spec: clone(spec), meta });
const apply = (state: EditorState, ...actions: EditorAction[]) =>
  actions.reduce(editorReducer, state);
const step = (spec: Spec, id: string) => spec.steps.find((s) => s.id === id) as SpecStep;

/** What the inspector sends when a step is opened and every control is left as it was. */
function untouched(s: SpecStep): StepPatch {
  const binding = (s.binding ?? {}) as Obj;
  const approval = (s.approval ?? {}) as Obj;
  const patch: StepPatch = {
    title: typeof s.title === "string" ? s.title : "",
    captain: typeof s.captain === "string" ? s.captain : null,
    approval: approval.mode === "manual" ? "manual" : "auto",
  };
  if (typeof s.max_attempts === "number") patch.max_attempts = s.max_attempts;
  if (binding.agent_work) {
    patch.skills = Array.isArray(binding.skills) ? binding.skills as string[] : [];
    patch.mcp = (binding.mcp ?? binding.mcp_servers ?? []) as string[];
  }
  return patch;
}

const FLOWS: [string, Spec][] = [
  ...catalogueSpecs(),
  generatedBranched(),
  ["video example", VIDEO],
  ["parallel and join", PARALLEL],
  ["if/else and a loop", BRANCHES],
];

describe("every shipped standard is in the fixture", () => {
  it("has all twelve, each a real spec", () => {
    const standards = catalogueSpecs();
    expect(standards.length).toBeGreaterThanOrEqual(12);
    for (const [, spec] of standards) expect(spec.steps.length).toBeGreaterThan(0);
  });
});

describe.each(FLOWS)("%s", (_name, original) => {
  it("opening every step and changing nothing saves exactly what was loaded", () => {
    let state = load(original);
    for (const s of original.steps) {
      state = apply(state, { type: "SELECT", id: s.id },
                    { type: "PATCH_STEP", id: s.id, patch: {} },
                    { type: "PATCH_STEP", id: s.id, patch: untouched(s) });
    }
    expect(state.spec).toEqual(original);
    expect(isDirty(state)).toBe(false);
    expect(state.past).toEqual([]);
    // And through the JSON view: the text it shows parses back to the same spec.
    const viaJson = apply(state, { type: "SET_JSON_TEXT", text: specText(state.spec) });
    expect(viaJson.spec).toEqual(original);
  });

  it("editing one property changes that property and nothing else", () => {
    const target = original.steps[original.steps.length - 1];
    const state = apply(load(original), { type: "PATCH_STEP", id: target.id,
                                          patch: { title: "Renamed by the operator" } });
    expect(state.spec).toEqual({
      ...original,
      steps: original.steps.map((s) => (s.id === target.id
        ? { ...s, title: "Renamed by the operator" } : s)),
    });
  });

  it("inserting in a straight stretch rewires only its two neighbours", () => {
    const order = linearOrder(original);
    // The first gap the graph accepts: in every flow here that is a straight stretch.
    const probe = { id: "inserted", kind: "timer", wait_seconds: 60, next: [] } as SpecStep;
    const gaps = [...order.keys(), order.length].filter((i) => i > 0);
    const at = gaps.find((i) => insertStep(original, i, probe) !== original) ?? -1;
    expect(at).toBeGreaterThan(0);
    const state = apply(load(original), { type: "INSERT", at, step: probe, palette: PALETTE });
    const before = order[at - 1];
    const after = order[at];
    expect(step(state.spec, before).next).toEqual(["inserted"]);
    expect(step(state.spec, "inserted").next).toEqual(after ? [after] : []);
    const { steps: _a, ...rest } = state.spec;
    const { steps: _b, ...originalRest } = original;
    expect(rest).toEqual(originalRest);
    for (const s of original.steps) {
      if (s.id === before) expect({ ...step(state.spec, s.id), next: s.next }).toEqual(s);
      else expect(step(state.spec, s.id)).toEqual(s);
    }
    expect(isLinear(state.spec)).toBe(isLinear(original));
  });
});

describe("changing one step's agent changes only that", () => {
  const saved = apply(load(VIDEO),
                      { type: "PATCH_STEP", id: "publish", patch: { captain: "brand_publisher" } })
    .spec;

  it("keeps the start form's inputs, so Start does not fall back to a sample", () => {
    expect(saved.input_schema).toEqual(VIDEO.input_schema);
  });

  it("keeps what the agent step was asked to produce", () => {
    const edit = step(saved, "edit").binding as Obj;
    expect(edit.produces).toEqual(["title", "description"]);
    expect(edit.agents).toEqual(["video_editor"]);
    expect(edit.max_steps).toBe(30);
  });

  it("keeps a typed decision's binding, so the workflow can still be saved", () => {
    expect((step(saved, "judge").binding as Obj).typed_decision)
      .toEqual({ question: "Is it ready?", options: ["yes", "no"] });
  });

  it("keeps the fields the editor does not draw", () => {
    expect(step(saved, "fetch").max_attempts).toBe(5);
    expect(step(saved, "fetch").on_failure).toEqual({ retry: "never" });
    expect((step(saved, "publish").binding as Obj).vendor_hint).toBe("youtube");
    expect((step(saved, "publish").approval as Obj).sla_hours).toBe(24);
    expect(saved.output_schema).toEqual({ kind: "video" });
  });

  it("and changes the agent", () => {
    expect(step(saved, "publish").captain).toBe("brand_publisher");
  });
});

describe("the editor still owns what it edits", () => {
  it("clearing an agent removes it", () => {
    const saved = apply(load(VIDEO), { type: "PATCH_STEP", id: "publish", patch: { captain: null } });
    expect("captain" in step(saved.spec, "publish")).toBe(false);
  });

  it("an agent step's scope is written to the binding, and removed when cleared", () => {
    const scoped = apply(load(VIDEO), { type: "PATCH_STEP", id: "edit",
                                        patch: { skills: ["video.trim"], mcp: ["drive", "slack"] } });
    const binding = step(scoped.spec, "edit").binding as Obj;
    expect(binding.skills).toEqual(["video.trim"]);
    // One list, in the current spelling.
    expect(binding.mcp).toEqual(["drive", "slack"]);
    expect("mcp_servers" in binding).toBe(false);
    const cleared = apply(scoped, { type: "PATCH_STEP", id: "edit", patch: { skills: [], mcp: [] } });
    const after = step(cleared.spec, "edit").binding as Obj;
    expect("skills" in after || "mcp" in after).toBe(false);
  });

  it("scope is never written to a step the engine would refuse it on", () => {
    const s = load(VIDEO);
    expect(apply(s, { type: "PATCH_STEP", id: "publish", patch: { skills: ["x"] } })).toBe(s);
  });

  it("'Run another workflow' saves its choice where the validator reads it", () => {
    const spec: Spec = { entry_step: "child", steps: [
      { id: "child", kind: "subworkflow", binding: {}, next: [] }] };
    const saved = apply(load(spec), { type: "PATCH_STEP", id: "child",
                                      patch: { config: { workflow_version_id: "wfv_1" } } }).spec;
    expect(saved.steps[0].binding).toEqual({ workflow_version_id: "wfv_1" });
    expect("workflow_version_id" in saved.steps[0]).toBe(false);
  });

  it("a gate's settings stay at the top level, where the runtime reads them", () => {
    const saved = apply(load(VIDEO), { type: "PATCH_STEP", id: "gate",
                                       patch: { config: { question: "Ship it?", guidance: null } } });
    expect(step(saved.spec, "gate")).toMatchObject({ question: "Ship it?" });
    expect("guidance" in step(saved.spec, "gate")).toBe(false);
  });

  it("fixed instructions are pinned as the literal the runner reads", () => {
    const fixed = apply(load(VIDEO), { type: "PATCH_STEP", id: "edit",
                                       patch: { instructions: { mode: "fixed", text: "Trim it" } } });
    expect(((step(fixed.spec, "edit").binding as Obj).inputs as Obj).instructions).toBe("Trim it");
    const ask = apply(fixed, { type: "PATCH_STEP", id: "edit", patch: { instructions: { mode: "ask" } } });
    expect(((step(ask.spec, "edit").binding as Obj).inputs as Obj).instructions)
      .toEqual({ from_input: "instructions" });
  });

  it("a block setting can never rewrite the step's identity or structure", () => {
    const s = load(VIDEO);
    const before = step(s.spec, "gate");
    const after = apply(s, { type: "PATCH_STEP", id: "gate", patch: { config: {
      id: "b", kind: "timer", next: ["zzz"], branches: ["x"], wait_for: ["x"], on_true: "x",
      on_false: "x", body: "x", join: "x", binding: {}, captain: "x", title: "x",
      approval: { mode: "manual" }, max_attempts: 9, question: "Ship it?",
    } } });
    expect(step(after.spec, "gate")).toEqual({ ...before, question: "Ship it?" });
    // An emptied setting is removed, not saved as "".
    const cleared = apply(after, { type: "PATCH_STEP", id: "gate", patch: { config: { guidance: "" } } });
    expect("guidance" in step(cleared.spec, "gate")).toBe(false);
  });

  it("renaming a step keeps every route that reads its output", () => {
    for (const [from, to] of [["probe", "ping"], ["judge", "verdict"]] as const) {
      const flow: Spec = from === "probe" ? BRANCHES : { ...VIDEO, steps: VIDEO.steps.map((st) =>
        st.id === "gate" ? { id: "gate", kind: "condition", on_true: "publish", on_false: "edit",
          expression: { path: "steps.judge.output.answer", op: "eq", value: "yes" }, next: [] }
          : st) };
      const renamed = apply(load(flow), { type: "RENAME_ID", from, to }).spec;
      expect(specText(renamed)).not.toContain(`steps.${from}.`);
      expect(specText(renamed)).toContain(`steps.${to}.output`);
    }
  });

  it("a title is capped at 80 characters, and an empty one is removed", () => {
    const long = apply(load(VIDEO), { type: "PATCH_STEP", id: "fetch", patch: { title: "x".repeat(90) } });
    expect(step(long.spec, "fetch").title).toHaveLength(80);
    const gone = apply(long, { type: "PATCH_STEP", id: "fetch", patch: { title: "  " } });
    expect("title" in step(gone.spec, "fetch")).toBe(false);
  });
});

/**
 * The old builder canvas (BuilderView) still saves through specFromDraft until the Library
 * replaces it. It gets the same guarantees, including the branch one.
 */
describe("the old builder canvas", () => {
  const CAPABILITIES = [{ key: "notify", required_inputs: ["message"] }];
  const CUSTOM = [{ step_key: "publish_youtube", inputs: [{ name: "media_artifact_id" }] }];
  const roundTrip = (spec: Spec, edit?: (draft: DraftStep[]) => DraftStep[]) => {
    let draft = (spec.steps as Obj[]).map(draftFromSpecStep);
    if (edit) draft = edit(draft);
    return specFromDraft(draft, { loaded: spec, capabilities: CAPABILITIES, customSteps: CUSTOM });
  };

  // The canvas writes an agent step's MCP list in the current spelling, so start from it.
  const video = clone(VIDEO);
  const edit = step(video, "edit").binding as Obj;
  edit.mcp = edit.mcp_servers;
  delete edit.mcp_servers;

  it("returns the loaded spec, every field of it, titles included", () => {
    expect(roundTrip(video)).toEqual(video);
    const titled = clone(video);
    titled.steps[0].title = "Fetch the video";
    expect(roundTrip(titled)).toEqual(titled);
  });

  it("keeps a branched flow's own next, branches and wait_for", () => {
    // The graph, field by field. (This canvas also writes a default approval block and offers
    // what the steps read on the start form; the editor above does neither on a no-op.)
    const graph = (spec: Spec) => spec.steps.map((s) => [s.id, s.next, s.branches, s.join,
      s.wait_for, s.on_true, s.on_false, s.body]);
    expect(graph(roundTrip(PARALLEL) as Spec)).toEqual(graph(PARALLEL));
    expect(graph(roundTrip(BRANCHES) as Spec)).toEqual(graph(BRANCHES));
    const edited = roundTrip(PARALLEL, (draft) =>
      draft.map((s) => (s.id === "ship" ? { ...s, captain: "sre" } : s)));
    expect(step(edited as Spec, "fan")).toEqual(step(PARALLEL, "fan"));
    expect(step(edited as Spec, "review_a").next).toEqual(["review_a2"]);
    expect(step(edited as Spec, "merge").wait_for).toEqual(["review_a2", "review_b"]);
    expect(edited.entry_step).toBe("intake");
  });

  it("reordering a straight line rewrites the chain", () => {
    const saved = roundTrip(VIDEO, (draft) => [draft[1], draft[0], ...draft.slice(2)]);
    expect(saved.entry_step).toBe("edit");
    expect(step(saved as Spec, "edit").next).toEqual(["fetch"]);
    expect(step(saved as Spec, "fetch").next).toEqual(["judge"]);
  });

  it("a step replaced by a different one does not inherit the old one's fields", () => {
    const saved = roundTrip(VIDEO, (draft) => draft.map((s) => (s.id === "fetch"
      ? { ...s, customStep: undefined, capability: "notify", inputs: undefined } : s)));
    const fetch = step(saved as Spec, "fetch");
    expect(fetch.binding).toEqual({ capability: "notify",
                                    inputs: { message: { from_input: "message" } } });
    expect(fetch.max_attempts).toBeUndefined();
  });

  it("a workflow drawn from nothing declares the inputs its steps read, offered not demanded", () => {
    const draft: DraftStep[] = [
      { id: "work", kind: "capability", agentWork: true },
      { id: "tell", kind: "capability", capability: "notify" },
    ];
    const saved = specFromDraft(draft, { capabilities: CAPABILITIES, customSteps: [] });
    expect(saved.input_schema).toEqual({ properties: {
      instructions: { type: "string", title: "Instructions", format: "textarea" },
      message: { type: "string", title: "Message" },
    } });
  });
});

describe("the start form", () => {
  it("adds a newly read input to a loaded schema without making it required", () => {
    const merged = mergeInputSchema(VIDEO.input_schema, ["video_url", "channel"]);
    expect((merged.required as string[])).toEqual(["video_url", "instructions"]);
    expect(Object.keys(merged.properties as Obj)).toEqual(
      ["video_url", "instructions", "channel"]);
  });

  it("keeps the older name-to-type shape in its own shape", () => {
    expect(mergeInputSchema({ trend: "string" }, ["trend", "to"]))
      .toEqual({ trend: "string", to: "string" });
  });

  it("reads inputs in the order the steps first use them", () => {
    expect(inputsReadBy(VIDEO.steps as Obj[])).toEqual(["video_url", "instructions"]);
  });

  it("an input wired to the start form in the editor is offered on it", () => {
    const state = apply(load(VIDEO), { type: "PATCH_STEP", id: "publish",
      patch: { input: { name: "title", source: { from_input: "video_title" } } } });
    const schema = state.spec.input_schema as { required: string[]; properties: Obj };
    expect(schema.required).toEqual(["video_url", "instructions"]);
    expect(Object.keys(schema.properties)).toEqual(["video_url", "instructions", "video_title"]);
  });
});
