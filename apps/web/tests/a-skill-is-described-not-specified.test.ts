import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Skills are described, not specified: the way in is a conversation, not a 13-field form.
 *
 * REWRITTEN for the Kit (skills-mcp spec §5.5): the conversation is the Library's `Describe`
 * button, beside `+ New skill`, and it opens the dock to the Program Manager with the same
 * prompt as before. The form survives as the skill drawer, progressive rather than folded.
 * (The render test is tests/library.test.tsx.)
 */
const LIBRARY = codeOnly("app/skills/library.tsx");

describe("skills are authored by conversation", () => {
  it("Describe sits beside + New skill, with its tooltip", () => {
    const at = LIBRARY.indexOf('data-testid="kit-lib-describe"');
    expect(at).toBeGreaterThan(0);
    expect(LIBRARY.slice(at - 120, at)).toContain("data-tip={TIP.describe}");
    expect(LIBRARY.indexOf('data-testid="kit-lib-new-skill"')).toBeGreaterThan(at);
  });

  it("the door is addressed to the role whose remit this is", () => {
    expect(LIBRARY).toContain('role: "Program Manager"');
    expect(LIBRARY).toContain("I want to create a skill for this project.");
  });

  it("the form survives as the skill drawer, its first fields the three that matter", () => {
    const DRAWER = codeOnly("app/skills/skill-drawer.tsx");
    const order = ["skill-label", "skill-summary", "skill-behaviour"].map((id) => DRAWER.indexOf(`data-testid="${id}"`));
    expect(order.every((i) => i > 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });
});
