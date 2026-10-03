/**
 * Every card says who runs each step, and says it the same way everywhere.
 *
 * Intent kept from a-card-says-who-runs-each-step: a step's agent is the first thing people look
 * for ("did you assign the agents or not?"). The canvas node, the library card and the run lane
 * all read `stepView`, so these cases are the whole of the rule:
 *   1. captain "human" → A person   2. a named agent → its display name   3. a gate → A person
 *   4. agent task / your step → Auto, or "Auto · <name>" when only one agent can do it
 *   5. a built-in step with no captain → You (it runs as whoever starts the workflow)
 */
import { describe, expect, it } from "vitest";

import { newStepFor, stepView } from "../lib/step-view";
import type { SpecStep } from "../lib/workflow-editor-types";
import { insertStep } from "../lib/workflow-graph";
import { LINEAR, PALETTE } from "./_helpers/workflow-fixtures";

const view = (step: Partial<SpecStep>) =>
  stepView({ id: "s", kind: "capability", ...step } as SpecStep, PALETTE);
const agentTask = (binding: Record<string, unknown> = {}, extra: Partial<SpecStep> = {}) =>
  view({ id: "write_script", binding: { agent_work: true,
         inputs: { instructions: { from_input: "instructions" } }, ...binding }, ...extra });

describe("who runs a step", () => {
  it("1. a step given to `human` is A person", () => {
    expect(view({ kind: "decision", captain: "human" }).agent)
      .toMatchObject({ kind: "person", label: "A person" });
  });

  it("2. a named agent is shown by its display name, else its key", () => {
    expect(view({ captain: "qa", binding: { capability: "build" } }).agent)
      .toEqual({ kind: "named", role_key: "qa", label: "QA Lead", tip: "Named by this workflow." });
    expect(view({ captain: "ghost", binding: { capability: "build" } }).agent.label).toBe("ghost");
  });

  it("3. an approval gate with no captain is A person", () => {
    expect(view({ kind: "decision", question: "Ship?" }).agent)
      .toEqual({ kind: "person", label: "A person", tip: "Waits for a person to answer." });
  });

  it("3. a built-in sign-off (a capability that is a gate) with no captain is A person", () => {
    const palette = { ...PALETTE, capabilities: PALETTE.capabilities.map((c) =>
      c.key === "ux_review" ? { ...c, is_gate: true } : c) };
    const signOff = stepView({ id: "ux_signoff", kind: "capability", next: [],
                               binding: { capability: "ux_review", inputs: {} } }, palette);
    expect(signOff.agent).toEqual({ kind: "person", label: "A person",
                                    tip: "Waits for a person to answer." });
    expect(signOff.badges.approval).toBe(true);
    expect(signOff.type).toBe("builtin");
    // The same capability, not a gate, is still the starter's.
    expect(view({ binding: { capability: "ux_review" } }).agent.label).toBe("You");
  });

  it("4. an agent task is Auto, and names the only agent that can do it", () => {
    expect(agentTask().agent).toEqual({ kind: "auto", label: "Auto",
      tip: "BOSS picks an agent holding every allowed skill." });
    expect(agentTask({ skills: ["video.trim"] }).agent).toEqual({
      kind: "auto", role_key: "video_editor", label: "Auto · Video Editor",
      tip: "Only Video Editor holds video.trim." });
    // Nobody holds both: BOSS still picks (and the check will say nobody can), never a name.
    expect(agentTask({ skills: ["video.trim", "test.run"] }).agent.label).toBe("Auto");
  });

  it("4. your own step is Auto, naming the one agent holding its skill or server", () => {
    expect(view({ binding: { custom_step: "publish_youtube" } }).agent).toMatchObject({
      kind: "auto", label: "Auto · QA Lead", tip: "Only QA Lead holds youtube.upload." });
    expect(view({ binding: { custom_step: "post_slack" } }).agent.label).toBe("Auto · Video Editor");
  });

  it("5. a built-in step with no captain is You — never the capability's default captain", () => {
    expect(view({ binding: { capability: "build" } }).agent).toEqual({
      kind: "you", label: "You", tip: "Built-in step: runs as whoever starts the workflow." });
  });

  it("flow steps nobody is assigned to are BOSS's own", () => {
    expect(view({ kind: "timer", wait_seconds: 60 }).agent.label).toBe("Auto");
    expect(view({ kind: "parallel" }).agent.label).toBe("Auto");
    expect(view({ kind: "subworkflow" }).agent.label).toBe("You");
  });
});

describe("what a step is called", () => {
  it("prefers its own title, then the label of what it binds, never the raw id", () => {
    expect(view({ id: "x1", title: "Ship it", binding: { capability: "deploy" } }).title)
      .toBe("Ship it");
    expect(view({ id: "deploy_prod", binding: { capability: "deploy" } }).title).toBe("Deploy");
    expect(view({ binding: { custom_step: "publish_youtube" } }).title).toBe("Publish to YouTube");
    expect(agentTask().title).toBe("Write script");
    expect(view({ id: "approve_release", kind: "decision" }).title).toBe("Approve release");
  });

  it("has the type and icon the lane draws", () => {
    expect(view({ binding: { capability: "deploy" } })).toMatchObject({ type: "builtin", icon: "deploy" });
    expect(agentTask()).toMatchObject({ type: "agent", icon: "agent" });
    expect(view({ kind: "decision" })).toMatchObject({ type: "gate", icon: "gate" });
    expect(view({ kind: "bounded_loop" }).type).toBe("loop");
    expect(view({ binding: { typed_decision: { question: "?" } } }).type).toBe("other");
  });
});

describe("badges", () => {
  it("marks a forced approval as forced, from the capability or from the stored step", () => {
    expect(view({ binding: { capability: "deploy", inputs: { run_id: "r" } } }).badges)
      .toMatchObject({ approval: true, forced: true });
    expect(view({ binding: { capability: "build" }, approval: { mode: "manual" } }).badges)
      .toMatchObject({ approval: true, forced: false });
    expect(view({ binding: { capability: "build" }, approval: { mode: "auto" } }).badges.approval)
      .toBe(false);
  });

  it("counts inputs and names the required ones nobody wired", () => {
    const badges = view({ binding: { capability: "notify", inputs: { message: { from_input: "t" } } } })
      .badges;
    expect(badges.inputs).toBe(2);
    expect(badges.unwired).toEqual(["channel"]);
  });

  it("counts an agent task's scope and names what its agent lacks", () => {
    const badges = agentTask({ skills: ["video.trim", "test.run"], mcp: ["slack"] },
                             { captain: "qa" }).badges;
    expect(badges).toMatchObject({ skills: 2, mcp: 1, lacks: ["video.trim", "slack"] });
    expect(agentTask({ skills: ["test.run"] }, { captain: "qa" }).badges.lacks).toEqual([]);
  });
});

describe("a new step from the palette", () => {
  it("gets a free id and the shape the validator expects", () => {
    expect(newStepFor({ capability: "build" }, LINEAR, PALETTE)).toEqual({
      id: "build_2", kind: "capability", next: [], binding: { capability: "build", inputs: {} } });
    expect(newStepFor({ capability: "deploy" }, LINEAR, PALETTE).approval).toEqual({ mode: "manual" });
    expect(newStepFor({ agentTask: true }, LINEAR, PALETTE).binding).toEqual(
      { agent_work: true, inputs: { instructions: { from_input: "instructions" } } });
    expect(newStepFor({ customStep: "publish_youtube" }, LINEAR, PALETTE)).toMatchObject({
      id: "publish_youtube", binding: { custom_step: "publish_youtube", inputs: {} },
      approval: { mode: "manual" } });
  });

  it("makes flow blocks with their settings where the runtime reads them", () => {
    expect(newStepFor({ block: "decision" }, LINEAR, PALETTE)).toEqual({
      id: "approval", kind: "decision", next: [], question: "", guidance: "",
      approver_role: "human" });
    expect(newStepFor({ block: "timer" }, LINEAR, PALETTE)).toMatchObject(
      { kind: "timer", wait_seconds: 3600 });
    expect(newStepFor({ block: "subworkflow" }, LINEAR, PALETTE)).toMatchObject(
      { kind: "subworkflow", binding: {} });
  });

  it("can be inserted as made", () => {
    const step = newStepFor({ block: "decision" }, LINEAR, PALETTE);
    expect(insertStep(LINEAR, 2, step)).not.toBe(LINEAR);
  });
});
