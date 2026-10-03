/**
 * The workflow says who is on the work, not just who signs it off.
 *
 * A run showed eight steps and named exactly one role — the gate's `approver_role`, rendered as
 * "release_manager signs this off". The agents that build, review, test and ship it went
 * unmentioned, because the only assignment BOSS had was `STAGE_ROLES`: a map used to attribute
 * cost at write time and never displayed anywhere.
 *
 * A PANEL, NOT A STEP — the operator's call, and the right one. A step would sit in the chain
 * where it can execute, fail and block a run, and would have to be added to all twelve
 * catalogue workflows. Assignment is a property of the app, not a stage of its delivery.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const PANEL = codeOnly("app/agents-on-this.tsx");
const WORKFLOWS = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("who is on this app", () => {
  it("assignment is a chip on the run's own bar, above its lane", () => {
    // It began as a block above the rail and could not be found, then became the rail's first
    // node — a node the runtime never runs, sitting in a chain of steps that do. Workflow
    // Studio draws the run on the editor's lane, which holds only real steps, so who is on the
    // app is the run's "Agents" chip: in the run's header, where a reader of the steps is.
    expect(WORKFLOWS).toContain('data-testid="dw-run-agents"');
    const chip = WORKFLOWS.indexOf('data-testid="dw-run-agents"');
    expect(chip).toBeLessThan(WORKFLOWS.indexOf("<FlowLane spec={spec}"));
    expect(WORKFLOWS).toContain("{showAgents ? (");
    expect(WORKFLOWS).toContain("<AgentsOnThis");
  });

  it("but it is not a runtime step: the lane is never told about it", () => {
    // A real step would mean editing all twelve catalogue workflows and adding something that
    // can execute, fail and block a run. The lane's spec is the run's own steps.
    expect(WORKFLOWS).not.toContain("__agents__");
    expect(WORKFLOWS).toContain("const spec = useRunSpec(projectId, run, capabilities);");
  });

  it("and the run's header opens the workflow it follows", () => {
    expect(WORKFLOWS).toContain('data-testid="dw-run-open-workflow"');
    expect(WORKFLOWS).toContain("onOpenWorkflow(run.workflow_key)");
  });

  it("and a finished run shows it as a record, not a setting", () => {
    // Offering "+ Assign" on a completed workflow invites a change that cannot affect
    // anything: the work is done and the gates it would have governed are already answered.
    expect(WORKFLOWS).toContain("readOnly={terminal}");
    // REWRITTEN: "finished" is the server's fact (the engine's FINISHED_RUN_STATES), not a
    // list kept here — that list had left out `compensated`.
    expect(WORKFLOWS).toContain("const terminal = run.finished === true;");
    expect(WORKFLOWS).not.toContain('["completed", "cancelled", "failed", "deadlettered"]');
    expect(PANEL).toContain("readOnly ? null : (");
    expect(PANEL).toContain("a record now, not a setting");
  });

  it("and it is a panel, not a step in the chain", () => {
    // If this ever became a capability it would appear in the catalogue, where it could gate a
    // run. Assert it has not: no `assign_agents` capability anywhere.
    expect(codeOnly("app/delivery-workflows-ui.tsx")).not.toContain('"assign_agents"');
  });

  it("keyed on the app, so a re-run keeps the assignment", () => {
    expect(WORKFLOWS).toContain("appName={appIdentity(run.input as AppRun).name}");
  });

  it("an empty table is never reported as nobody working", () => {
    // THE LIE. "Nobody is assigned, so any agent may work on this" appeared over a LIVE
    // Standard Product Delivery run with ten steps — agents were unquestionably running it.
    // The panel was reporting the state of a table as a fact about the world.
    expect(PANEL).not.toContain("Nobody is assigned");
    expect(PANEL).not.toContain("Nobody was assigned to this run");
    // An app with no row is running the project's team, and that is what it shows.
    expect(PANEL).toContain("These run by default");
    expect(PANEL).toContain('builtin: "by default"');
  });

  it("and the three sources are told apart in words", () => {
    // `builtin` is the one that matters: nothing recorded, and BOSS runs the project's team
    // anyway. `seeded` was written when the run started. `chosen` is a person.
    expect(PANEL).toContain("SOURCE_LABEL");
    for (const word of ["by default", "seeded", "chosen"]) {
      expect(PANEL).toContain(word);
    }
  });

  it("a seeded default is distinguishable from a person's choice", () => {
    // BOSS seeds the agents that would have run anyway. That is a guess; a person choosing is
    // a decision. The gate refuses on both, so which one it is matters when it refuses.
    expect(PANEL).toContain("SOURCE_LABEL[a.source]");
    expect(CSS).toContain('.aot-agent[data-source="operator"]');
  });

  it("and the panel says the assignment has teeth", () => {
    expect(PANEL).toContain("Only these agents may sign off");
    expect(PANEL).toContain("Anyone else is refused");
  });

  it("each name links to that agent's page", () => {
    expect(PANEL).toContain("?area=workforce&role=");
  });

  it("a change here reaches the agent's own page", () => {
    // The detail view lists what an agent is assigned to; assigning from here must not leave
    // it stale. Same signal the rest of the console already uses.
    expect(PANEL).toContain('notifyChanged("agents")');
    expect(PANEL).toContain("onChanged(");
  });

  it("the picker does not cost a scoreboard", () => {
    // /v1/console/v2/workforce computes spend and live status for 26 agents. A name picker
    // should not pay for that.
    expect(codeOnly("app/api/agent/route.ts")).toContain("/v1/roles?project_id=");
  });
});
