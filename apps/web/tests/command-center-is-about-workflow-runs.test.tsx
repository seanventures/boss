/**
 * The Command Center shows what needs the operator, what is running and how it went — in runs.
 *
 * The old landing page read `tasks`. A run started by Build creates no task, so the thing the
 * operator had just started never appeared; "Live coordination" animated idle orbs from a
 * field nothing populates; "Pending decisions" linked nowhere; two buttons were dead links.
 */
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CommandCenter } from "../app/command-center-ui";
import {
  agentPhrase,
  commandCenterFrom,
  decisionHref,
  elapsedLabel,
  needsYouLabel,
  runHref,
  type CommandCenterData,
} from "../lib/command-center";

const DATA: CommandCenterData = commandCenterFrom({
  observed_at: "2026-10-02T12:00:00+00:00",
  running: [{
    run_id: "wfr_live", workflow_name: "Standard Product Delivery",
    subject: "Incident timeline builder", status: "running", steps_done: 3, step_count: 9,
    started_at: "2026-10-02T11:00:00+00:00", step_id: "build", step_label: "Build, review and test",
    step_status: "running",
    agent: { role_key: "engineering_captain", name: "Engineering Captain", how: "default" },
    since: "2026-10-02T11:48:00+00:00", elapsed_seconds: 720, blocked_reason: null,
  }],
  needs_you: [
    { kind: "approval", decision_kind: "deploy_gate", title: "“Deploy” is waiting for you",
      detail: "Builds a container…", decision_id: "dec_1", run_id: "wfr_live", step_id: "deploy",
      actions: ["approve", "reject", "delegate"], at: null },
    { kind: "failed_step", title: "“Build” failed", detail: "the sandbox ran out of disk",
      what_to_do: "Fix the cause, then Retry.", run_id: "wfr_dead", step_id: "build",
      // The run's own answers (integration: workflow_run_actions.stopped_for_operator).
      actions: ["retry_step"],
      answers: [{ key: "retry_step", label: "Try build again", enabled: true, reason: null,
                  step_id: "build" }],
      at: null },
    { kind: "approval", decision_kind: "refusal", title: "Model capacity refused",
      detail: "No model had credit.", what_to_do: "Add credit, then Retry the step.",
      fix_area: "setup", fix_label: "Fix in Repo and models", decision_id: "dec_9",
      actions: ["reject"], at: null },
    { kind: "readiness", title: "Connect a repository", detail: "", fix_area: "setup",
      fix_focus: "repository", fix_label: "Fix in Repo and models", actions: [], at: null },
  ],
  recent: [{ run_id: "wfr_done", workflow_name: "Standard Product Delivery", subject: "Bug box",
             outcome: "succeeded", finished_at: "2026-10-01T09:00:00+00:00", error: null }],
  counts: { running: 1, needs_you: 4, approvals: 2, failed_steps: 1, readiness: 1,
            succeeded_7d: 1, failed_7d: 0 },
});

const html = renderToStaticMarkup(
  <CommandCenter projectId="p" orgId="org_1" actor="ceo" initial={DATA} />);

describe("what is running", () => {
  it("shows the run, its current step, the agent on it and how long it has been there", () => {
    expect(html).toContain("Incident timeline builder");
    expect(html).toContain("Build, review and test");
    expect(html).toContain("Engineering Captain");
    expect(html).toContain("12m");
    expect(html).toContain("3/9 steps");
  });

  it("links straight into the run", () => {
    expect(html).toContain(`href="${runHref("wfr_live", "org_1").replace(/&/g, "&amp;")}"`);
  });
});

describe("what needs you", () => {
  it("a gate links to its decision and its run", () => {
    expect(html).toContain(decisionHref("dec_1", "org_1").replace(/&/g, "&amp;"));
    expect(html).toContain("Review and decide");
    expect(html).toContain("Deploy gate");
  });

  it("a failed step says why and offers Retry", () => {
    expect(html).toContain("the sandbox ran out of disk");
    expect(html).toContain('data-testid="cc2-retry"');
  });

  it("offers Retry only when the run itself offers it", () => {
    // Integration: the Retry is the run's own answer (stopped_for_operator). A failed step the
    // run offers no enabled retry for — a refused approval, a superseded attempt — gets none.
    const failed = DATA.needs_you.find((n) => n.kind === "failed_step")!;
    const without = renderToStaticMarkup(
      <CommandCenter projectId="p" orgId="org_1" actor="ceo"
                     initial={{ ...DATA, needs_you: [{ ...failed, answers: [
                       { key: "retry_step", label: "Try build again", enabled: false,
                         reason: "a newer attempt is running", step_id: "build" }] }] }} />);
    expect(without).toContain("the sandbox ran out of disk");
    expect(without).not.toContain('data-testid="cc2-retry"');
  });

  it("a refusal says what to fix and links there, not to an Approve button", () => {
    expect(html).toContain("Add credit, then Retry the step.");
    expect(html).toContain("Fix in Repo and models");
    expect(html).toContain("area=setup");
  });

  it("a setup gap is listed, and lands on its row in Repo and models", () => {
    expect(html).toContain("Connect a repository");
    expect(html).toContain("area=setup&amp;focus=repository");
  });
});

it("recent outcomes link to their runs", () => {
  expect(html).toContain("Bug box");
  expect(html).toContain("wfr_done");
});

it("an empty factory says so, and how to start", () => {
  const empty = renderToStaticMarkup(
    <CommandCenter projectId="p" orgId="org_1" actor="ceo" initial={commandCenterFrom({})} />);
  expect(empty).toContain('data-testid="cc2-needs-you-empty"');
  expect(empty).toContain('data-testid="cc2-running-empty"');
  expect(empty).toContain("area=delivery_workflows");
});

it("an unreachable API is said, not shown as an empty factory", () => {
  const down = renderToStaticMarkup(
    <CommandCenter projectId="p" actor="ceo" initial={{
      ...commandCenterFrom({}), apiReachable: false, blockedReason: "Postgres is not reachable." }} />);
  expect(down).toContain('data-testid="cc2-load-error"');
  expect(down).toContain("Postgres is not reachable.");
});

describe("the words", () => {
  it("elapsed reads like a person would say it", () => {
    expect(elapsedLabel(42)).toBe("42s");
    expect(elapsedLabel(12 * 60)).toBe("12m");
    expect(elapsedLabel(65 * 60)).toBe("1h 05m");
    expect(elapsedLabel(3 * 86400 + 4 * 3600)).toBe("3d 4h");
    expect(elapsedLabel(null)).toBe("");
  });

  it("the agent says how it came to be on the step", () => {
    expect(agentPhrase({ role_key: "qa", name: "QA", how: "picked" })).toContain("picked by BOSS");
    expect(agentPhrase({ role_key: null, name: null, how: "auto" })).toContain("BOSS picks");
    expect(agentPhrase({ role_key: null, name: "You", how: "person" })).toBe("Waiting on you");
  });

  it("each approval type has its own label", () => {
    const base = { kind: "approval" as const, title: "", detail: "", actions: [], at: null };
    expect(needsYouLabel({ ...base, decision_kind: "pull_request" })).toBe("PR merge");
    expect(needsYouLabel({ ...base, decision_kind: "refusal" })).toBe("Refused");
    expect(needsYouLabel({ ...base, decision_kind: "deploy_gate" })).toBe("Deploy gate");
  });
});

it("both pages mount it in place of the task-era panels", () => {
  // The Community tree has one page: the private repo's page-community.tsx IS this page.tsx
  // (the export's page_export_rewrite), so the console-a stream's two-page loop reads one here.
  for (const page of ["app/page.tsx"]) {
    const src = readFileSync(page, "utf8");
    expect(src).toContain("<CommandCenter");
    expect(src).toContain('fetchCommandCenter(activeProjectId, needs("overview"))');
    expect(src).not.toContain('area="apps"');
    expect(src).not.toContain('<div className="cc-kpis">');
    expect(src).not.toContain("Pending decisions");
  }
});
