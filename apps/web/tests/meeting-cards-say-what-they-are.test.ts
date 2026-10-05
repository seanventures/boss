import { describe, expect, it } from "vitest";

import { meetingCardTitle } from "../lib/meeting-title";
import { codeOnly } from "./_helpers/source";

/**
 * The Meetings screen, as an operator reads it.
 *
 * boss-1, 2026-10-02: four workflow-convened meetings, four cards reading "PRD meeting: --- WHAT
 * THIS PANEL IS FOR ---", the same four rows in the consensus picker, every card "awaiting
 * approval" (including meetings whose PRD fed shipped projects), and a vote-tally form prefilled
 * Approve 2 / Reject 1 with "requires meeting_id" copy. The titles are now fixed at the source
 * (engine meeting_subject; engine and API tests cover that); these pin the screen's side.
 */
const ROOM = codeOnly("app/meeting-room-ui.tsx");
const PHASE3 = codeOnly("app/phase3-ui.tsx");

describe("meeting cards", () => {
  it("drop only the shared prefix from a title", () => {
    expect(meetingCardTitle("PRD meeting: Build a recipe planner")).toBe("Build a recipe planner");
    expect(meetingCardTitle("PRD meeting:   ")).toBe("Untitled meeting");
  });

  it("the cards and the consensus picker read titles through the same function", () => {
    expect(ROOM).toContain("shortTitle(meetingCardTitle(m.title))");
    expect(PHASE3).toContain("meetingCardTitle(item.title)");
  });

  it("say where the PRD stands from the API's prd_label, never a hard-coded guess", () => {
    expect(ROOM).toContain("m.prd_label");
    expect(ROOM).not.toMatch(/>\s*awaiting approval\s*</);
  });
});

describe("the consensus card is an operator form", () => {
  it("starts with no votes filled in", () => {
    const body = PHASE3.slice(PHASE3.indexOf("export function MeetingConsensusCard"),
                              PHASE3.indexOf("export function FactoryScaleCard"));
    expect(body).not.toMatch(/name="(approve|reject|abstain)"[^>]*defaultValue/);
    expect(body).not.toContain("requires meeting_id");
    expect(body).not.toContain("mtg_… or mrun_…");
  });

  it("keeps the stall sweep, folded under Maintenance with what it does", () => {
    const body = PHASE3.slice(PHASE3.indexOf("export function MeetingConsensusCard"));
    expect(body).toContain("Sweep stalled meeting runs");
    expect(body).toContain("<summary className=\"detail\">Maintenance</summary>");
  });
});
