from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id
from .jobs import JobLeaseService


class CommandIntakeService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)

    def create_task(
        self,
        connection: Connection,
        project_id: str,
        title: str,
        description: Optional[str],
        dedupe_key: str,
        priority: str = "p2",
        risk_tier: int = 1,
        inputs: Optional[dict] = None,
        required_artifacts: Optional[list[str]] = None,
    ) -> dict:
        task_id = make_id("tsk")
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id, priority,
                    risk_tier, inputs, required_artifacts, budget_allocation_id, dedupe_key
                )
                VALUES (
                    :id, :project_id, :title, :description, 'open', NULL, :priority,
                    :risk_tier, CAST(:inputs AS JSONB), CAST(:required_artifacts AS JSONB), NULL,
                    :dedupe_key
                )
                """
            ),
            {
                "id": task_id,
                "project_id": project_id,
                "title": title,
                "description": description,
                "priority": priority,
                "risk_tier": risk_tier,
                "inputs": json.dumps(inputs or {}),
                "required_artifacts": json.dumps(required_artifacts or []),
                "dedupe_key": dedupe_key,
            },
        )
        emit_event(
            connection,
            project_id,
            "task.created",
            "control_plane",
            "task",
            task_id,
            {"dedupe_key": dedupe_key, "priority": priority, "risk_tier": risk_tier},
        )
        return {"task_id": task_id}

    def propose_command(
        self,
        connection: Connection,
        project_id: str,
        command: str,
        constraints: Optional[dict] = None,
    ) -> dict:
        task = self.create_task(
            connection,
            project_id=project_id,
            title=f"CEO command: {command[:96]}",
            description=command,
            dedupe_key=make_id("cmd"),
            priority="p1",
            risk_tier=2,
            inputs={
                "command": command,
                "constraints": constraints or {},
            },
            required_artifacts=["plan_draft"],
        )
        task_id = task["task_id"]
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "command_brief",
            "CEO command intake",
            {
                "command": command,
                "constraints": constraints or {},
                "next_expected_artifact": "plan_draft",
            },
        )
        job = self.jobs.create_job(
            connection,
            project_id=project_id,
            task_id=task_id,
            kind="plan.command",
            priority=50,
            payload={
                "command": command,
                "constraints": constraints or {},
                "artifact_id": artifact_id,
            },
        )
        return {
            "task_id": task_id,
            "job_id": job["id"],
            "artifact_id": artifact_id,
            "status": "accepted",
        }
