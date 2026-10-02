/**
 * Opening a workflow in the builder and pressing Save must not change anything you did not
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
 * These tests load real-shaped specs, round-trip them through the canvas, and compare.
 */
import { describe, expect, it } from "vitest";

import {
  draftFromSpecStep, inputsReadBy, mergeInputSchema, specFromDraft, type DraftStep,
} from "../lib/workflow-spec";

type Obj = Record<string, unknown>;

const CAPABILITIES = [{ key: "notify", required_inputs: ["message"] }];
const CUSTOM = [{ step_key: "publish_youtube", inputs: [{ name: "media_artifact_id" }] }];

/** The video example, as examples/video-workflow/setup.py publishes it (trimmed). */
const VIDEO: Obj = {
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
                 max_steps: 30,
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

const roundTrip = (spec: Obj, edit?: (draft: DraftStep[]) => DraftStep[]) => {
  let draft = (spec.steps as Obj[]).map(draftFromSpecStep);
  if (edit) draft = edit(draft);
  return specFromDraft(draft, { loaded: spec, capabilities: CAPABILITIES, customSteps: CUSTOM });
};

const step = (spec: Obj, id: string) =>
  (spec.steps as Obj[]).find((s) => s.id === id) as Obj;

describe("a save with nothing changed changes nothing", () => {
  it("returns the loaded spec, every field of it", () => {
    expect(roundTrip(VIDEO)).toEqual(VIDEO);
  });
});

describe("changing one step's agent changes only that", () => {
  const saved = roundTrip(VIDEO, (draft) =>
    draft.map((s) => (s.id === "publish" ? { ...s, captain: "brand_publisher" } : s)));

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

  it("keeps the fields the canvas does not draw", () => {
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

describe("the canvas still owns what it draws", () => {
  it("clearing an agent removes it, on every kind of step", () => {
    const saved = roundTrip(VIDEO, (draft) => draft.map((s) => ({ ...s, captain: undefined })));
    for (const s of saved.steps as Obj[]) expect("captain" in s).toBe(false);
  });

  it("reordering rewrites the chain", () => {
    const saved = roundTrip(VIDEO, (draft) => [draft[1], draft[0], ...draft.slice(2)]);
    expect(saved.entry_step).toBe("edit");
    expect(step(saved, "edit").next).toEqual(["fetch"]);
    expect(step(saved, "fetch").next).toEqual(["judge"]);
  });

  it("a step replaced by a different one does not inherit the old one's fields", () => {
    const saved = roundTrip(VIDEO, (draft) => draft.map((s) => (s.id === "fetch"
      ? { ...s, customStep: undefined, capability: "notify", inputs: undefined } : s)));
    const fetch = step(saved, "fetch");
    expect(fetch.binding).toEqual({ capability: "notify",
                                    inputs: { message: { from_input: "message" } } });
    expect(fetch.max_attempts).toBeUndefined();
  });

  it("an agent step's scope is written to the binding, and removed when cleared", () => {
    const scoped = roundTrip(VIDEO, (draft) => draft.map((s) => (s.id === "edit"
      ? { ...s, skills: ["video.trim"], mcp: ["drive"] } : s)));
    expect((step(scoped, "edit").binding as Obj).skills).toEqual(["video.trim"]);
    expect((step(scoped, "edit").binding as Obj).mcp).toEqual(["drive"]);
    const cleared = roundTrip(scoped, (draft) => draft.map((s) => (s.id === "edit"
      ? { ...s, skills: [], mcp: [] } : s)));
    expect("skills" in (step(cleared, "edit").binding as Obj)).toBe(false);
    expect("mcp" in (step(cleared, "edit").binding as Obj)).toBe(false);
  });

  it("'Run another workflow' saves its choice where the validator reads it", () => {
    // It was written at the top level, and the validator requires binding.workflow_version_id,
    // so the block could never be saved from the builder.
    const draft: DraftStep[] = [{ id: "child", kind: "subworkflow",
                                  config: { workflow_version_id: "wfv_1" } }];
    const saved = specFromDraft(draft, { capabilities: [], customSteps: [] });
    const child = (saved.steps as Obj[])[0];
    expect(child.binding).toEqual({ workflow_version_id: "wfv_1" });
    expect("workflow_version_id" in child).toBe(false);
  });
});

describe("a workflow drawn from nothing still has a start form", () => {
  it("declares the inputs its steps read, offered rather than demanded", () => {
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
});
