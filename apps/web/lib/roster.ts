import type { RoleFeedItem } from "./roles";
import type { TaskFeedItem } from "./tasks";

/**
 * Pure roster/board derivation shared by the client operations UI and the
 * server-rendered Command Center (a "use client" module's functions cannot be
 * called from a server component, so this logic lives boundary-free here).
 */

export const TASK_BOARD_COLUMNS = [
  { id: "backlog", label: "Backlog" },
  { id: "in_progress", label: "In progress" },
  { id: "review", label: "Review" },
  { id: "blocked", label: "Blocked" },
  { id: "done", label: "Done" },
] as const;

export type TaskBoardColumn = (typeof TASK_BOARD_COLUMNS)[number]["id"];
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

export function boardStatus(task: { status: string; derivedStatus?: string }): string {
  // BOSS-281: the tasks table only ever holds open/archived — the ACTUAL lifecycle is
  // derived server-side from the task's latest job + pending decisions.
  return task.derivedStatus || task.status;
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
  const column = taskBoardColumn(boardStatus(task));
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
  const column = taskBoardColumn(boardStatus(task));
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
