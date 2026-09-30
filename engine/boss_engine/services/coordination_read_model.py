"""Project coordination snapshot shared by the HTTP API and the console.

The console used to derive this view in Next.js from roles, tasks, and recent
work. Keep the task-state and assignment rules here so API, TUI, and MCP see
the same current work that the operator sees.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from boss_engine.schemas import RoleSummary, TaskSummary


TaskColumn = Literal["backlog", "in_progress", "review", "blocked", "done"]
AgentState = Literal["running", "verified", "blocked", "idle"]


class CoordinationAgent(BaseModel):
    id: str
    name: str
    role: str
    trust: int
    state: AgentState
    current_task: str = Field(alias="currentTask")
    current_task_id: str | None = Field(default=None, alias="currentTaskId")
    current_task_status: str | None = Field(default=None, alias="currentTaskStatus")
    current_task_owner: str | None = Field(default=None, alias="currentTaskOwner")
    current_task_updated_at: str | None = Field(default=None, alias="currentTaskUpdatedAt")


class CoordinationWorkItem(BaseModel):
    task_id: str = Field(alias="taskId")
    title: str
    status: str
    stage: str
    area: str
    created_at: str | None = Field(alias="createdAt")
    active: bool
    owner: str | None = None
    updated_at: str | None = Field(default=None, alias="updatedAt")


class CoordinationSnapshot(BaseModel):
    roster: list[CoordinationAgent]
    in_flight: list[CoordinationWorkItem] = Field(alias="inFlight")
    observed_at: str = Field(alias="observedAt")


def _identifier(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_")


def _column(status: str) -> TaskColumn:
    value = status.lower()
    if value in {"done", "completed", "succeeded", "shipped"}:
        return "done"
    if value in {"blocked", "failed", "deadletter", "deadlettered", "cancelled"}:
        return "blocked"
    if any(word in value for word in ("review", "decision", "approval")):
        return "review"
    if value in {"running", "executing", "in_progress", "leased"}:
        return "in_progress"
    return "backlog"


def _status(task: TaskSummary) -> str:
    return task.derived_status or task.status


def _updated_at(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _task_for_role(role: RoleSummary, tasks: Sequence[TaskSummary]) -> TaskSummary | None:
    matches = [
        task for task in tasks
        if _identifier(task.owner_role_id)
        and _identifier(task.owner_role_id) in {
            _identifier(role.id), _identifier(role.role_key), _identifier(role.display_name)
        }
    ]
    rank = {"blocked": 0, "in_progress": 1, "review": 2, "backlog": 3, "done": 4}
    # Match the console's stable sort: rank uses stored status, then latest update.
    matches.sort(key=lambda task: (rank[_column(task.status)], -_updated_at(task.updated_at)))
    return matches[0] if matches else None


def _agent(role: RoleSummary, task: TaskSummary | None) -> CoordinationAgent:
    if task is None:
        state: AgentState = "idle"
        label = "No assigned work"
    else:
        status = _status(task)
        column = _column(status)
        state = (
            "blocked" if column == "blocked" else
            "running" if column == "in_progress" else
            "verified" if column in {"review", "done"} else "idle"
        )
        label = (
            f"Last verified · {task.title}" if column == "done" else
            f"In review · {task.title}" if column == "review" else
            f"Queued · {task.title}" if column == "backlog" else task.title
        )
    return CoordinationAgent(
        id=role.id,
        name=role.display_name,
        role=role.role_key.replace("_", " "),
        trust=role.trust_score,
        state=state,
        currentTask=label,
        currentTaskId=task.id if task else None,
        currentTaskStatus=_status(task) if task else None,
        currentTaskOwner=task.owner_role_id if task else None,
        currentTaskUpdatedAt=task.updated_at if task else None,
    )


def build_coordination_snapshot(
    roles: Sequence[RoleSummary],
    tasks: Sequence[TaskSummary],
    recent_work: Sequence[dict[str, Any]],
    *,
    observed_at: datetime | None = None,
) -> CoordinationSnapshot:
    """Match the Command Center's 50-task roster and six-item recent-work view."""
    tasks_by_id = {task.id: task for task in tasks}
    in_flight = []
    for item in recent_work:
        task_id = str(item.get("task_id") or "")
        task = tasks_by_id.get(task_id)
        status = _status(task) if task else str(item.get("status") or "")
        in_flight.append(CoordinationWorkItem(
            taskId=task_id,
            title=str(item.get("title") or ""),
            status=status,
            stage=str(item.get("stage") or ""),
            area=str(item.get("area") or "code_tasks"),
            createdAt=item.get("created_at"),
            active=_column(status) == "in_progress" if task else bool(item.get("active")),
            owner=task.owner_role_id if task else None,
            updatedAt=task.updated_at if task else None,
        ))
    now = observed_at or datetime.now(timezone.utc)
    return CoordinationSnapshot(
        roster=[_agent(role, _task_for_role(role, tasks)) for role in roles],
        inFlight=in_flight,
        observedAt=now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
