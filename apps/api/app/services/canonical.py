from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id


class CanonicalObjectService:
    def list_departments(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, department_key, name, mission, status,
                       created_by, created_at, updated_at
                FROM departments
                WHERE project_id = :project_id
                ORDER BY department_key ASC
                """
            ),
            {"project_id": project_id},
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

    def list_policy_records(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, policy_key, version, title, status, body,
                       created_by, created_at, updated_at
                FROM policy_records
                WHERE project_id = :project_id
                ORDER BY policy_key ASC, version DESC
                """
            ),
            {"project_id": project_id},
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

    def list_plans(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, title, objective, status, phases,
                       success_criteria, risks, artifact_id, created_by, created_at,
                       updated_at
                FROM plans
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                """
            ),
            {"project_id": project_id},
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
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, title, purpose, status, participants, agenda,
                       minutes_artifact_id, decisions, created_by, created_at,
                       updated_at
                FROM meetings
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                """
            ),
            {"project_id": project_id},
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

    def _serialize_temporal(self, row: Any) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at"):
            if payload.get(key):
                payload[key] = payload[key].isoformat()
        return payload
