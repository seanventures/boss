from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc


class BreakGlassGrantNotFoundError(RuntimeError):
    pass


class BreakGlassService:
    def expire_grants(self, connection: Connection, project_id: str) -> None:
        rows = connection.execute(
            text(
                """
                UPDATE break_glass_grants
                SET status = 'expired', updated_at = now()
                WHERE project_id = :project_id
                  AND status = 'active'
                  AND expires_at <= now()
                RETURNING id, target_member_id
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        for row in rows:
            emit_event(
                connection,
                project_id,
                "break_glass.expired",
                "system",
                "break_glass_grant",
                row["id"],
                {"target_member_id": row["target_member_id"]},
            )

    def list_grants(
        self,
        connection: Connection,
        project_id: str,
        *,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        self.expire_grants(connection, project_id)
        status_clause = "" if include_expired else "AND status = 'active'"
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, target_member_id, requested_by, reason, scope,
                       granted_permissions, status, expires_at, postmortem_task_id,
                       revoked_by, revoked_reason, revoked_at, created_at, updated_at
                FROM break_glass_grants
                WHERE project_id = :project_id
                {status_clause}
                ORDER BY created_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize(row) for row in rows]

    def create_grant(
        self,
        connection: Connection,
        *,
        project_id: str,
        target_member_id: str,
        requested_by: str,
        reason: str,
        scope: dict[str, Any],
        granted_permissions: list[str],
        ttl_minutes: int,
    ) -> dict[str, Any]:
        grant_id = make_id("bg")
        expires_at = now_utc() + timedelta(minutes=ttl_minutes)
        permissions = self._normalize_permissions(granted_permissions)
        connection.execute(
            text(
                """
                INSERT INTO break_glass_grants (
                    id, project_id, target_member_id, requested_by, reason, scope,
                    granted_permissions, expires_at
                )
                VALUES (
                    :id, :project_id, :target_member_id, :requested_by, :reason,
                    CAST(:scope AS JSONB), CAST(:granted_permissions AS JSONB),
                    :expires_at
                )
                """
            ),
            {
                "id": grant_id,
                "project_id": project_id,
                "target_member_id": target_member_id,
                "requested_by": requested_by,
                "reason": reason,
                "scope": json.dumps(scope),
                "granted_permissions": json.dumps(permissions),
                "expires_at": expires_at,
            },
        )
        emit_event(
            connection,
            project_id,
            "break_glass.granted",
            requested_by,
            "break_glass_grant",
            grant_id,
            {
                "target_member_id": target_member_id,
                "granted_permissions": permissions,
                "expires_at": expires_at.isoformat(),
                "scope": scope,
            },
        )
        return self.get_grant(connection, grant_id)

    def attach_postmortem_task(
        self,
        connection: Connection,
        grant_id: str,
        postmortem_task_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                UPDATE break_glass_grants
                SET postmortem_task_id = :postmortem_task_id,
                    updated_at = now()
                WHERE id = :grant_id
                RETURNING project_id
                """
            ),
            {"grant_id": grant_id, "postmortem_task_id": postmortem_task_id},
        ).mappings().first()
        if not row:
            raise BreakGlassGrantNotFoundError(f"Break-glass grant '{grant_id}' not found.")
        emit_event(
            connection,
            row["project_id"],
            "break_glass.postmortem_task_created",
            "control_plane",
            "break_glass_grant",
            grant_id,
            {"postmortem_task_id": postmortem_task_id},
        )
        return self.get_grant(connection, grant_id)

    def revoke_grant(
        self,
        connection: Connection,
        grant_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        existing = self.get_grant(connection, grant_id)
        connection.execute(
            text(
                """
                UPDATE break_glass_grants
                SET status = 'revoked',
                    revoked_by = :actor,
                    revoked_reason = :reason,
                    revoked_at = now(),
                    updated_at = now()
                WHERE id = :grant_id
                """
            ),
            {"grant_id": grant_id, "actor": actor, "reason": reason},
        )
        emit_event(
            connection,
            existing["project_id"],
            "break_glass.revoked",
            actor,
            "break_glass_grant",
            grant_id,
            {"reason": reason, "target_member_id": existing["target_member_id"]},
        )
        return self.get_grant(connection, grant_id)

    def get_grant(self, connection: Connection, grant_id: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, target_member_id, requested_by, reason, scope,
                       granted_permissions, status, expires_at, postmortem_task_id,
                       revoked_by, revoked_reason, revoked_at, created_at, updated_at
                FROM break_glass_grants
                WHERE id = :grant_id
                """
            ),
            {"grant_id": grant_id},
        ).mappings().first()
        if not row:
            raise BreakGlassGrantNotFoundError(f"Break-glass grant '{grant_id}' not found.")
        return self._serialize(row)

    def _normalize_permissions(self, permissions: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for permission in permissions:
            token = permission.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            cleaned.append(token)
        if not cleaned:
            raise ValueError("granted_permissions must contain at least one permission.")
        return cleaned

    def _serialize(self, row: Any) -> dict[str, Any]:
        payload = dict(row)
        for key in ("expires_at", "revoked_at", "created_at", "updated_at"):
            if payload.get(key):
                payload[key] = payload[key].isoformat()
        payload["scope"] = payload.get("scope") or {}
        payload["granted_permissions"] = payload.get("granted_permissions") or []
        return payload
