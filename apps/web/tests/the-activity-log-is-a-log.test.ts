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
    expect(UI).toContain('value: "app_log"');
  });

  it("no longer calls the work queue 'infra' in the console", () => {
    // The API keeps `infra` as a group — it is a documented value and callers link to it — but
    // the console offers the three sources it stood for as their own toggles, so nothing an
    // operator READS calls a work queue "infra" any more.
    expect(UI).not.toContain('label: "BOSS infra"');
  });

  it("offers every source the API knows about", () => {
    // A source added to the engine and not to this list is a stream nobody can reach.
    for (const value of ["event", "process_step", "job", "deadletter", "receipt",
                         "llm_call", "agent_turn", "app_log", "workflow_step",
                         "deployment"]) {
      expect(UI).toContain(`value: "${value}"`);
    }
  });
});

describe("every stream is on until the operator says otherwise", () => {
  /**
   * A log's job is to show what happened. A viewer that opens already filtered hides things
   * nobody asked it to hide — so the default is everything, and unticking is the act of
   * filtering.
   */
  it("starts with all of them enabled", () => {
    expect(UI).toContain("useState<Set<string>>(\n    () => new Set(ALL_SOURCES),\n  )");
  });

  it("sends nothing at all when everything is on", () => {
    // Listing all ten names would mean the same thing to the API and would cost the request
    // its unfiltered fast path, which is what keeps the first page of a large project quick.
    expect(UI).toContain("allSourcesOn\n    ? undefined");
  });

  it("does not answer 'none of them' with the whole log", () => {
    // An empty `source` means EVERY stream to the API, so sending one when nothing is ticked
    // would show the operator the opposite of what they asked for.
    expect(UI).toContain("if (noSourcesOn)");
    expect(UI).toContain('data-testid="activity-no-sources"');
  });

  it("has a way back to everything", () => {
    expect(UI).toContain('data-testid="activity-source-all"');
    expect(UI).toContain("setEnabledSources(new Set(ALL_SOURCES))");
  });

  it("shows how many rows each stream contributed", () => {
    // A toggle with a count says whether unticking it will change anything — and a stream
    // reading 0 over a long window says BOSS is not writing it, not that the filter is wrong.
    // Counted against the LOG (server-side facets), not the page — see activity-log-viewer.
    expect(UI).toContain("chipCount(facets?.sources, choice.value)");
  });
});
