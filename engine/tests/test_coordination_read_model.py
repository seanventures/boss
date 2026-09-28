from datetime import datetime, timezone

from boss_engine.schemas import RoleSummary, TaskSummary
from boss_engine.services.coordination_read_model import build_coordination_snapshot


def _role(key: str = "release_manager") -> RoleSummary:
    return RoleSummary(
        id="role-1", project_id="project-1", role_key=key,
        display_name="Release Manager", autonomy_level=1, trust_score=90,
        created_at="2026-09-28T09:00:00Z",
    )


def _task(
    task_id: str, *, status: str = "open", derived_status: str | None = None,
    owner_role_id: str = "release_manager", updated_at: str = "2026-09-28T11:00:00Z",
) -> TaskSummary:
    return TaskSummary(
        id=task_id, project_id="project-1", title=f"Task {task_id}",
        status=status, derived_status=derived_status, owner_role_id=owner_role_id,
        priority="p1", risk_tier=1, dedupe_key=f"test:{task_id}",
        created_at="2026-09-28T09:00:00Z", updated_at=updated_at,
    )


def test_coordination_matches_console_assignment_and_recent_work_rules():
    # Stored-status rank selects the running task; lifecycle overrides its label.
    tasks = [
        _task("done", status="done", updated_at="2026-09-28T12:00:00Z"),
        _task("active", status="running", derived_status="review"),
    ]
    snapshot = build_coordination_snapshot(
        [_role()], tasks,
        [{"task_id": "active", "title": "Task active", "status": "running",
          "stage": "coding", "area": "code_tasks", "created_at": "2026-09-28T09:00:00Z",
          "active": True}],
        observed_at=datetime(2026, 9, 28, 12, 34, tzinfo=timezone.utc),
    ).model_dump(by_alias=True, exclude_none=True)

    assert snapshot["observedAt"] == "2026-09-28T12:34:00.000Z"
    assert snapshot["roster"] == [{
        "id": "role-1", "name": "Release Manager", "role": "release manager",
        "trust": 90, "state": "verified", "currentTask": "In review · Task active",
        "currentTaskId": "active", "currentTaskStatus": "review",
        "currentTaskOwner": "release_manager",
        "currentTaskUpdatedAt": "2026-09-28T11:00:00Z",
    }]
    assert snapshot["inFlight"] == [{
        "taskId": "active", "title": "Task active", "status": "review",
        "stage": "coding", "area": "code_tasks", "createdAt": "2026-09-28T09:00:00Z",
        "active": False, "owner": "release_manager",
        "updatedAt": "2026-09-28T11:00:00Z",
    }]


def test_coordination_keeps_unmatched_recent_work_and_idle_role():
    model = build_coordination_snapshot(
        [_role()], [],
        [{"task_id": "missing", "title": "Older work", "status": "running",
          "stage": "coding", "area": "code_tasks", "created_at": None,
          "active": True}],
    )
    snapshot = model.model_dump(by_alias=True, exclude_none=True)
    assert snapshot["roster"][0]["state"] == "idle"
    assert snapshot["roster"][0]["currentTask"] == "No assigned work"
    assert "currentTaskId" not in snapshot["roster"][0]
    assert snapshot["inFlight"][0]["active"] is True
    assert snapshot["inFlight"][0]["status"] == "running"
    assert model.model_dump(by_alias=True)["inFlight"][0]["createdAt"] is None
