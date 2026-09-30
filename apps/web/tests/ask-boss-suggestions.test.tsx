import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Ask BOSS suggests things about THIS app.
 *
 * The dock offered four fixed prompts — "What changed today?", "Which runs failed and why?" —
 * which are the same four whatever you have open, so they never referred to the work in front
 * of you. Everything needed to do better was already recorded on the run.
 */
const UI = codeOnly("app/code-tasks-ui.tsx");
const DOCK = codeOnly("app/agent-ui.tsx");

describe("app-specific suggestions", () => {
  it("derives them from what BOSS recorded, not from a fixed list", () => {
    const fn = UI.slice(UI.indexOf("const suggestionsFor"));
    // Each of these is a fact the pipeline writes about a specific build.
    expect(fn).toContain("deferred_findings");
    expect(fn).toContain("advisory_failing_gates");
    expect(fn).toContain("mutation_caught");
    expect(fn).toContain("acceptance_criteria");
    expect(fn).toContain("adversarial_verdict");
  });

  it("only offers a suggestion when the fact behind it exists", () => {
    // A prompt about deferred findings on a build with none would be worse than no prompt:
    // it invents a gap and sends the operator looking for it.
    const fn = UI.slice(UI.indexOf("const suggestionsFor"), UI.indexOf("const askBossAbout"));
    expect(fn).toMatch(/if \(Array\.isArray\(deferred\) && deferred\.length\)/);
    expect(fn).toMatch(/if \(st\.mutation_caught === false\)/);
    expect(fn).toMatch(/if \(criteria\.length\)/);
  });

  it("names the app rather than talking in the abstract", () => {
    const fn = UI.slice(UI.indexOf("const suggestionsFor"));
    expect(fn).toContain("appIdentity(m).name");
    expect(fn).toContain("what should we build next");
  });

  it("hands them to the dock with the facts", () => {
    expect(UI).toContain("boss.agent.suggests.");
    expect(DOCK).toContain("boss.agent.suggests.");
  });

  it("falls back to general prompts when no run is in context", () => {
    // Opened from nowhere there is nothing to ground them in, and the general set is the
    // honest answer rather than a guess about an app that is not open.
    const dock = DOCK.slice(DOCK.indexOf("const [suggestions"));
    expect(dock).toContain("What changed today?");
    expect(dock).toContain("if (!initialTaskId) return;");
  });

  it("ignores a malformed handoff rather than rendering junk", () => {
    const dock = DOCK.slice(DOCK.indexOf("const [suggestions"));
    expect(dock).toContain('parsed.every((x) => typeof x === "string")');
  });
});
