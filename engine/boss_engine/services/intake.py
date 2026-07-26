from __future__ import annotations

import json
import re
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

    def dispatch_plan(
        self,
        connection: Connection,
        plan_id: str,
        *,
        project_id: str,
        actor: str = "program_manager",
    ) -> dict:
        """Turn a plan's structured phases into executable tasks: one child task per phase, parented
        to the plan's task and sequenced (each phase depends on the previous unless the phase
        declares its own `depends_on`). Bridges the plan primitive to the task/job engine.

        `project_id` is required and must match the plan's project — callers authorize against the
        project they claim; never load a plan by id alone (cross-project dispatch).
        """
        plan = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, title, status, phases
                FROM plans
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": plan_id, "project_id": project_id},
        ).mappings().first()
        if not plan:
            raise ValueError(f"plan {plan_id} not found")
        if plan["status"] == "dispatched":
            raise ValueError(f"plan {plan_id} already dispatched")
        phases = plan["phases"] if isinstance(plan["phases"], list) else json.loads(plan["phases"] or "[]")
        created: list[str] = []
        prev_task_id: Optional[str] = None
        for i, phase in enumerate(phases):
            phase = phase if isinstance(phase, dict) else {"title": str(phase)}
            title = phase.get("title") or phase.get("name") or f"{plan['title']} — phase {i + 1}"
            desc = phase.get("objective") or phase.get("summary") or phase.get("description") or ""
            # explicit deps (by 0-based phase index) or default to the previous phase (sequential)
            dep_idx = phase.get("depends_on")
            if isinstance(dep_idx, list) and dep_idx:
                deps = [created[d] for d in dep_idx if isinstance(d, int) and 0 <= d < len(created)]
            else:
                deps = [prev_task_id] if prev_task_id else []
            result = self.create_task(
                connection, project_id, title=title, description=desc,
                dedupe_key=f"plan:{plan_id}:phase:{i}",
                risk_tier=int(phase.get("risk_tier", 1)),
                parent_task_id=plan["task_id"], dependency_task_ids=deps,
            )
            created.append(result["task_id"])
            prev_task_id = result["task_id"]
        connection.execute(
            text("UPDATE plans SET status = 'dispatched', updated_at = now() WHERE id = :id"),
            {"id": plan_id},
        )
        emit_event(connection, project_id, "plan.dispatched", actor, "plan", plan_id,
                   {"phase_task_ids": created, "phase_count": len(created)})
        return {"plan_id": plan_id, "status": "dispatched", "phase_task_ids": created}

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
        # Persist a dispatchable plan projection immediately. The background
        # planning job can enrich the artifact, but the orchestration surface
        # needs a stable plan id and phase boundary before asking a human to
        # confirm fan-out. Creating the plan is non-actuating: child tasks are
        # still created only by dispatch_plan after explicit confirmation.
        phases = self._draft_phases(command, constraints or {})
        plan_id = make_id("plan")
        connection.execute(
            text(
                """
                INSERT INTO plans (
                    id, project_id, task_id, title, objective, phases,
                    success_criteria, risks, artifact_id, created_by
                )
                VALUES (
                    :id, :project_id, :task_id, :title, :objective,
                    CAST(:phases AS JSONB), CAST(:success AS JSONB),
                    CAST(:risks AS JSONB), :artifact_id, 'command_intake'
                )
                """
            ),
            {
                "id": plan_id,
                "project_id": project_id,
                "task_id": task_id,
                "title": f"Plan: {command[:120]}",
                "objective": command,
                "phases": json.dumps(phases),
                "success": json.dumps((constraints or {}).get("success_criteria") or []),
                "risks": json.dumps((constraints or {}).get("risks") or []),
                "artifact_id": artifact_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "plan.proposed",
            "command_intake",
            "plan",
            plan_id,
            {"task_id": task_id, "phase_count": len(phases)},
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
            "plan_id": plan_id,
            "phases": phases,
            "status": "accepted",
        }

    @staticmethod
    def _draft_phases(command: str, constraints: dict) -> list[dict]:
        supplied = constraints.get("phases")
        if isinstance(supplied, list) and supplied:
            normalized = []
            for index, phase in enumerate(supplied[:12]):
                item = phase if isinstance(phase, dict) else {"title": str(phase)}
                title = str(item.get("title") or item.get("name") or "").strip()
                if title:
                    normalized.append(
                        {
                            **item,
                            "title": title,
                            "depends_on": item.get("depends_on", [index - 1] if index else []),
                        }
                    )
            if normalized:
                return normalized

        # Only treat a directive as MULTI-phase when the user CLEARLY asked for a sequence: an
        # explicit numbered/bulleted list, or a strong "… and then …" / ";" separator. Splitting on
        # a bare "and"/"then" shredded ordinary prose — "add currency conversion … keep it clean AND
        # minimal" became 3 junk phases. A single coherent request now stays ONE phase (one task).
        text = command.strip()
        numbered = re.findall(r"(?m)^\s*(?:\d+[.)]|[-*])\s+(.+?)\s*$", text)
        if len(numbered) >= 2:
            clauses = [c.strip(" .,:-") for c in numbered if c.strip(" .,:-")][:8]
        else:
            clauses = [
                clause.strip(" .,:-")
                for clause in re.split(r"\s*(?:;|\band then\b)\s*", text, flags=re.IGNORECASE)
                if clause.strip(" .,:-")
            ][:8]
        if len(clauses) <= 1:
            clauses = [text or "Complete the requested outcome"]
        return [
            {
                "title": clause[0].upper() + clause[1:] if clause else f"Phase {index + 1}",
                "objective": clause,
                "depends_on": [index - 1] if index else [],
                "risk_tier": 1,
            }
            for index, clause in enumerate(clauses)
        ]
