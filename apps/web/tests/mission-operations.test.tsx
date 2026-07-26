import type { OpsQueueJobItem } from "@boss/api-client";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  buildAgentRoster,
  compactTaskId,
  MissionAgentRoster,
  MissionTaskBoard,
  receiptCountForTask,
  taskBoardColumn,
} from "../app/mission-operations-ui";
import type { ReceiptFeedItem } from "../lib/receipts";
import type { RoleFeedItem } from "../lib/roles";
import type { TaskFeedItem } from "../lib/tasks";

const roles: RoleFeedItem[] = [
  {
    id: "role_pm",
    roleKey: "pm",
    displayName: "Product Manager",
    autonomyLevel: 1,
    trustScore: 91,
    successfulRuns: 3,
    policyViolationCount: 0,
    duplicateEffectAttemptCount: 0,
    scopeViolationCount: 0,
    createdAt: "2026-07-26T00:00:00Z",
  },
  {
    id: "role_ops",
    roleKey: "provider_ops",
    displayName: "Provider Ops",
    autonomyLevel: 1,
    trustScore: 84,
    successfulRuns: 1,
    policyViolationCount: 0,
    duplicateEffectAttemptCount: 0,
    scopeViolationCount: 0,
    createdAt: "2026-07-26T00:00:00Z",
  },
];

const tasks: TaskFeedItem[] = [
  {
    id: "tsk_12345678",
    title: "Reconcile deployment receipts",
    status: "in_progress",
    ownerRoleId: "pm",
    dependencyTaskIds: [],
    priority: "high",
    riskTier: 1,
    updatedAt: "2026-07-26T02:00:00Z",
  },
  {
    id: "tsk_blocked",
    title: "Inspect provider health",
    status: "blocked",
    ownerRoleId: "role_ops",
    dependencyTaskIds: [],
    priority: "urgent",
    riskTier: 2,
    updatedAt: "2026-07-26T03:00:00Z",
  },
];

const jobs: OpsQueueJobItem[] = [
  {
    id: "job_1",
    project_id: "boss",
    task_id: "tsk_12345678",
    kind: "receipt.reconcile",
    status: "succeeded",
    priority: 1,
    attempt: 1,
    max_attempts: 3,
    data_boundary: "internal",
    run_after: "2026-07-26T00:00:00Z",
    last_error: {},
    created_at: "2026-07-26T00:00:00Z",
    updated_at: "2026-07-26T01:00:00Z",
  },
];

const receipts: ReceiptFeedItem[] = [
  {
    id: "rcpt_1",
    dedupeKey: "receipt-1",
    effectKind: "receipt.reconcile",
    status: "succeeded",
    createdByJobId: "job_1",
    createdAt: "2026-07-26T01:00:00Z",
  },
  {
    id: "rcpt_unrelated",
    dedupeKey: "receipt-2",
    effectKind: "other",
    status: "succeeded",
    createdByJobId: "job_other",
    createdAt: "2026-07-26T01:00:00Z",
  },
];

describe("mission operations views", () => {
  it("derives truthful roster state from assigned canonical tasks", () => {
    const roster = buildAgentRoster(roles, tasks);

    expect(roster[0]).toMatchObject({
      name: "Product Manager",
      state: "running",
      currentTask: "Reconcile deployment receipts",
    });
    expect(roster[1]).toMatchObject({
      name: "Provider Ops",
      state: "blocked",
      currentTask: "Inspect provider health",
    });
  });

  it("maps task states and receipts without inventing evidence", () => {
    expect(taskBoardColumn("pending_approval")).toBe("review");
    expect(taskBoardColumn("deadlettered")).toBe("blocked");
    expect(compactTaskId(tasks[0].id)).toBe("T-123456");
    expect(receiptCountForTask(tasks[0].id, jobs, receipts)).toBe(1);
    expect(receiptCountForTask(tasks[1].id, jobs, receipts)).toBe(0);
  });

  it("renders roster state rings, current work, stable task ids, and receipt counts", () => {
    const rosterHtml = renderToStaticMarkup(
      <MissionAgentRoster roles={roles} tasks={tasks} />,
    );
    const boardHtml = renderToStaticMarkup(
      <MissionTaskBoard tasks={tasks} jobs={jobs} receipts={receipts} />,
    );

    expect(rosterHtml).toContain("mission-roster-header");
    expect(rosterHtml).toContain("state-running");
    expect(rosterHtml).toContain("state-blocked");
    expect(rosterHtml).toContain("Reconcile deployment receipts");
    expect(boardHtml).toContain("T-123456");
    expect(boardHtml).toContain("1 receipt");
    expect(boardHtml).toContain("0 receipts");
    expect(boardHtml).toContain("mission-task-owner");
  });
});
