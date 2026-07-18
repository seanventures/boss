import { renderToStaticMarkup } from "react-dom/server";
import React from "react";
import { describe, expect, it } from "vitest";

import { WorkflowRegistryView } from "../app/workflow-registry-ui";
import { emptyWorkflowFeed } from "../lib/workflows";


describe("workflow registry", () => {
  it("keeps API failures explicit", () => {
    const feed = emptyWorkflowFeed("API returned 503", "boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("API returned 503");
    expect(feed.definitions).toEqual([]);
    expect(feed.runs).toEqual([]);
  });

  it("renders definitions, pinned steps, and audit events", () => {
    const html = renderToStaticMarkup(
      <WorkflowRegistryView
        apiReachable
        definitions={[
          {
            id: "wfd_1",
            project_id: "boss-dogfood",
            workflow_key: "release",
            name: "Release",
            status: "active",
            active_version_id: "wfv_1",
            created_by: "ceo",
            created_at: "2026-07-18T00:00:00Z",
            updated_at: "2026-07-18T00:00:00Z",
            version_count: 1,
          },
        ]}
        mappings={[
          {
            id: "wfm_1",
            project_id: "boss-dogfood",
            workflow_version_id: "wfv_1",
            selector_kind: "work_class",
            selector_value: "release",
            scope_type: "project",
            scope_id: "boss-dogfood",
            priority: 100,
            enabled: true,
            created_by: "ceo",
            created_at: "2026-07-18T00:00:00Z",
            updated_at: "2026-07-18T00:00:00Z",
          },
        ]}
        runs={[
          {
            id: "wfr_1",
            project_id: "boss-dogfood",
            workflow_definition_id: "wfd_1",
            workflow_version_id: "wfv_1",
            actor: "ceo",
            status: "running",
            input: {},
            output: {},
            context: {},
            policy_snapshot: {},
            current_frontier: [{}],
            created_at: "2026-07-18T00:00:00Z",
            updated_at: "2026-07-18T00:00:00Z",
            workflow_key: "release",
            workflow_name: "Release",
            version: 1,
          },
        ]}
        selectedRun={{
          id: "wfr_1",
          project_id: "boss-dogfood",
          workflow_definition_id: "wfd_1",
          workflow_version_id: "wfv_1",
          actor: "ceo",
          status: "running",
          input: {},
          output: {},
          context: {},
          policy_snapshot: {},
          current_frontier: [{}],
          created_at: "2026-07-18T00:00:00Z",
          updated_at: "2026-07-18T00:00:00Z",
          workflow_key: "release",
          workflow_name: "Release",
          version: 1,
          steps: [
            {
              id: "wfs_1",
              project_id: "boss-dogfood",
              workflow_run_id: "wfr_1",
              step_id: "build",
              step_kind: "task",
              iteration: 0,
              status: "ready",
              input: {},
              output: {},
              binding_snapshot: {},
              evidence: [],
              attempt: 0,
              max_attempts: 3,
              run_after: "2026-07-18T00:00:00Z",
              created_at: "2026-07-18T00:00:00Z",
              updated_at: "2026-07-18T00:00:00Z",
            },
          ],
          migrations: [],
          compensations: [],
          timeline: [
            {
              id: "evt_1",
              event_type: "workflow.run_started",
              actor: "ceo",
              data: {},
              created_at: "2026-07-18T00:00:00Z",
            },
          ],
        }}
      />,
    );

    expect(html).toContain("Reusable workflows");
    expect(html).toContain("release");
    expect(html).toContain("build");
    expect(html).toContain("workflow.run started");
    expect(html).toContain("version 1");
  });
});
