from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import create_artifact, emit_event, make_id, normalize_data_boundary
from .jobs import JobLeaseService
from .specpack import SpecPackService


class CommandIntakeService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)
        self.specpack = SpecPackService(settings)
        self.budget = BudgetGovernor(settings)

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
        data_boundary: str = "internal",
        owner_role_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        dependency_task_ids: Optional[list[str]] = None,
        due_at: Optional[object] = None,
    ) -> dict:
        task_id = make_id("tsk")
        boundary = normalize_data_boundary(data_boundary)
        owner = self._normalize_optional_text(owner_role_id, "owner_role_id")
        parent = self._normalize_optional_text(parent_task_id, "parent_task_id")
        dependencies = self._normalize_dependency_ids(dependency_task_ids)
        due_at_iso = due_at.isoformat() if hasattr(due_at, "isoformat") else due_at

        if owner:
            self._require_role_reference(connection, project_id, owner)
        if parent:
            self._require_task_reference(connection, project_id, parent, "parent_task_id")
        for dependency_id in dependencies:
            self._require_task_reference(
                connection,
                project_id,
                dependency_id,
                "dependency_task_ids",
            )
        if parent and parent in dependencies:
            raise ValueError("parent_task_id cannot also be listed as a dependency.")

        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status, owner_role_id,
                    parent_task_id, dependency_task_ids, due_at, priority, risk_tier,
                    inputs, required_artifacts, budget_allocation_id, data_boundary,
                    dedupe_key
                )
                VALUES (
                    :id, :project_id, :title, :description, 'open', :owner_role_id,
                    :parent_task_id, CAST(:dependency_task_ids AS JSONB), :due_at,
                    :priority, :risk_tier, CAST(:inputs AS JSONB),
                    CAST(:required_artifacts AS JSONB), NULL, :data_boundary,
                    :dedupe_key
                )
                """
            ),
            {
                "id": task_id,
                "project_id": project_id,
                "title": title,
                "description": description,
                "owner_role_id": owner,
                "parent_task_id": parent,
                "dependency_task_ids": json.dumps(dependencies),
                "due_at": due_at,
                "priority": priority,
                "risk_tier": risk_tier,
                "inputs": json.dumps(inputs or {}),
                "required_artifacts": json.dumps(required_artifacts or []),
                "data_boundary": boundary,
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
            {
                "dedupe_key": dedupe_key,
                "priority": priority,
                "risk_tier": risk_tier,
                "data_boundary": boundary,
                "owner_role_id": owner,
                "parent_task_id": parent,
                "dependency_task_ids": dependencies,
                "due_at": due_at_iso,
            },
        )
        return {"task_id": task_id}

    def _normalize_optional_text(self, value: Optional[str], field_name: str) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string.")
        normalized = value.strip()
        return normalized or None

    def _normalize_dependency_ids(self, value: Optional[list[str]]) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("dependency_task_ids must be a list of task IDs.")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("dependency_task_ids must contain non-empty strings.")
            task_id = item.strip()
            if task_id not in normalized:
                normalized.append(task_id)
        return normalized

    def _require_task_reference(
        self,
        connection: Connection,
        project_id: str,
        task_id: str,
        field_name: str,
    ) -> None:
        exists = connection.execute(
            text(
                """
                SELECT 1
                FROM tasks
                WHERE project_id = :project_id AND id = :task_id
                """
            ),
            {"project_id": project_id, "task_id": task_id},
        ).first()
        if not exists:
            raise ValueError(f"{field_name} references an unknown task in this project.")

    def _require_role_reference(
        self,
        connection: Connection,
        project_id: str,
        owner_role_id: str,
    ) -> None:
        exists = connection.execute(
            text(
                """
                SELECT 1
                FROM roles
                WHERE project_id = :project_id
                  AND (id = :owner_role_id OR role_key = :owner_role_id)
                """
            ),
            {"project_id": project_id, "owner_role_id": owner_role_id},
        ).first()
        if not exists:
            raise ValueError("owner_role_id references an unknown role in this project.")

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
            data_boundary=str((constraints or {}).get("data_boundary") or "internal"),
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
        job = None
        # Prefer a real model-generated plan when a model is configured and
        # budget allows; otherwise fall back to a deterministic local plan.
        if self.settings.openai_api_key:
            reservation = self.budget.reserve(
                connection,
                project_id,
                "model",
                self.settings.plan_budget_usd_micros,
                "CEO command plan generation.",
                60,
            )
            if reservation.get("status") == "reserved":
                plan_prompt = (
                    f"You are planning a CEO directive for project {project_id}.\n\n"
                    f"Directive: {command}\n\n"
                    f"Constraints: {json.dumps(constraints or {})}\n\n"
                    "Produce a concrete, actionable implementation plan. Include: a one-line "
                    "objective; a numbered list of concrete execution steps (each with what to do "
                    "and how you will know it is done); key risks with mitigations; explicit "
                    "acceptance criteria; and the single decision that needs human approval. "
                    "Be specific to the directive and avoid generic boilerplate."
                )
                job = self.jobs.create_job(
                    connection,
                    project_id=project_id,
                    task_id=task_id,
                    kind="openai.plan",
                    priority=50,
                    payload={
                        "project_id": project_id,
                        "reservation_id": reservation["reservation_id"],
                        "dedupe_key": f"ceo-plan-{task_id}",
                        "prompt": plan_prompt,
                        "system_prompt": None,
                        "command": command,
                        "constraints": constraints or {},
                        "artifact_id": artifact_id,
                        "requires_reservation": True,
                    },
                )
                self.budget.attach_job(
                    connection, project_id, reservation["reservation_id"], job["id"]
                )

        if job is None:
            default_module = self.specpack.find_default_agent_module_for_job_kind(
                connection,
                project_id,
                "plan.command",
            )
            module_descriptor = None
            if isinstance(default_module, dict):
                role = default_module.get("role") or {}
                module_descriptor = {
                    "id": default_module.get("id"),
                    "version": default_module.get("version"),
                    "display_name": default_module.get("display_name"),
                    "role_key": role.get("role_key") or role.get("role_id"),
                    "prompt_bundle": default_module.get("prompt_bundle") or {},
                }
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
                    "agent_module": module_descriptor,
                },
            )
        return {
            "task_id": task_id,
            "job_id": job["id"],
            "artifact_id": artifact_id,
            "status": "accepted",
        }
