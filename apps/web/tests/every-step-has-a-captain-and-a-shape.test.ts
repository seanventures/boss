import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "We should have a captain assigned who can answer those questions" and "different icons for
 * each workflow step, so steps are uniquely known".
 *
 * Both were the same absence in different clothes: a step had no identity. Every node looked
 * alike apart from a number, and every step's only way to ask a question reached BOSS in
 * general — no lens, no remit, no memory of what this kind of work has produced before.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ICONS = codeOnly("app/step-icons.tsx");
const CSS = codeOnly("app/globals.css");

describe("every step has somebody who answers for it", () => {
  it("the captain travels with the capability, not in a second copy here", () => {
    // A console-side map would drift from the engine's the first time a capability was added.
    expect(UI).toContain("captain?: string;");
    expect(UI).toContain("capability?.captain");
  });

  it("the ask is addressed, not broadcast", () => {
    // `dock_role` is the parameter the dock already reads to answer as a named member of the
    // workforce, reading that role's own record.
    expect(UI).toContain("role: captain ? roleLabel(captain) : null");
  });

  it("the button says who it reaches", () => {
    expect(UI).toContain("`Ask the ${roleLabel(stepCaptain(step, capability))}`");
    // And still works for a step with no captain at all.
    expect(UI).toContain('"Ask BOSS about this step"');
  });

  it("and the captain is shown, not only implied by a button label", () => {
    expect(UI).toContain("<AgentChip roleKey={stepCaptain(step, capability)}");
  });

  it("one resolver decides who it is, so the override cannot be half-honoured", () => {
    // A workflow may name a captain for a step, because two steps can run the same capability
    // against different accounts — "publish to our channel" and "publish to a client's" are one
    // step kind and two agents. Resolved in one function: workflow's choice, else the agent
    // that actually ran it (`ran_as`, when BOSS picked one), else the capability's default.
    // REWRITTEN for the middle term: a step BOSS gave to an agent showed the default captain,
    // an agent that did not run it. REWRITTEN AGAIN: a built-in step that ran as whoever started
    // the run still fell back to the default captain, so the chip read "engineering_captain —
    // answers for this step" above "Ran as ceo". The rule moved to `stepAgentChip` in
    // step-agents-ui.tsx, where its behaviour is tested (a-step-says-who-ran-it.test.tsx); here
    // only that the run view asks it, for both the agent and the chip's note.
    expect(UI).toContain("function stepCaptain(step: RunStep, capability?: Capability): string {");
    expect(UI).toContain(
      "return stepAgentChip(step.captain, step.output, capability?.captain)?.role ?? \"\";");
    expect(UI).toContain("note={stepAgentChip(step.captain, step.output, capability?.captain)?.note");
  });
});

describe("every step has its own shape", () => {
  it("no two kinds share a glyph", () => {
    const used = [...ICONS.matchAll(/^\s{2}(\w+):\s([A-Z]\w+),/gm)].map((m) => m[2]);
    expect(used.length).toBeGreaterThan(15);
    expect(new Set(used).size).toBe(used.length);
  });

  it("and a kind with no glyph falls back rather than guessing", () => {
    // A shared icon would be worse than none: it asserts a similarity that does not exist.
    expect(ICONS).toContain("export function hasStepIcon");
    expect(UI).toContain('hasStepIcon(step.capability ?? undefined)');
    // (The run's lane is the editor's FlowLane now; its node glyphs, failure marks and the
    // motion of a step in flight are pinned in tests/flow-lane.test.tsx.)
  });

  it("the card wears the same glyph as its rail node", () => {
    expect(UI).toContain('data-testid="dw-detail-glyph"');
  });
});

