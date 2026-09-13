import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * You get a skill by describing one, not by filling in a form about it.
 *
 * The authoring form asks an operator to already know what a behaviour kind is, which fields a
 * sandboxed command needs, and that its image has to be digest-pinned. That is the shape of the
 * stored record, not the shape of anything a person wants to say. What they want to say is
 * "trim a video between two timecodes and stamp our watermark on it".
 *
 * Reported as: "The New Skill option actually dont make sense to me."
 *
 * So the conversation is the way in — BOSS asks for what it still needs and offers the finished
 * skill as one press (draft_skill) — and the form stays, folded away, for correcting one field
 * afterwards. Deleting it would trade one dead end for another: a wrong risk tier should not
 * require a second conversation.
 */
const SKILLS = codeOnly("app/skills-ui.tsx");

describe("skills are authored by conversation", () => {
  it("the conversation is what the page offers first", () => {
    expect(SKILLS).toContain('data-testid="skill-agent"');
    expect(SKILLS).toContain('data-testid="skill-agent-open"');
    expect(SKILLS).toContain("Design a skill with BOSS");
    // Ahead of the form in the document, because order is the recommendation.
    expect(SKILLS.indexOf('data-testid="skill-agent"'))
      .toBeLessThan(SKILLS.indexOf('data-testid="skill-form"'));
  });

  it('"New skill" is no longer the heading an operator meets', () => {
    expect(SKILLS).not.toContain('"New skill"');
    expect(SKILLS).not.toContain("The form below is the whole of it");
  });

  it("the form survives, folded away rather than deleted", () => {
    // A wrong risk tier should be one field to fix, not a second conversation.
    expect(SKILLS).toContain('<details className="stack-card" data-testid="skill-form">');
    expect(SKILLS).toContain("skill-form-summary");
    expect(SKILLS).toContain("Write one by hand instead");
    // And editing still reaches it by name.
    expect(SKILLS).toContain("`Edit ${draft.skill_key} by hand`");
  });

  it("the door is addressed to the role whose remit this is", () => {
    const fn = SKILLS.slice(SKILLS.indexOf("function askBossAboutSkills"));
    const body = fn.slice(0, fn.indexOf("\n}"));
    expect(body).toContain('"dock_role", "Program Manager"');
    expect(body).toContain("dock_ask");
    // The opener must tell BOSS to look first, or it will offer a skill that already exists.
    expect(body).toContain("already has");
  });
});
