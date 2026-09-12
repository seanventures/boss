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
    expect(UI).toContain('if (captain) url.searchParams.set("dock_role", roleLabel(captain));');
    expect(UI).toContain('else url.searchParams.delete("dock_role");');
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
    // step kind and two agents. Resolved in one function: workflow's choice, else the
    // capability's default.
    expect(UI).toContain("function stepCaptain(step: RunStep, capability?: Capability): string {");
    expect(UI).toContain("return named || String(capability?.captain ?? \"\").trim();");
    // And the chip says WHICH of the two it is, so an override is visible as an override.
    expect(UI).toContain('note={step.captain ? "named by this workflow" : "answers for this step"}');
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
    expect(UI).toContain('phase === "done" ? "✓" : index + 1');
  });

  it("a failure keeps its mark, which an icon must never hide", () => {
    const at = UI.indexOf('<span className="dw-dot" data-phase={phase}');
    const block = UI.slice(at, at + 600);
    expect(block.indexOf('phase === "failed" ? "!"')).toBeLessThan(block.indexOf("hasStepIcon"));
  });

  it("the card wears the same glyph as its rail node", () => {
    expect(UI).toContain('data-testid="dw-detail-glyph"');
  });
});

describe("a step that is working looks like it", () => {
  it("the dot breathes and the node sweeps", () => {
    expect(CSS).toContain('.dw-node[data-phase="now"] .dw-dot {');
    expect(CSS).toContain("animation: dw-dot-breathe");
    expect(CSS).toContain("animation: dw-node-sweep");
  });

  it("and neither animation moves the layout", () => {
    // A run takes eighteen minutes; anything that reflows would jitter for all of it.
    const at = CSS.indexOf("@keyframes dw-dot-breathe");
    const block = CSS.slice(at, at + 300);
    expect(block).toContain("box-shadow");
    expect(block).not.toContain("width");
    expect(block).not.toContain("margin");
  });

  it("motion off still leaves a visible busy signal", () => {
    const reduced = CSS.split("@media (prefers-reduced-motion: reduce)").slice(1).join("\n");
    expect(reduced).toContain('.dw-node[data-phase="now"] .dw-node-btn');
    expect(reduced).toContain("animation: none");
    // Still marked, because a busy signal that vanishes says nothing is happening.
    expect(reduced).toContain("--live");
  });
});
