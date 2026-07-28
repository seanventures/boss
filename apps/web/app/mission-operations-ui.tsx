"use client";

import type { OpsQueueJobItem } from "@boss/api-client";
import React from "react";

import { pushUrlParam } from "./url-nav";
import { AgentOrb } from "./agent-orb";

import type { ReceiptFeedItem } from "../lib/receipts";
import type { RoleFeedItem } from "../lib/roles";
import type { TaskFeedItem } from "../lib/tasks";

import {
  TASK_BOARD_COLUMNS,
  boardStatus,
  buildAgentRoster,
  taskBoardColumn,
} from "../lib/roster";
import type { AgentOperationalState, AgentRosterRow, TaskBoardColumn } from "../lib/roster";

export { boardStatus, buildAgentRoster, taskBoardColumn };
export type { AgentOperationalState, AgentRosterRow };

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
    <div className="ar-table">
      <div className="ar-head" aria-hidden>
        <span>Agent</span>
        <span>Now</span>
        <span>State</span>
      </div>
      {roster.map((agent) => (
        <article className="ar-row" key={agent.id}>
          <span className="ar-agent">
            <AgentOrb
              label={agent.name.slice(0, 2).toUpperCase()}
              state={agent.state}
              size={40}
              title={`${agent.name} is ${agent.state}`}
            />
            <span className="ar-identity">
              <span className="ar-name">{agent.name}</span>
              <span className="ar-sub">{agent.role} · trust {Math.round(agent.trust)}%</span>
            </span>
          </span>
          <span className="ar-now">{agent.currentTask}</span>
          <span className={`ar-state ${agent.state}`}>
            <span className="ar-state-dot" aria-hidden />
            {agent.state}
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
        const columnTasks = tasks.filter((task) => taskBoardColumn(boardStatus(task)) === column.id);
        return (
          <section className={`mission-task-column column-${column.id}`} key={column.id}>
            <header>
              <span className="mission-task-column-title">{column.label}</span>
              <b>{columnTasks.length}</b>
            </header>
            <div className="mission-task-list">
              {columnTasks.map((task) => {
                const receiptCount = receiptCountForTask(task.id, jobs, receipts);
                const state = taskBoardColumn(boardStatus(task));
                return (
                  <article
                    className="mission-task-card"
                    key={task.id}
                    role="link"
                    tabIndex={0}
                    style={{ cursor: "pointer" }}
                    aria-label={`Open task ${task.title}`}
                    onClick={() => {
                      pushUrlParam("task", task.id);
                      window.dispatchEvent(new PopStateEvent("popstate"));
                      document.querySelector(".wf-board")?.scrollIntoView({
                        behavior: "smooth", block: "start" });
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        pushUrlParam("task", task.id);
                        window.dispatchEvent(new PopStateEvent("popstate"));
                      }
                    }}
                  >
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
                        {boardStatus(task).replaceAll("_", " ")}
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
