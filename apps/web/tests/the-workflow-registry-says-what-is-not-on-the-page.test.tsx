/**
 * The registry draws the first eight workflows, the first eight runs, and the
 * first ten events of a pinned run. When a list is longer than that page, the
 * screen says how many are on it and how many are not. A list that fits stays quiet.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type {
  WorkflowDefinitionSummary,
  WorkflowRunDetail,
  WorkflowRunSummary,
} from "@boss/api-client";
import { WorkflowRegistryView } from "../app/workflow-registry-ui";

const STAMP = "2026-07-18T00:00:00Z";

function definition(n: number): WorkflowDefinitionSummary {
  return {
    id: `wfd_${n}`,
    project_id: "boss-dogfood",
    workflow_key: `wf-${n}`,
    name: `Workflow ${n}`,
    status: "active",
    active_version_id: `wfv_${n}`,
    created_by: "ceo",
    created_at: STAMP,
    updated_at: STAMP,
    version_count: 1,
  };
}

function run(n: number): WorkflowRunSummary {
  return {
    id: `wfr_${n}`,
    project_id: "boss-dogfood",
    workflow_definition_id: `wfd_${n}`,
    workflow_version_id: `wfv_${n}`,
    actor: "ceo",
    status: "running",
    input: {},
    output: {},
    context: {},
    policy_snapshot: {},
    current_frontier: [],
    created_at: STAMP,
    updated_at: STAMP,
    workflow_key: `wf-${n}`,
    workflow_name: `Run ${n}`,
    version: 1,
  };
}

function detail(eventCount: number): WorkflowRunDetail {
  return {
    ...run(1),
    steps: [],
    migrations: [],
    compensations: [],
    timeline: Array.from({ length: eventCount }, (_, i) => ({
      id: `evt_${i + 1}`,
      event_type: `audit_event_${i + 1}`,
      actor: "ceo",
      data: {},
      created_at: STAMP,
    })),
  };
}

function markup(opts: {
  definitions?: number;
  runs?: number;
  events?: number;
}): string {
  return renderToStaticMarkup(
    <WorkflowRegistryView
      apiReachable
      definitions={Array.from({ length: opts.definitions ?? 1 }, (_, i) => definition(i + 1))}
      mappings={[]}
      runs={Array.from({ length: opts.runs ?? 0 }, (_, i) => run(i + 1))}
      selectedRun={opts.events === undefined ? undefined : detail(opts.events)}
    />,
  );
}

function paragraph(html: string, sentence: string): string | undefined {
  return html.match(/<p[^>]*>[^<]*<\/p>/g)?.find((p) => p.includes(sentence));
}

describe("the workflow registry says what is not on the page", () => {
  it("says eight workflows are on the page and one more is not", () => {
    const html = markup({ definitions: 9 });
    expect(paragraph(html, "8 workflows are on this page. 1 more are not.")).toBeTruthy();
    expect(html).toContain("Workflow 8");
    expect(html).not.toContain("Workflow 9");
  });

  it("does not mention a remainder when every workflow is on the page", () => {
    const html = markup({ definitions: 8 });
    expect(html).not.toContain("more are not");
    expect(html).toContain("Workflow 8");
  });

  it("says eight runs are on the page and one more is not", () => {
    const html = markup({ runs: 9 });
    expect(paragraph(html, "8 runs are on this page. 1 more are not.")).toBeTruthy();
    expect(html).toContain("Run 8");
    expect(html).not.toContain("Run 9");
  });

  it("does not mention a remainder when every run is on the page", () => {
    const html = markup({ runs: 8 });
    expect(html).not.toContain("more are not");
    expect(html).toContain("Run 8");
  });

  it("says ten events are on the page and one more is not", () => {
    const html = markup({ events: 11 });
    expect(paragraph(html, "10 events are on this page. 1 more are not.")).toBeTruthy();
    expect(html).toContain("audit event 10");
    expect(html).not.toContain("audit event 11");
  });

  it("does not mention a remainder when every event is on the page", () => {
    const html = markup({ events: 10 });
    expect(html).not.toContain("more are not");
    expect(html).toContain("audit event 10");
  });
});
