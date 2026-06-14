from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id, now_utc


class CanonicalObjectService:
    def list_departments(
        self, connection: Connection, project_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, department_key, name, mission, status,
                       created_by, created_at, updated_at
                FROM departments
                WHERE project_id = :project_id
                ORDER BY department_key ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_temporal(row) for row in rows]

    def create_department(
        self,
        connection: Connection,
        *,
        project_id: str,
        department_key: str,
        name: str,
        mission: str,
        actor: str,
    ) -> dict[str, Any]:
        department_id = make_id("dept")
        connection.execute(
            text(
                """
                INSERT INTO departments (
                    id, project_id, department_key, name, mission, created_by
                )
                VALUES (
                    :id, :project_id, :department_key, :name, :mission, :created_by
                )
                ON CONFLICT (project_id, department_key) DO UPDATE
                SET name = EXCLUDED.name,
                    mission = EXCLUDED.mission,
                    updated_at = now()
                """
            ),
            {
                "id": department_id,
                "project_id": project_id,
                "department_key": department_key,
                "name": name,
                "mission": mission,
                "created_by": actor,
            },
        )
        row = self._get_by_key(
            connection,
            "departments",
            project_id,
            "department_key",
            department_key,
        )
        emit_event(
            connection,
            project_id,
            "department.upserted",
            actor,
            "department",
            row["id"],
            {"department_key": department_key, "name": name},
        )
        return self._serialize_temporal(row)

    def list_kpis(
        self, connection: Connection, project_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, kpi_key, name, description, owner_role_id,
                       target_value, current_value, unit, status, created_by,
                       created_at, updated_at
                FROM kpis
                WHERE project_id = :project_id
                ORDER BY status ASC, kpi_key ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_kpi(row) for row in rows]

    def create_kpi(
        self,
        connection: Connection,
        *,
        project_id: str,
        kpi_key: str,
        name: str,
        description: Optional[str],
        target_value: float,
        current_value: float,
        unit: str,
        owner_role_id: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        kpi_key = self._require_slug(kpi_key, "kpi_key")
        unit = unit.strip() or "count"
        if target_value <= 0:
            raise ValueError("target_value must be greater than zero.")
        if current_value < 0:
            raise ValueError("current_value must be greater than or equal to zero.")
        if owner_role_id:
            self._require_project_role(connection, project_id, owner_role_id)
        kpi_id = make_id("kpi")
        connection.execute(
            text(
                """
                INSERT INTO kpis (
                    id, project_id, kpi_key, name, description, target_value,
                    current_value, unit, owner_role_id, created_by
                )
                VALUES (
                    :id, :project_id, :kpi_key, :name, :description, :target_value,
                    :current_value, :unit, :owner_role_id, :created_by
                )
                ON CONFLICT (project_id, kpi_key) DO UPDATE
                SET name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    target_value = EXCLUDED.target_value,
                    current_value = EXCLUDED.current_value,
                    unit = EXCLUDED.unit,
                    owner_role_id = EXCLUDED.owner_role_id,
                    updated_at = now()
                """
            ),
            {
                "id": kpi_id,
                "project_id": project_id,
                "kpi_key": kpi_key,
                "name": name.strip(),
                "description": description.strip() if description else None,
                "target_value": target_value,
                "current_value": current_value,
                "unit": unit,
                "owner_role_id": owner_role_id,
                "created_by": actor,
            },
        )
        row = self._get_by_key(connection, "kpis", project_id, "kpi_key", kpi_key)
        emit_event(
            connection,
            project_id,
            "kpi.upserted",
            actor,
            "kpi",
            row["id"],
            {
                "kpi_key": kpi_key,
                "name": name.strip(),
                "target_value": target_value,
                "current_value": current_value,
                "unit": unit,
                "owner_role_id": owner_role_id,
            },
        )
        return self._serialize_kpi(row)

    def list_policy_records(
        self, connection: Connection, project_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, policy_key, version, title, status, body,
                       created_by, created_at, updated_at
                FROM policy_records
                WHERE project_id = :project_id
                ORDER BY policy_key ASC, version DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_temporal(row) for row in rows]

    def create_policy_record(
        self,
        connection: Connection,
        *,
        project_id: str,
        policy_key: str,
        version: int,
        title: str,
        body: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if version < 1:
            raise ValueError("version must be greater than zero.")
        policy_id = make_id("pol")
        connection.execute(
            text(
                """
                INSERT INTO policy_records (
                    id, project_id, policy_key, version, title, body, created_by
                )
                VALUES (
                    :id, :project_id, :policy_key, :version, :title,
                    CAST(:body AS JSONB), :created_by
                )
                ON CONFLICT (project_id, policy_key, version) DO UPDATE
                SET title = EXCLUDED.title,
                    body = EXCLUDED.body,
                    updated_at = now()
                """
            ),
            {
                "id": policy_id,
                "project_id": project_id,
                "policy_key": policy_key,
                "version": version,
                "title": title,
                "body": json.dumps(body),
                "created_by": actor,
            },
        )
        row = connection.execute(
            text(
                """
                SELECT id, project_id, policy_key, version, title, status, body,
                       created_by, created_at, updated_at
                FROM policy_records
                WHERE project_id = :project_id
                  AND policy_key = :policy_key
                  AND version = :version
                """
            ),
            {"project_id": project_id, "policy_key": policy_key, "version": version},
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "policy.upserted",
            actor,
            "policy",
            row["id"],
            {"policy_key": policy_key, "version": version},
        )
        return self._serialize_temporal(row)

    def list_plans(self, connection: Connection, project_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, title, objective, status, phases,
                       success_criteria, risks, artifact_id, created_by, created_at,
                       updated_at
                FROM plans
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_temporal(row) for row in rows]

    def create_plan(
        self,
        connection: Connection,
        *,
        project_id: str,
        title: str,
        objective: str,
        actor: str,
        task_id: Optional[str] = None,
        phases: Optional[list[Any]] = None,
        success_criteria: Optional[list[Any]] = None,
        risks: Optional[list[Any]] = None,
        artifact_id: Optional[str] = None,
    ) -> dict[str, Any]:
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
                    CAST(:phases AS JSONB), CAST(:success_criteria AS JSONB),
                    CAST(:risks AS JSONB), :artifact_id, :created_by
                )
                """
            ),
            {
                "id": plan_id,
                "project_id": project_id,
                "task_id": task_id,
                "title": title,
                "objective": objective,
                "phases": json.dumps(phases or []),
                "success_criteria": json.dumps(success_criteria or []),
                "risks": json.dumps(risks or []),
                "artifact_id": artifact_id,
                "created_by": actor,
            },
        )
        row = self._get_by_id(connection, "plans", plan_id)
        emit_event(
            connection,
            project_id,
            "plan.created",
            actor,
            "plan",
            plan_id,
            {"task_id": task_id, "artifact_id": artifact_id},
        )
        return self._serialize_temporal(row)

    def list_meetings(
        self, connection: Connection, project_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, title, purpose, status, participants, agenda,
                       transcript_artifact_id, minutes_artifact_id, action_task_ids,
                       decisions, dissent_records, blocked_state, completed_at,
                       created_by, created_at, updated_at
                FROM meetings
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_temporal(row) for row in rows]

    def create_meeting(
        self,
        connection: Connection,
        *,
        project_id: str,
        title: str,
        purpose: str,
        actor: str,
        participants: Optional[list[str]] = None,
        agenda: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        meeting_id = make_id("mtg")
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    id, project_id, title, purpose, participants, agenda, created_by
                )
                VALUES (
                    :id, :project_id, :title, :purpose, CAST(:participants AS JSONB),
                    CAST(:agenda AS JSONB), :created_by
                )
                """
            ),
            {
                "id": meeting_id,
                "project_id": project_id,
                "title": title,
                "purpose": purpose,
                "participants": json.dumps(participants or []),
                "agenda": json.dumps(agenda or []),
                "created_by": actor,
            },
        )
        row = self._get_by_id(connection, "meetings", meeting_id)
        emit_event(
            connection,
            project_id,
            "meeting.created",
            actor,
            "meeting",
            meeting_id,
            {"participant_count": len(participants or [])},
        )
        return self._serialize_temporal(row)

    def run_meeting(
        self,
        connection: Connection,
        settings: Settings,
        *,
        meeting_id: str,
        actor: str,
        transcript: list[dict[str, Any]],
        minutes: dict[str, Any],
        action_items: Optional[list[dict[str, Any]]] = None,
        decision_items: Optional[list[dict[str, Any]]] = None,
        dissent_records: Optional[list[dict[str, Any]]] = None,
        blocked_state: Optional[dict[str, Any]] = None,
        outcome_status: str = "completed",
        consensus_reached: bool = True,
    ) -> dict[str, Any]:
        meeting = connection.execute(
            text(
                """
                SELECT *
                FROM meetings
                WHERE id = :meeting_id
                FOR UPDATE
                """
            ),
            {"meeting_id": meeting_id},
        ).mappings().first()
        if not meeting:
            raise ValueError("meeting not found")
        if meeting["status"] in {"completed", "blocked", "cancelled", "deadlettered"}:
            raise ValueError(f"meeting is already {meeting['status']}")
        normalized_status = self._normalize_meeting_outcome_status(outcome_status)
        normalized_transcript = self._normalize_transcript(transcript)
        normalized_minutes = self._normalize_minutes(minutes)
        normalized_actions = self._normalize_meeting_objects(action_items or [], "action_items")
        normalized_decisions = self._normalize_meeting_objects(
            decision_items or [], "decision_items"
        )
        normalized_dissent = self._normalize_meeting_objects(
            dissent_records or [], "dissent_records"
        )
        if normalized_status == "blocked" and not blocked_state:
            raise ValueError("blocked_state is required when outcome_status is blocked")
        if not consensus_reached and not normalized_dissent:
            raise ValueError("dissent_records are required when consensus_reached is false")

        project_id = str(meeting["project_id"])
        transcript_artifact_id = create_artifact(
            connection,
            settings,
            project_id,
            None,
            "meeting_transcript",
            f"{meeting['title']} transcript",
            {
                "meeting_id": meeting_id,
                "title": meeting["title"],
                "purpose": meeting["purpose"],
                "participants": meeting["participants"] or [],
                "agenda": meeting["agenda"] or [],
                "transcript": normalized_transcript,
                "consensus_reached": consensus_reached,
                "dissent_records": normalized_dissent,
            },
        )
        minutes_artifact_id = create_artifact(
            connection,
            settings,
            project_id,
            None,
            "meeting_minutes",
            f"{meeting['title']} minutes",
            {
                "meeting_id": meeting_id,
                "title": meeting["title"],
                "purpose": meeting["purpose"],
                "participants": meeting["participants"] or [],
                "agenda": meeting["agenda"] or [],
                "minutes": normalized_minutes,
                "action_items": normalized_actions,
                "decision_items": normalized_decisions,
                "blocked_state": blocked_state,
                "consensus_reached": consensus_reached,
                "dissent_records": normalized_dissent,
                "transcript_artifact_id": transcript_artifact_id,
            },
        )

        action_task_ids = [
            self._create_meeting_action_task(
                connection,
                project_id,
                meeting_id,
                index,
                item,
                actor,
                minutes_artifact_id,
            )
            for index, item in enumerate(normalized_actions, start=1)
        ]
        decisions = [
            self._create_meeting_decision(
                connection,
                project_id,
                meeting_id,
                index,
                item,
                actor,
                minutes_artifact_id,
            )
            for index, item in enumerate(normalized_decisions, start=1)
        ]
        completed_at = now_utc()
        connection.execute(
            text(
                """
                UPDATE meetings
                SET status = :status,
                    transcript_artifact_id = :transcript_artifact_id,
                    minutes_artifact_id = :minutes_artifact_id,
                    action_task_ids = CAST(:action_task_ids AS JSONB),
                    decisions = CAST(:decisions AS JSONB),
                    dissent_records = CAST(:dissent_records AS JSONB),
                    blocked_state = CAST(:blocked_state AS JSONB),
                    completed_at = :completed_at,
                    updated_at = now()
                WHERE id = :meeting_id
                """
            ),
            {
                "status": normalized_status,
                "transcript_artifact_id": transcript_artifact_id,
                "minutes_artifact_id": minutes_artifact_id,
                "action_task_ids": json.dumps(action_task_ids),
                "decisions": json.dumps(decisions),
                "dissent_records": json.dumps(normalized_dissent),
                "blocked_state": json.dumps(blocked_state) if blocked_state else None,
                "completed_at": completed_at,
                "meeting_id": meeting_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "meeting.completed" if normalized_status == "completed" else "meeting.blocked",
            actor,
            "meeting",
            meeting_id,
            {
                "status": normalized_status,
                "transcript_artifact_id": transcript_artifact_id,
                "minutes_artifact_id": minutes_artifact_id,
                "action_task_ids": action_task_ids,
                "decision_ids": [item["decision_id"] for item in decisions],
                "dissent_count": len(normalized_dissent),
                "blocked": normalized_status == "blocked",
            },
        )
        row = self._get_by_id(connection, "meetings", meeting_id)
        return self._serialize_temporal(row)

    def _create_meeting_action_task(
        self,
        connection: Connection,
        project_id: str,
        meeting_id: str,
        index: int,
        item: dict[str, Any],
        actor: str,
        minutes_artifact_id: str,
    ) -> str:
        title = self._required_text(item, "title", f"action_items[{index - 1}]")
        task_id = make_id("tsk")
        dedupe_key = f"meeting:{meeting_id}:action:{index}"
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
                    NULL, '[]'::jsonb, :due_at, :priority, :risk_tier,
                    CAST(:inputs AS JSONB), CAST(:required_artifacts AS JSONB),
                    NULL, :data_boundary, :dedupe_key
                )
                """
            ),
            {
                "id": task_id,
                "project_id": project_id,
                "title": title,
                "description": item.get("description") or item.get("summary"),
                "owner_role_id": item.get("owner_role_id"),
                "due_at": item.get("due_at"),
                "priority": item.get("priority") or "p2",
                "risk_tier": int(item.get("risk_tier", 1)),
                "inputs": json.dumps(
                    {
                        "meeting_id": meeting_id,
                        "minutes_artifact_id": minutes_artifact_id,
                        "action_item": item,
                    }
                ),
                "required_artifacts": json.dumps(["meeting_minutes"]),
                "data_boundary": item.get("data_boundary") or "internal",
                "dedupe_key": dedupe_key,
            },
        )
        emit_event(
            connection,
            project_id,
            "task.created",
            actor,
            "task",
            task_id,
            {
                "dedupe_key": dedupe_key,
                "meeting_id": meeting_id,
                "minutes_artifact_id": minutes_artifact_id,
                "source": "meeting.action_item",
            },
        )
        return task_id

    def _create_meeting_decision(
        self,
        connection: Connection,
        project_id: str,
        meeting_id: str,
        index: int,
        item: dict[str, Any],
        actor: str,
        minutes_artifact_id: str,
    ) -> dict[str, str]:
        question = self._required_text(item, "question", f"decision_items[{index - 1}]")
        decision_id = make_id("dec")
        recommendation = item.get("recommendation") or {}
        if isinstance(recommendation, str):
            recommendation = {"summary": recommendation}
        recommendation["meeting_id"] = meeting_id
        recommendation["minutes_artifact_id"] = minutes_artifact_id
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options,
                    risk_tier, cost_summary, deadline_at, default_action, status,
                    external_ref
                )
                VALUES (
                    :id, :project_id, :task_id, :question,
                    CAST(:recommendation AS JSONB), CAST(:options AS JSONB),
                    :risk_tier, CAST(:cost_summary AS JSONB), :deadline_at,
                    :default_action, 'pending', :external_ref
                )
                """
            ),
            {
                "id": decision_id,
                "project_id": project_id,
                "task_id": item.get("task_id"),
                "question": question,
                "recommendation": json.dumps(recommendation),
                "options": json.dumps(
                    item.get("options")
                    or [
                        {"action": "approve", "label": "Approve"},
                        {"action": "reject", "label": "Reject"},
                        {"action": "revise", "label": "Request revision"},
                    ]
                ),
                "risk_tier": int(item.get("risk_tier", 1)),
                "cost_summary": json.dumps(item.get("cost_summary") or {}),
                "deadline_at": item.get("deadline_at"),
                "default_action": item.get("default_action") or "hold",
                "external_ref": f"meeting:{meeting_id}:decision:{index}",
            },
        )
        emit_event(
            connection,
            project_id,
            "decision.created",
            actor,
            "decision",
            decision_id,
            {
                "meeting_id": meeting_id,
                "minutes_artifact_id": minutes_artifact_id,
                "risk_tier": int(item.get("risk_tier", 1)),
                "default_action": item.get("default_action") or "hold",
                "source": "meeting.decision_item",
            },
        )
        return {"decision_id": decision_id, "question": question}

    def _normalize_meeting_outcome_status(self, value: str) -> str:
        normalized = (value or "completed").strip().lower()
        if normalized not in {"completed", "blocked"}:
            raise ValueError("outcome_status must be completed or blocked")
        return normalized

    def _normalize_transcript(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValueError("transcript must contain at least one entry")
        normalized = self._normalize_meeting_objects(value, "transcript")
        for index, entry in enumerate(normalized):
            self._required_text(entry, "speaker", f"transcript[{index}]")
            self._required_text(entry, "message", f"transcript[{index}]")
        return normalized

    def _normalize_minutes(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValueError("minutes must be a non-empty object")
        if not (value.get("summary") or value.get("notes") or value.get("outcomes")):
            raise ValueError("minutes must include summary, notes, or outcomes")
        return value

    def _normalize_meeting_objects(
        self, value: list[dict[str, Any]], field_name: str
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{field_name}[{index}] must be an object")
            normalized.append(item)
        return normalized

    def _required_text(self, item: dict[str, Any], key: str, context: str) -> str:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{context}.{key} is required")
        return value.strip()

    def _get_by_id(self, connection: Connection, table: str, row_id: str) -> dict[str, Any]:
        row = connection.execute(
            text(f"SELECT * FROM {table} WHERE id = :id"),
            {"id": row_id},
        ).mappings().one()
        return dict(row)

    def _get_by_key(
        self,
        connection: Connection,
        table: str,
        project_id: str,
        key_column: str,
        key_value: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT *
                FROM {table}
                WHERE project_id = :project_id AND {key_column} = :key_value
                """
            ),
            {"project_id": project_id, "key_value": key_value},
        ).mappings().one()
        return dict(row)

    def _require_project_role(
        self,
        connection: Connection,
        project_id: str,
        role_id: str,
    ) -> None:
        exists = connection.execute(
            text(
                """
                SELECT 1
                FROM roles
                WHERE id = :role_id AND project_id = :project_id
                """
            ),
            {"role_id": role_id, "project_id": project_id},
        ).first()
        if not exists:
            raise ValueError("owner_role_id must reference a role in the project.")

    def _require_slug(self, value: str, field_name: str) -> str:
        cleaned = value.strip().lower().replace(" ", "_")
        if not cleaned:
            raise ValueError(f"{field_name} must not be blank.")
        if not all(
            character.isalnum() or character in {"_", "-"} for character in cleaned
        ):
            raise ValueError(
                f"{field_name} may only contain letters, numbers, hyphens, or underscores."
            )
        return cleaned

    def _serialize_kpi(self, row) -> dict[str, Any]:
        payload = self._serialize_temporal(row)
        for key in ("target_value", "current_value"):
            value = payload.get(key)
            if isinstance(value, Decimal):
                payload[key] = float(value)
        return payload

    def _serialize_temporal(self, row: Any) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "completed_at"):
            if payload.get(key):
                payload[key] = payload[key].isoformat()
        return payload
