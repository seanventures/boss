import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * A card that says SHIPPED and Building… at the same time.
 *
 * Both facts were true — one attempt had shipped, a follow-up was still building — but the
 * card applied two OPPOSITE precedence rules to them. The badge documents "shipped beats
 * running beats failed"; `AppPrimaryAction` tested `running` first. So a working app with
 * three attempts rendered a SHIPPED badge above a disabled "Building…" button, and the only
 * useful control on the card — open the app that already exists — was switched off because a
 * LATER attempt was in flight.
 */
const SRC = codeOnly("app/code-tasks-ui.tsx");

describe("the primary action", () => {
  it("only says Building… when there is genuinely nothing to show", () => {
    expect(SRC).toContain("if (running && !shipped) {");
  });

  it("does not disable the action on an app that has already shipped", () => {
    // The bare `if (running)` was the bug: it short-circuited before the shipped branch.
    const action = SRC.slice(SRC.indexOf("function AppPrimaryAction"));
    expect(action).not.toMatch(/\n\s{2}if \(running\) \{/);
  });

  it("still offers the preview branches ahead of the building branch", () => {
    // A live preview is the most useful thing on the card and must keep winning outright.
    const action = SRC.slice(SRC.indexOf("function AppPrimaryAction"));
    expect(action.indexOf("previewStarting")).toBeLessThan(
      action.indexOf("if (running && !shipped)"));
  });
});

describe("the status badge", () => {
  it("reports both states when both are true", () => {
    expect(SRC).toContain('anyShipped && anyRunning ? "shipped · building"');
  });

  it("keeps the single-state readings unchanged", () => {
    const badge = SRC.slice(SRC.indexOf('anyShipped && anyRunning'),
                            SRC.indexOf('anyShipped && anyRunning') + 260);
    expect(badge).toContain('anyShipped ? "shipped"');
    expect(badge).toContain('anyRunning ? "building"');
  });

  it("agrees with the action about which state wins", () => {
    // The defect was two different precedence orders in one card. Shipped wins in both now.
    const badge = SRC.slice(SRC.indexOf('anyShipped && anyRunning'),
                            SRC.indexOf('anyShipped && anyRunning') + 260);
    expect(badge.indexOf('"shipped"')).toBeLessThan(badge.indexOf('"building"'));
    const action = SRC.slice(SRC.indexOf("function AppPrimaryAction"));
    expect(action).toContain("running && !shipped");
  });
});
