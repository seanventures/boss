import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CoordinationPanel } from "../app/coordination-ui";
import { WorkBoard } from "../app/workflow-ui";
import { reconcileRecentWork } from "../lib/metrics";

(globalThis as Record<string, unknown>).React = React;

describe("live coordination", () => {
  it("shows an agent's actual task and a direct task link", () => {
    const html = renderToStaticMarkup(<CoordinationPanel
      projectId="boss-dogfood" orgId="org_boss"
      initial={{
        observedAt: "2026-09-24T12:34:00Z",
        roster: [{
          id: "role-1", name: "Reviewer", role: "reviewer", trust: 90,
          state: "running", currentTask: "Review release", currentTaskId: "tsk_123",
          currentTaskStatus: "in_progress", currentTaskOwner: "reviewer",
          currentTaskUpdatedAt: "2026-09-24T12:31:00Z",
        }],
        inFlight: [],
      }} />);
    expect(html).toContain("Reviewer");
    expect(html).toContain("Review release");
    expect(html).toContain("area=work&amp;task=tsk_123");
    expect(html).toContain("1 running");
    expect(html).toContain("12:34 UTC");
    expect(html).toContain("owned by reviewer");
    expect(html).toContain("updated 2026-09-24 12:31 UTC");
  });

  it("links in-flight work to its selected task trace", () => {
    const html = renderToStaticMarkup(<CoordinationPanel projectId="boss-dogfood"
      orgId="org_boss" initial={{ observedAt: "2026-09-24T12:34:00Z", roster: [],
        inFlight: [{ taskId: "tsk_456", title: "Ship service", status: "running",
          stage: "coding", area: "code_tasks", createdAt: "2026-09-24T11:00:00Z",
          active: true }] }} />);
    expect(html).toContain("area=work&amp;task=tsk_456");
    expect(html).toContain("coding, running");
    expect(html).toContain("started 2026-09-24 11:00 UTC");
  });

  it("does not offer a task link for an idle agent", () => {
    const html = renderToStaticMarkup(<CoordinationPanel projectId="boss-dogfood"
      initial={{ observedAt: "2026-09-24T12:34:00Z", inFlight: [], roster: [{
        id: "role-2", name: "Architect", role: "architect", trust: 90,
        state: "idle", currentTask: "No assigned work",
      }] }} />);
    expect(html).toContain("No assigned work");
    expect(html).not.toContain("Open task");
  });

  it("uses the canonical task lifecycle when the recent-work metric is stale", () => {
    const items = reconcileRecentWork([{
      taskId: "tsk_456", title: "Ship service", status: "running", stage: "discovery",
      area: "code_tasks", createdAt: "2026-09-24T11:00:00Z", active: true,
    }], [{
      id: "tsk_456", title: "Ship service", status: "open", derivedStatus: "done",
      ownerRoleId: "release_manager", dependencyTaskIds: [], priority: "p1",
      riskTier: 2, updatedAt: "2026-09-24T12:00:00Z",
    }]);
    expect(items[0]).toMatchObject({ status: "done", active: false,
      owner: "release_manager", updatedAt: "2026-09-24T12:00:00Z" });
  });

  it("shows an unavailable trace when a linked task is absent from the feed", () => {
    const html = renderToStaticMarkup(<WorkBoard
      tasks={[{ id: "tsk_other", title: "Another task", status: "open", priority: "p2",
        riskTier: 0, updatedAt: "2026-09-24T12:00:00Z" }]}
      jobs={[]} events={[]} artifacts={[]} decisions={[]} initialTaskId="tsk_missing"
    />);
    expect(html).toContain("Run trace");
    expect(html).toContain("Task unavailable");
    expect(html).toContain("tsk_missing is unavailable");
  });
});
