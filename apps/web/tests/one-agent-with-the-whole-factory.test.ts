import { describe, expect, it } from "vitest";

import { itemHeadline, itemQuestion, itemSubject } from "../app/boss-agent-home";
import { codeOnly } from "./_helpers/source";

/**
 * "please come up with the new design for common Agent and it should have full picture of the
 * BOSS software factory... I'm happy with the recently built all agents like Workflow, Infra,
 * Factory etc.. I want such quality"
 *
 * Three surfaces became one. The chat area was a thread rail and a composer that knew nothing
 * about the system it was attached to. The Metatron Captain was a page over `metatron_sessions`
 * — a table that does not exist in this database. The Task Shepherd was an autopilot switched
 * off after one afternoon of repairing things nobody asked about.
 *
 * What the operator likes is the SAME agent on Workflows, Infra, Factory and Monitoring, and
 * what makes it good there is not the model: those screens hand it their facts before the first
 * word. A composer with no context is the same agent doing worse, because the operator has to
 * type what the page already knew.
 */
const decision = {
  kind: "decision", id: "dec_1", title: "Merge PR #186", note: "past its deadline",
};
const step = {
  kind: "workflow_step", id: "wfs_1", app: "Lead-Triage Inbox", step: "deploy",
  status: "deadlettered", error: "no active staging target", run_id: "wfr_1",
};
const down = {
  kind: "unhealthy_app", id: "deptgt_1", app: "Change Calendar", environment: "staging",
  at: "192.0.2.202:3013", error: "connection refused",
};
const quiet = {
  kind: "quiet_build", id: "crun_1", app: "Interview Scorecard App", phase: "implement",
  quiet_minutes: 27,
};

describe("every row names something real", () => {
  it.each([
    [decision, "Merge PR #186"],
    [step, "Lead-Triage Inbox — deploy deadlettered"],
    [down, "Change Calendar (staging) at 192.0.2.202:3013"],
    [quiet, "Interview Scorecard App — 27m quiet in implement"],
  ])("%#", (item, expected) => {
    expect(itemHeadline(item as never)).toBe(expected);
  });
});

describe("and the question it asks is about the row's own record", () => {
  // boss-1 (2026-10-02): the row's facts used to be pasted into the question, with "Check the
  // app's code and the host before answering" on the end — the sentence that sent a Preview
  // step's turn to the deployment tools for 276 seconds. The row now hands over WHAT it is
  // (a typed subject the server resolves into its whole record) and a question in the
  // operator's own words.
  it("a stopped step is asked about as that step of that run", () => {
    expect(itemSubject(step as never)).toEqual(
      { kind: "workflow_step", id: "wfr_1", step: "deploy" });
    const q = itemQuestion(step as never);
    expect(q).toContain("Lead-Triage Inbox");
    expect(q).toContain("'deploy' step");
    expect(q).not.toContain("Check the app's code and the host");
  });

  it("an app that is down is asked about as its deployment target", () => {
    expect(itemSubject(down as never)).toEqual({ kind: "deployment", id: "deptgt_1" });
    expect(itemQuestion(down as never)).toContain("Change Calendar (staging)");
  });

  it("a quiet build asks whether it is working — and forbids cancelling it", () => {
    // The incident this whole effort started from: asked why a build was slow, BOSS cancelled
    // it. Twenty-seven minutes of work, and nobody had asked for that.
    expect(itemSubject(quiet as never)).toEqual({ kind: "code_task", id: "crun_1" });
    const q = itemQuestion(quiet as never);
    expect(q).toContain("working or stuck");
    expect(q).toContain("do not cancel anything");
  });

  it("a decision asks what it is gating rather than just naming it", () => {
    expect(itemSubject(decision as never)).toEqual({ kind: "decision", id: "dec_1" });
    expect(itemQuestion(decision as never)).toContain("What is it gating");
  });
});

describe("the shape of the page", () => {
  const UI = codeOnly("app/boss-agent-home.tsx");

  it("the briefing comes before the conversation", () => {
    expect(UI.indexOf('data-testid="agent-briefing"')).toBeLessThan(UI.indexOf("<AgentChat"));
  });

  it("it uses the same chat component as the dock, not a second one", () => {
    // Two chats in one product drift, and then only one of them gets the next fix.
    expect(UI).toContain('import { AgentChat } from "./agent-ui";');
  });

  it("the Shepherd's diagnosis is shown and never run", () => {
    expect(UI).toContain('data-testid="agent-diagnosis"');
    expect(UI).toContain("usually:");
    expect(UI).not.toContain("autopilot");
  });

  it("a failed briefing is not rendered as 'nothing needs you'", () => {
    // Opposite claims. Rendering the second as the first is how a console says everything is
    // fine during an outage.
    expect(UI).toContain('data-testid="agent-briefing-failed"');
    expect(UI).toContain("This is not the same as “nothing needs\n            you”");
  });

  it("and it reloads when something changes elsewhere", () => {
    expect(UI).toContain('onChanged(() => { void load(); }, "agent-home")');
  });
});
