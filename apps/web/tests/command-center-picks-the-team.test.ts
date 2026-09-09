/**
 * The Command Center can choose who runs things.
 *
 * "if it's auto when will user be able to choose the agent, I did not see option in the command
 *  center too to assign the agents at the time of creation --- this design is very confusing"
 *
 * The confusion was structural, not an oversight. Build does not create a project: it sends the
 * directive down panel → PRD → code, so at the moment of pressing it there is no app for anyone
 * to be assigned to. There could not be a per-app picker there.
 *
 * What CAN be set is what the workflow picker beside it sets — a default for this project. One
 * control per level: the project's team here, this app's team on its workflow.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const HERO = codeOnly("app/ask-boss-hero.tsx");
const PICKER = codeOnly("app/default-agent-picker.tsx");

describe("choosing the team from the Command Center", () => {
  it("the picker sits beside the workflow picker", () => {
    expect(HERO).toContain("<DefaultAgentPicker");
    const workflow = HERO.indexOf("<DefaultWorkflowPicker");
    const agents = HERO.indexOf("<DefaultAgentPicker");
    expect(workflow).toBeGreaterThan(-1);
    // Adjacent, and in that order: the path first, then who walks it.
    expect(agents).toBeGreaterThan(workflow);
    expect(agents - workflow).toBeLessThan(1200);
  });

  it("it sets the PROJECT default, not one app's", () => {
    // There is no app yet when Build is pressed. A per-app picker here would be a control for
    // something that does not exist.
    expect(PICKER).toContain('const PROJECT_DEFAULT = "*"');
    expect(PICKER).toContain("Every new project here starts with these");
  });

  it("and uses the same table as the per-app assignment", () => {
    // One way a team gets set, not two that can disagree — the lesson from the workflow mapping
    // that had two writers.
    expect(PICKER).toContain("/api/project-agents");
  });

  it("an empty team is not reported as nobody", () => {
    // The defect this whole change exists to fix: an empty list means BOSS's built-in set runs,
    // which is a different statement from "no agents".
    expect(PICKER).not.toContain("Nobody");
    expect(PICKER).toContain("BOSS uses its built-in set");
    expect(PICKER).toContain("run research, ");
  });

  it("and a change reaches the agent pages that list it", () => {
    expect(PICKER).toContain('notifyChanged("agents")');
  });
});
