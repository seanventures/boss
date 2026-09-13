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


/**
 * The page stopped fitting on a screen.
 *
 * Four expanded sections — channels, authoring, custom skills, seventeen built-ins — and the
 * thing an operator came to do was somewhere in the middle of all of it. Reported as "all of
 * these are growing the page long".
 *
 * So: authoring is first and open, because it is what you came for. Everything else is shut,
 * and each shut section says what is inside it, because "Custom skills" tells a reader nothing
 * about whether opening it is worth the click.
 */
describe("the skills page fits on a screen", () => {
  const CHANNELS = codeOnly("app/channels-ui.tsx");
  const PAGE = codeOnly("app/page.tsx");

  it("authoring is first, then channels, then the catalogues", () => {
    // Channels is passed as a CHILD rather than rendered beside, so the order lives in one
    // file instead of being split between the mount and the component's own layout.
    expect(PAGE).toContain("<SkillsHome projectId={activeProjectId} actor={activeActorId}>");
    expect(PAGE).toContain("<ChannelsHome projectId={activeProjectId} actor={activeActorId} />");
    const agent = SKILLS.indexOf('data-testid="skill-agent"');
    const slot = SKILLS.indexOf("{children}");
    const custom = SKILLS.indexOf('data-testid="skills-custom"');
    const builtin = SKILLS.indexOf('data-testid="skills-builtin"');
    expect(agent).toBeLessThan(slot);
    expect(slot).toBeLessThan(custom);
    expect(custom).toBeLessThan(builtin);
  });

  it("the catalogues are shut, and say what is in them", () => {
    for (const id of ["skills-custom", "skills-builtin"]) {
      expect(SKILLS).toContain(`<details className="stack-card" data-testid="${id}">`);
    }
    // A COUNT, not just a label. "Custom skills" tells a reader nothing about whether opening
    // it is worth the click; "Custom skills (3)" does. Asserted on both summaries.
    const custom = SKILLS.slice(SKILLS.indexOf('data-testid="skills-custom"'));
    expect(custom.slice(0, 400)).toContain("skills.length");
    const builtin = SKILLS.slice(SKILLS.indexOf('data-testid="skills-builtin"'));
    expect(builtin.slice(0, 400)).toContain("builtIn.length");
  });

  it("the channel list is not collapsible, because it is already short", () => {
    // COLLAPSING THIS WAS WRONG TWICE OVER.
    //
    // A connected channel is one line, so it was never what made the page long — the client
    // form and the twenty-two built-in skills were. And a summary reading "Channels — Makkal
    // Sinthanai" names ONE channel, which makes "expand it to connect a different one"
    // incoherent, which is exactly how it was reported.
    expect(CHANNELS).toContain('<article className="stack-card" data-testid="channel-connections">');
    expect(CHANNELS).not.toContain('<details className="stack-card" data-testid="channel-connections"');
  });

  it("what varies is the explanation, not the visibility", () => {
    // A lot of explanation on the first visit; none once a channel is connected. That is the
    // thing that should shrink, rather than hiding the status itself.
    expect(CHANNELS).toContain("{connected.length === 0 ? (");
    // And connecting another stops being the loudest control once one exists.
    expect(CHANNELS).toContain(
      'className={connected.length === 0 ? "cap-btn primary" : "linklike"}');
  });
});
