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
  it("the panel is rendered on the run, above the step chain", () => {
    expect(WORKFLOWS).toContain("<AgentsOnThis");
    // Above StepPath, not below it: the question "who is doing this" comes before "what are
    // the steps", and a reader who has already scrolled the chain has stopped asking.
    const panel = WORKFLOWS.indexOf("<AgentsOnThis");
    const chain = WORKFLOWS.indexOf("<StepPath run={run}");
    expect(panel).toBeGreaterThan(-1);
    expect(panel).toBeLessThan(chain);
  });

  it("and it is a panel, not a step in the chain", () => {
    // If this ever became a capability it would appear in the catalogue, where it could gate a
    // run. Assert it has not: no `assign_agents` capability anywhere.
    expect(codeOnly("app/delivery-workflows-ui.tsx")).not.toContain('"assign_agents"');
  });

  it("keyed on the app, so a re-run keeps the assignment", () => {
    expect(WORKFLOWS).toContain("appName={appIdentity(run.input as AppRun).name}");
  });

  it("nobody assigned is stated as a choice not yet made, not as a fault", () => {
    // An app with no assignment lets any agent act — enforcement stops work that contradicts a
    // decision, not work nobody has decided about. Saying "unassigned" alone would read as
    // broken.
    expect(PANEL).toContain("Nobody is assigned, so any agent may work on this");
    expect(PANEL).toContain("Assign someone to make that a");
  });

  it("a seeded default is distinguishable from a person's choice", () => {
    // BOSS seeds the agents that would have run anyway. That is a guess; a person choosing is
    // a decision. The gate refuses on both, so which one it is matters when it refuses.
    expect(PANEL).toContain('a.source === "default" ? "default" : "assigned"');
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
