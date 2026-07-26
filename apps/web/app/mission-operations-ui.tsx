import type { OpsQueueJobItem } from "@boss/api-client";
import React from "react";

import type { ReceiptFeedItem } from "../lib/receipts";
import type { RoleFeedItem } from "../lib/roles";
import type { TaskFeedItem } from "../lib/tasks";

const TASK_BOARD_COLUMNS = [
  { id: "backlog", label: "Backlog" },
  { id: "in_progress", label: "In progress" },
  { id: "review", label: "Review" },
  { id: "blocked", label: "Blocked" },
  { id: "done", label: "Done" },
] as const;

type TaskBoardColumn = (typeof TASK_BOARD_COLUMNS)[number]["id"];
export type AgentOperationalState = "running" | "verified" | "blocked" | "idle";

export type AgentRosterRow = {
  id: string;
  name: string;
  role: string;
  trust: number;
  state: AgentOperationalState;
  currentTask: string;
};

function normalizedIdentifier(value?: string): string {
  return (value ?? "").trim().toLowerCase().replaceAll("-", "_");
}

export function taskBoardColumn(status: string): TaskBoardColumn {
  const value = status.toLowerCase();
  if (["done", "completed", "succeeded", "shipped"].includes(value)) return "done";
  if (["blocked", "failed", "deadletter", "deadlettered", "cancelled"].includes(value)) {
    return "blocked";
  }
  if (value.includes("review") || value.includes("decision") || value.includes("approval")) {
    return "review";
  }
  if (["running", "executing", "in_progress", "leased"].includes(value)) {
    return "in_progress";
  }
  return "backlog";
}

function stateForTask(task?: TaskFeedItem): AgentOperationalState {
  if (!task) return "idle";
  const column = taskBoardColumn(task.status);
  if (column === "blocked") return "blocked";
  if (column === "in_progress") return "running";
  if (column === "review" || column === "done") return "verified";
  return "idle";
}

function taskRank(task: TaskFeedItem): number {
  const ranks: Record<TaskBoardColumn, number> = {
    blocked: 0,
    in_progress: 1,
    review: 2,
    backlog: 3,
    done: 4,
  };
  return ranks[taskBoardColumn(task.status)];
}

function taskMatchesRole(task: TaskFeedItem, role: RoleFeedItem): boolean {
  const owner = normalizedIdentifier(task.ownerRoleId);
  return (
    Boolean(owner) &&
    [role.id, role.roleKey, role.displayName].some(
      (candidate) => normalizedIdentifier(candidate) === owner,
    )
  );
}

function currentTaskLabel(task?: TaskFeedItem): string {
  if (!task) return "No assigned work";
  const column = taskBoardColumn(task.status);
  if (column === "done") return `Last verified · ${task.title}`;
  if (column === "review") return `In review · ${task.title}`;
  if (column === "backlog") return `Queued · ${task.title}`;
  return task.title;
}

export function buildAgentRoster(
  roles: RoleFeedItem[],
  tasks: TaskFeedItem[],
): AgentRosterRow[] {
  return roles.map((role) => {
    const assignedTasks = tasks
      .filter((task) => taskMatchesRole(task, role))
      .sort((left, right) => {
        const rankDifference = taskRank(left) - taskRank(right);
        if (rankDifference !== 0) return rankDifference;
        return Date.parse(right.updatedAt) - Date.parse(left.updatedAt);
      });
    const currentTask = assignedTasks[0];

    return {
      id: role.id,
      name: role.displayName,
      role: role.roleKey.replaceAll("_", " "),
      trust: role.trustScore,
      state: stateForTask(currentTask),
      currentTask: currentTaskLabel(currentTask),
    };
  });
}

export function compactTaskId(taskId: string): string {
  const compact = taskId.replace(/^tsk[_-]?/i, "").replace(/[^a-z0-9]/gi, "");
  return `T-${(compact.slice(0, 6) || "UNKNOWN").toUpperCase()}`;
}

export function receiptCountForTask(
  taskId: string,
  jobs: OpsQueueJobItem[],
  receipts: ReceiptFeedItem[],
): number {
  const jobIds = new Set(
    jobs.filter((job) => job.task_id === taskId).map((job) => job.id),
  );
  return receipts.filter(
    (receipt) => receipt.createdByJobId && jobIds.has(receipt.createdByJobId),
  ).length;
}

function formatDateTime(value?: string): string {
  if (!value) return "Unknown time";
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function ownerInitial(ownerRoleId?: string): string {
  return (ownerRoleId?.trim().slice(0, 1) || "—").toUpperCase();
}

export function MissionAgentRoster({
  roles,
  tasks,
}: {
  roles: RoleFeedItem[];
  tasks: TaskFeedItem[];
}) {
  const roster = buildAgentRoster(roles, tasks);

  if (roster.length === 0) {
    return <p className="empty-state">No project roles are installed yet.</p>;
  }

  return (
    <div className="mission-agent-roster">
      <div className="mission-roster-header" aria-hidden>
        <span />
        <span>Agent</span>
        <span>Now</span>
        <span>State</span>
        <span>Trust</span>
      </div>
      {roster.map((agent) => (
        <article className="mission-agent-row" key={agent.id}>
          <span
            className={`mission-agent-avatar state-${agent.state}`}
            aria-label={`${agent.name} is ${agent.state}`}
          >
            {agent.name.slice(0, 2).toUpperCase()}
          </span>
          <span className="mission-agent-identity">
            <strong>{agent.name}</strong>
            <small>{agent.role}</small>
          </span>
          <span className="mission-agent-now">{agent.currentTask}</span>
          <span className={`status-text roster-state state-${agent.state}`}>
            {agent.state}
          </span>
          <span className="mission-agent-metric">
            <small>Trust</small>
            <strong>{Math.round(agent.trust)}%</strong>
          </span>
        </article>
      ))}
    </div>
  );
}

export function MissionTaskBoard({
  tasks,
  jobs,
  receipts,
}: {
  tasks: TaskFeedItem[];
  jobs: OpsQueueJobItem[];
  receipts: ReceiptFeedItem[];
}) {
  return (
    <div className="mission-task-board" aria-label="Task status board">
      {TASK_BOARD_COLUMNS.map((column) => {
        const columnTasks = tasks.filter((task) => taskBoardColumn(task.status) === column.id);
        return (
          <section className={`mission-task-column column-${column.id}`} key={column.id}>
            <header>
              <span className="mission-task-column-title">{column.label}</span>
              <b>{columnTasks.length}</b>
            </header>
            <div className="mission-task-list">
              {columnTasks.map((task) => {
                const receiptCount = receiptCountForTask(task.id, jobs, receipts);
                const state = taskBoardColumn(task.status);
                return (
                  <article className="mission-task-card" key={task.id}>
                    <span className="task-priority" title={task.id}>
                      {task.priority} · {compactTaskId(task.id)}
                    </span>
                    <strong>{task.title}</strong>
                    <footer className="mission-task-footer">
                      <span
                        className="mission-task-owner"
                        aria-label={`Owner ${task.ownerRoleId ?? "unassigned"}`}
                      >
                        {ownerInitial(task.ownerRoleId)}
                      </span>
                      <span>
                        {receiptCount} {receiptCount === 1 ? "receipt" : "receipts"}
                      </span>
                      <span className={`mission-task-state state-${state}`}>
                        {task.status.replaceAll("_", " ")}
                      </span>
                    </footer>
                    <time dateTime={task.updatedAt}>{formatDateTime(task.updatedAt)}</time>
                  </article>
                );
              })}
              {columnTasks.length === 0 ? <p>No tasks</p> : null}
            </div>
          </section>
        );
      })}
    </div>
  );
}
