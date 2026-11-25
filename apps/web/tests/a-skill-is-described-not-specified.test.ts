import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Skills are described, not specified: the way in is a conversation, not a 13-field form.
 *
 * REWRITTEN for the one home (docs/design/SKILLS_AND_MCP_HOME.md): the conversation is Add
 * skill's `Describe`, beside `Write one`, and it opens the dock to the Program Manager with the
 * same prompt as before. The form survives as the skill drawer, progressive rather than folded.
 * (The render test is tests/library.test.tsx.)
 */
const LIBRARY = codeOnly("app/skills/library.tsx");
const ADD_SKILL = codeOnly("app/skills/add-skill-sheet.tsx");
const HOME = codeOnly("app/skills/kit-home.tsx");

describe("skills are authored by conversation", () => {
  it("Describe sits beside Write one in Add skill, and asks BOSS", () => {
    const write = ADD_SKILL.indexOf('way: "write"');
    const describe = ADD_SKILL.indexOf('way: "describe"');
    expect(write).toBeGreaterThan(0);
    expect(describe).toBeGreaterThan(write);
    expect(HOME).toContain('if (way === "describe") { askBossAboutSkills(projectId); return; }');
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
