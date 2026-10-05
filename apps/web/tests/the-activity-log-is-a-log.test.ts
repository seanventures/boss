import { describe, expect, it } from "vitest";

import { ACTIVITY_LOG_STREAMS } from "../lib/activity-log";
import { apiParams, readLogParams } from "../lib/log-url";
import { STREAMS } from "../app/activity/log-model";
import { codeOnly } from "./_helpers/source";

/**
 * The Activity area is a LOG, not a dashboard.
 *
 * The Factory flow board used to render above the log inside `data-area="activity"`. It is a
 * status board: it answers where work stands. This screen answers what the machinery emitted,
 * and an operator arriving with "why did this fail" had to scroll past four run summaries to
 * reach the first log line. The boards live in their own areas; this one stays a log.
 */
const PAGE = codeOnly("app/page.tsx");

function activitySection(): string {
  const start = PAGE.indexOf('data-area="activity"');
  expect(start, "the activity area should exist in page.tsx").toBeGreaterThan(-1);
  const rest = PAGE.slice(start);
  const end = rest.indexOf("{shows(");
  return end === -1 ? rest : rest.slice(0, end);
}

describe("the activity area holds a log and nothing else", () => {
  it("does not render the factory flow board, nor import it", () => {
    expect(activitySection()).not.toContain("FactoryFlowBoard");
    expect(PAGE).not.toContain("factory-flow-ui");
  });

  it("renders the Log explorer", () => {
    expect(activitySection()).toContain("<LogExplorer");
  });
});

describe("every stream the API knows is offered, by an operator's name", () => {
  it("the rail offers exactly the API's ten streams", () => {
    expect(STREAMS.map((s) => s.key).sort()).toEqual([...ACTIVITY_LOG_STREAMS].sort());
  });

  it("the workflow's steps, the deploys and the container output are their own streams", () => {
    const label = Object.fromEntries(STREAMS.map((s) => [s.key, s.label]));
    expect(label.workflow_step).toBe("Workflow steps");
    expect(label.deployment).toBe("Deploys");
    expect(label.app_log).toBe("App output");
  });

  it("the queue is named for what it is, never 'infra'; dead-letters read as Gave up", () => {
    const labels = STREAMS.map((s) => s.label.toLowerCase());
    expect(labels).not.toContain("infra");
    expect(labels).toContain("gave up");
    expect(labels.join(" ")).not.toContain("deadletter");
  });
});

describe("every stream is on until the operator says otherwise", () => {
  it("no stream token means every stream, and nothing is sent", () => {
    expect(apiParams(readLogParams("?area=activity")).source).toBeUndefined();
  });

  it("picking streams sends just those; excluding one sends the exclusion", () => {
    expect(apiParams(readLogParams("?src=workflow_step,deployment")).source).toBe("workflow_step,deployment");
    expect(apiParams(readLogParams("?src=-agent_turn")).exclude_source).toBe("agent_turn");
  });
});
