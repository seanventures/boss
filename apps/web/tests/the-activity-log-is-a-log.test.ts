import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The Activity area is a LOG VIEWER, not a dashboard.
 *
 * The Factory flow board — "Where official work is, and why it stalled" — used to render above
 * the log inside `data-area="activity"`. It is a status board: it answers where work stands.
 * This screen answers a different question, which is what the machinery actually emitted, and
 * an operator arriving with "why did this fail" had to scroll past four run summaries to reach
 * the first log line.
 *
 * The boards live in their own areas. This one stays a log.
 */
const PAGE = codeOnly("app/page.tsx");
const UI = codeOnly("app/activity-log-ui.tsx");

/** The body of the `activity` section, from its opening tag to the next section. */
function activitySection(): string {
  const start = PAGE.indexOf('data-area="activity"');
  expect(start, "the activity area should exist in page.tsx").toBeGreaterThan(-1);
  const rest = PAGE.slice(start);
  const end = rest.indexOf("{shows(");
  return end === -1 ? rest : rest.slice(0, end);
}

describe("the activity area holds a log and nothing else", () => {
  it("does not render the factory flow board", () => {
    expect(activitySection()).not.toContain("FactoryFlowBoard");
  });

  it("does not import it either, so it cannot drift back in unnoticed", () => {
    expect(PAGE).not.toContain("factory-flow-ui");
  });

  it("still renders the log itself", () => {
    expect(activitySection()).toContain("<ActivityLog");
  });
});

describe("the streams an operator asked for are offered", () => {
  /**
   * Three gaps, all found by opening this screen with a real question:
   *
   *   - the workflow's own steps, with duration and error, were not a source at all;
   *   - "BOSS infra" meant jobs + deadletters + receipts, so "did the deploy fail?" was
   *     answered with the work queue;
   *   - the container output of the apps BOSS builds never reached the log.
   */
  it("offers the workflow's steps as their own stream", () => {
    expect(UI).toContain('value: "workflow_step"');
  });

  it("offers deployments and container output, separately from the queue", () => {
    expect(UI).toContain('value: "deployment"');
    expect(UI).toContain('value: "platform"');
  });

  it("no longer calls the work queue 'infra'", () => {
    // The value stays `infra` — it is a documented value of the public API and callers link to
    // it — but what the operator READS must say what it actually is.
    expect(UI).toContain('value: "infra", label: "BOSS queue"');
    expect(UI).not.toContain('label: "BOSS infra"');
  });
});
