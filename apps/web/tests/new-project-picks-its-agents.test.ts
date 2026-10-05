/**
 * The new-project wizard says who will work on it.
 *
 * It collected a title, a directive, an audience, acceptance criteria, a workflow and a
 * repository — `NewProjectDraft` had seven fields and not one of them was an agent. So a
 * project was created with no say in who would build it, and the only answer to "who is on
 * this" was whatever STAGE_ROLES happened to imply at each stage.
 *
 * The defaults are shown PRE-SELECTED rather than as an empty box, because they are what BOSS
 * seeds anyway: an empty picker would imply nobody is coming, which is the opposite of true.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

// The Runs composer lives in its own module since the Workflows area became Runs + Library.
const UI = codeOnly("app/workflows/new-project-composer.tsx");

describe("choosing who builds it", () => {
  it("the draft carries agents", () => {
    expect(UI).toContain("agents: string[]");
    expect(UI).toContain("agents: []");
  });

  it("the defaults come from the API, not from a second copy of the rule", () => {
    // start_run seeds from STAGE_ROLES plus the chosen workflow's approver roles. A copy of
    // that rule in the console would drift from the one that actually runs, and the wizard
    // would promise agents the run does not assign.
    expect(UI).toContain("defaults=1");
    expect(UI).toContain("default_roles");
  });

  it("and they follow the chosen workflow", () => {
    // A workflow whose gate names release_manager needs them assigned or the gate refuses.
    // Adding a feature, the workflow of the change: Small seeds a different team from Standard.
    expect(UI).toContain("workflow_key=${encodeURIComponent(teamKey)}");
    expect(UI).toContain("[projectId, teamKey]");
  });

  it("they are shown pre-selected, not as an empty picker", () => {
    // New project's Team fact: the defaults (or, adding to an app, the crew that built it).
    expect(UI).toContain("draft.agents.length ? draft.agents");
    expect(UI).toContain(": defaultAgents");
    expect(UI).toContain('data-testid="np-team"');
  });

  it("an unchanged list is not written back as a decision", () => {
    // The panel distinguishes a seeded default from a person's choice, and the gate refuses on
    // both. Recording an identical list as `operator` would claim a decision nobody made.
    expect(UI).toContain("sameAsDefault");
    expect(UI).toContain("chosenAgents.length && !sameAsDefault");
  });

  it("and a removal actually removes", () => {
    // Assigning the kept ones is not enough: the run has already seeded the full default set,
    // so anyone taken off in the wizard is still assigned unless they are unassigned for real.
    expect(UI).toContain('action: "unassign"');
    expect(UI).toContain("defaultAgents.filter((d) => !chosenAgents.includes(d))");
  });

  it("the change is reversible before it is committed", () => {
    expect(UI).toContain("Put it back");
  });

  it("and a failed assignment does not lose the run", () => {
    // The run is already started and seeded by the time this fires, so a failure here must not
    // surface as "could not start it". Asserted as the property — the comment saying so is
    // stripped by codeOnly, and pinning comment text is how a grep test rots.
    const at = UI.indexOf("const assignTeam");
    const block = UI.slice(at, UI.indexOf("const startRun", at));
    expect(block).toContain("try {");
    expect(block).toContain("} catch");
    expect(block).not.toContain("setError(");
  });
});
