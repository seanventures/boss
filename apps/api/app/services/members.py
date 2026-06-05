from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event


class ProjectMemberNotFoundError(RuntimeError):
    pass


class ProjectMemberPermissionError(RuntimeError):
    pass


class MemberDirectoryService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _policy_path(self) -> Path:
        return self.settings.specpack_root / "policies" / "default_safety.yaml"

    def _load_policy(self) -> dict[str, Any]:
        payload = yaml.safe_load(self._policy_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return payload

    def seed_default_members(
        self, connection: Connection, project_id: str, actor: str = "bootstrap"
    ) -> None:
        defaults = [
            {
                "member_id": "ceo",
                "display_name": "Founder / CEO",
                "member_type": "human",
                "role_keys": ["ceo"],
                "permissions": [
                    "command_submit",
                    "task_create",
                    "decision_resolve",
                    "pack_manage",
                    "module_manage",
                    "role_review",
                    "self_improvement_manage",
                    "audit_export",
                    "health_refresh",
                    "comms_manage",
                    "member_manage",
                    "budget_manage",
                    "entitlement_manage",
                    "enterprise_manage",
                    "infra_provision",
                    "providerops_manage",
                ],
            },
            {
                "member_id": "cto",
                "display_name": "Chief Technology Officer",
                "member_type": "human",
                "role_keys": ["cto"],
                "permissions": [
                    "decision_resolve",
                    "audit_export",
                    "health_refresh",
                    "infra_provision",
                    "providerops_manage",
                ],
            },
            {
                "member_id": "cao",
                "display_name": "Chief Architect",
                "member_type": "human",
                "role_keys": ["cao"],
                "permissions": ["decision_resolve", "audit_export", "health_refresh"],
            },
            {
                "member_id": "vp_agents",
                "display_name": "VP Agents",
                "member_type": "human",
                "role_keys": ["vp_agents"],
                "permissions": [
                    "role_review",
                    "module_manage",
                    "self_improvement_manage",
                    "audit_export",
                ],
            },
            {
                "member_id": "release_manager",
                "display_name": "Release Manager",
                "member_type": "human",
                "role_keys": ["release_manager"],
                "permissions": [
                    "decision_resolve",
                    "self_improvement_manage",
                    "comms_manage",
                    "health_refresh",
                    "infra_provision",
                ],
            },
            {
                "member_id": "metatron",
                "display_name": "Metatron Operator",
                "member_type": "system",
                "role_keys": ["metatron"],
                "permissions": ["command_submit", "health_refresh", "audit_export"],
            },
            {
                "member_id": "executive_console",
                "display_name": "Executive Console Operator",
                "member_type": "system",
                "role_keys": ["ceo", "ceo_delegate"],
                "permissions": [
                    "command_submit",
                    "task_create",
                    "decision_resolve",
                    "pack_manage",
                    "module_manage",
                    "role_review",
                    "self_improvement_manage",
                    "audit_export",
                    "health_refresh",
                    "comms_manage",
                    "budget_manage",
                    "entitlement_manage",
                    "enterprise_manage",
                    "infra_provision",
                    "providerops_manage",
                ],
            },
            {
                "member_id": "system",
                "display_name": "BOSS System Runtime",
                "member_type": "system",
                "role_keys": ["system"],
                "permissions": [
                    "command_submit",
                    "task_create",
                    "decision_resolve",
                    "pack_manage",
                    "module_manage",
                    "role_review",
                    "self_improvement_manage",
                    "audit_export",
                    "health_refresh",
                    "comms_manage",
                    "member_manage",
                    "budget_manage",
                    "entitlement_manage",
                    "enterprise_manage",
                    "infra_provision",
                    "providerops_manage",
                ],
            },
        ]

        for member in defaults:
            self.upsert_member(
                connection,
                project_id=project_id,
                member_id=member["member_id"],
                display_name=member["display_name"],
                member_type=member["member_type"],
                role_keys=member["role_keys"],
                permissions=member["permissions"],
                actor=actor,
                emit_install_event=False,
            )

    def list_members(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        project_rows = connection.execute(
            text(
                """
                SELECT id, project_id, member_id, display_name, member_type, role_keys,
                       permissions, status, created_by, created_at, updated_at
                FROM project_members
                WHERE project_id = :project_id
                  AND status = 'active'
                ORDER BY member_type ASC, display_name ASC, member_id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        org_rows = connection.execute(
            text(
                """
                SELECT om.id, :project_id AS project_id, om.member_id,
                       om.display_name, om.member_type, om.role_keys,
                       om.permissions, om.status, om.created_by,
                       om.created_at, om.updated_at
                FROM org_members om
                JOIN workspaces w ON w.org_id = om.org_id
                JOIN projects p ON p.workspace_id = w.id
                WHERE p.id = :project_id
                  AND om.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM project_members pm
                    WHERE pm.project_id = :project_id
                      AND pm.member_id = om.member_id
                      AND pm.status = 'active'
                  )
                ORDER BY om.member_type ASC, om.display_name ASC, om.member_id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize_member(row) for row in [*project_rows, *org_rows]]

    def list_org_members(self, connection: Connection, org_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, member_id, display_name, member_type, role_keys,
                       permissions, status, created_by, created_at, updated_at
                FROM org_members
                WHERE org_id = :org_id
                ORDER BY member_type ASC, display_name ASC, member_id ASC
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
        return [self._serialize_org_member(row) for row in rows]

    def get_member(
        self,
        connection: Connection,
        project_id: str,
        member_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, member_id, display_name, member_type, role_keys,
                       permissions, status, created_by, created_at, updated_at
                FROM project_members
                WHERE project_id = :project_id AND member_id = :member_id
                {lock_clause}
                """
            ),
            {"project_id": project_id, "member_id": member_id},
        ).mappings().first()
        if not row or row["status"] != "active":
            raise ProjectMemberNotFoundError(
                f"Actor '{member_id}' is not an active member of project '{project_id}'."
            )
        return self._serialize_member(row)

    def get_org_member(
        self,
        connection: Connection,
        org_id: str,
        member_id: str,
        *,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, :project_id AS project_id, member_id,
                       display_name, member_type, role_keys, permissions,
                       status, created_by, created_at, updated_at
                FROM org_members
                WHERE org_id = :org_id AND member_id = :member_id
                """
            ),
            {"org_id": org_id, "member_id": member_id, "project_id": project_id},
        ).mappings().first()
        if not row or row["status"] != "active":
            raise ProjectMemberNotFoundError(
                f"Actor '{member_id}' is not an active member of org '{org_id}'."
            )
        payload = self._serialize_org_member(row)
        if project_id:
            payload["project_id"] = project_id
        return payload

    def upsert_member(
        self,
        connection: Connection,
        *,
        project_id: str,
        member_id: str,
        display_name: str,
        member_type: str,
        role_keys: list[str],
        permissions: list[str],
        actor: str,
        emit_install_event: bool = True,
    ) -> dict[str, Any]:
        normalized_member_id = member_id.strip()
        normalized_display_name = display_name.strip()
        normalized_type = member_type.strip() or "human"
        normalized_roles = self._normalize_tokens(role_keys)
        normalized_permissions = self._normalize_tokens(permissions)
        existing = connection.execute(
            text(
                """
                SELECT id, display_name, member_type, role_keys, permissions, status
                FROM project_members
                WHERE project_id = :project_id AND member_id = :member_id
                """
            ),
            {"project_id": project_id, "member_id": normalized_member_id},
        ).mappings().first()
        member_row_id = (
            existing["id"] if existing else f"pm_{project_id}_{normalized_member_id}"
        )
        install_status = "already_present"
        if (
            not existing
            or existing["display_name"] != normalized_display_name
            or existing["member_type"] != normalized_type
            or (existing["role_keys"] or []) != normalized_roles
            or (existing["permissions"] or []) != normalized_permissions
            or existing["status"] != "active"
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO project_members (
                        id, project_id, member_id, display_name, member_type, role_keys,
                        permissions, status, created_by
                    )
                    VALUES (
                        :id, :project_id, :member_id, :display_name, :member_type,
                        CAST(:role_keys AS JSONB), CAST(:permissions AS JSONB), 'active', :created_by
                    )
                    ON CONFLICT (project_id, member_id) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        member_type = EXCLUDED.member_type,
                        role_keys = EXCLUDED.role_keys,
                        permissions = EXCLUDED.permissions,
                        status = EXCLUDED.status,
                        created_by = EXCLUDED.created_by,
                        updated_at = now()
                    """
                ),
                {
                    "id": member_row_id,
                    "project_id": project_id,
                    "member_id": normalized_member_id,
                    "display_name": normalized_display_name,
                    "member_type": normalized_type,
                    "role_keys": json.dumps(normalized_roles),
                    "permissions": json.dumps(normalized_permissions),
                    "created_by": actor,
                },
            )
            install_status = "updated" if existing else "created"
            if emit_install_event:
                emit_event(
                    connection,
                    project_id,
                    "project_member.upserted",
                    actor,
                    "project_member",
                    member_row_id,
                    {
                        "member_id": normalized_member_id,
                        "member_type": normalized_type,
                        "role_keys": normalized_roles,
                        "permissions": normalized_permissions,
                    },
                )
        member = self.get_member(connection, project_id, normalized_member_id)
        member["upsert_status"] = install_status
        return member

    def upsert_org_member(
        self,
        connection: Connection,
        *,
        org_id: str,
        member_id: str,
        display_name: str,
        member_type: str,
        role_keys: list[str],
        permissions: list[str],
        actor: str,
    ) -> dict[str, Any]:
        normalized_member_id = member_id.strip()
        normalized_display_name = display_name.strip()
        normalized_type = member_type.strip() or "human"
        normalized_roles = self._normalize_tokens(role_keys)
        normalized_permissions = self._normalize_tokens(permissions)
        existing = connection.execute(
            text(
                """
                SELECT id, display_name, member_type, role_keys, permissions, status
                FROM org_members
                WHERE org_id = :org_id AND member_id = :member_id
                """
            ),
            {"org_id": org_id, "member_id": normalized_member_id},
        ).mappings().first()
        member_row_id = existing["id"] if existing else f"om_{org_id}_{normalized_member_id}"
        upsert_status = "already_present"
        if (
            not existing
            or existing["display_name"] != normalized_display_name
            or existing["member_type"] != normalized_type
            or (existing["role_keys"] or []) != normalized_roles
            or (existing["permissions"] or []) != normalized_permissions
            or existing["status"] != "active"
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO org_members (
                        id, org_id, member_id, display_name, member_type,
                        role_keys, permissions, status, created_by
                    )
                    VALUES (
                        :id, :org_id, :member_id, :display_name, :member_type,
                        CAST(:role_keys AS JSONB), CAST(:permissions AS JSONB),
                        'active', :created_by
                    )
                    ON CONFLICT (org_id, member_id) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        member_type = EXCLUDED.member_type,
                        role_keys = EXCLUDED.role_keys,
                        permissions = EXCLUDED.permissions,
                        status = EXCLUDED.status,
                        created_by = EXCLUDED.created_by,
                        updated_at = now()
                    """
                ),
                {
                    "id": member_row_id,
                    "org_id": org_id,
                    "member_id": normalized_member_id,
                    "display_name": normalized_display_name,
                    "member_type": normalized_type,
                    "role_keys": json.dumps(normalized_roles),
                    "permissions": json.dumps(normalized_permissions),
                    "created_by": actor,
                },
            )
            upsert_status = "updated" if existing else "created"
            emit_event(
                connection,
                self._first_project_for_org(connection, org_id),
                "org_member.upserted",
                actor,
                "org_member",
                member_row_id,
                {
                    "org_id": org_id,
                    "member_id": normalized_member_id,
                    "member_type": normalized_type,
                    "role_keys": normalized_roles,
                    "permissions": normalized_permissions,
                },
            )
        member = self.get_org_member(connection, org_id, normalized_member_id)
        member["upsert_status"] = upsert_status
        return member

    def require_member(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
        *,
        permission: Optional[str] = None,
        role_keys_any: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        try:
            member = self.get_member(connection, project_id, actor)
        except ProjectMemberNotFoundError:
            member = self._get_org_member_for_project(connection, project_id, actor)
        if permission and permission not in (member.get("permissions") or []):
            grant = self._active_break_glass_grant(
                connection,
                project_id,
                actor,
                permission,
            )
            if not grant:
                raise ProjectMemberPermissionError(
                    f"Actor '{actor}' lacks permission '{permission}' for project '{project_id}'."
                )
            emit_event(
                connection,
                project_id,
                "break_glass.used",
                actor,
                "break_glass_grant",
                grant["id"],
                {"permission": permission},
            )
        if role_keys_any:
            member_roles = set(member.get("role_keys") or [])
            if not member_roles.intersection(set(role_keys_any)):
                raise ProjectMemberPermissionError(
                    f"Actor '{actor}' does not hold one of the required roles: {', '.join(role_keys_any)}."
                )
        return member

    def org_id_for_project(self, connection: Connection, project_id: str) -> str:
        org_id = connection.execute(
            text(
                """
                SELECT o.id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).scalar_one_or_none()
        if not org_id:
            raise ProjectMemberNotFoundError(f"Project '{project_id}' was not found.")
        return str(org_id)

    def project_belongs_to_org(
        self,
        connection: Connection,
        *,
        project_id: str,
        org_id: str,
    ) -> bool:
        found = connection.execute(
            text(
                """
                SELECT 1
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id AND w.org_id = :org_id
                """
            ),
            {"project_id": project_id, "org_id": org_id},
        ).scalar_one_or_none()
        return bool(found)

    def _get_org_member_for_project(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
    ) -> dict[str, Any]:
        org_id = self.org_id_for_project(connection, project_id)
        return self.get_org_member(connection, org_id, actor, project_id=project_id)

    def _first_project_for_org(self, connection: Connection, org_id: str) -> str:
        project_id = connection.execute(
            text(
                """
                SELECT p.id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE w.org_id = :org_id
                ORDER BY p.created_at ASC, p.id ASC
                LIMIT 1
                """
            ),
            {"org_id": org_id},
        ).scalar_one_or_none()
        return str(project_id or org_id)

    def require_decision_actor(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
        *,
        action: str,
        risk_tier: int,
        delegate_to: Optional[str] = None,
        delegated_to: Optional[str] = None,
    ) -> dict[str, Any]:
        member = self.require_member(
            connection,
            project_id,
            actor,
            permission="decision_resolve",
        )
        if action == "delegate":
            if not delegate_to or not delegate_to.strip():
                raise ProjectMemberPermissionError(
                    "Delegation requires a valid target project member."
                )
            self.get_member(connection, project_id, delegate_to.strip())
        if action == "approve":
            required_roles = self._required_approver_roles(risk_tier)
            if required_roles:
                if delegated_to and delegated_to.strip() == actor:
                    return member
                member_roles = set(member.get("role_keys") or [])
                if not member_roles.intersection(required_roles):
                    raise ProjectMemberPermissionError(
                        f"Actor '{actor}' is not allowed to {action} a Tier-{risk_tier} decision."
                    )
        return member

    def _required_approver_roles(self, risk_tier: int) -> set[str]:
        policy = self._load_policy()
        rules = policy.get("rules") or {}
        approvals = (rules.get("approvals") or {}).get("risk_tier_requirements") or {}
        requirement = approvals.get(str(risk_tier)) or {}
        configured = requirement.get("approvals") or []
        return {item for item in configured if isinstance(item, str) and item}

    def _active_break_glass_grant(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
        permission: str,
    ) -> Optional[dict[str, Any]]:
        expired = connection.execute(
            text(
                """
                UPDATE break_glass_grants
                SET status = 'expired', updated_at = now()
                WHERE project_id = :project_id
                  AND target_member_id = :actor
                  AND status = 'active'
                  AND expires_at <= now()
                RETURNING id
                """
            ),
            {"project_id": project_id, "actor": actor},
        ).mappings().all()
        for row in expired:
            emit_event(
                connection,
                project_id,
                "break_glass.expired",
                "system",
                "break_glass_grant",
                row["id"],
                {"target_member_id": actor},
            )
        row = connection.execute(
            text(
                """
                SELECT id
                FROM break_glass_grants
                WHERE project_id = :project_id
                  AND target_member_id = :actor
                  AND status = 'active'
                  AND expires_at > now()
                  AND (
                    granted_permissions ? :permission
                    OR granted_permissions ? '*'
                  )
                ORDER BY expires_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "actor": actor, "permission": permission},
        ).mappings().first()
        return dict(row) if row else None

    def _normalize_tokens(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            token = value.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            cleaned.append(token)
        return cleaned

    def _serialize_member(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "member_id": row["member_id"],
            "display_name": row["display_name"],
            "member_type": row["member_type"],
            "role_keys": row.get("role_keys") or [],
            "permissions": row.get("permissions") or [],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _serialize_org_member(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "org_id": row["org_id"],
            "project_id": row.get("project_id"),
            "member_id": row["member_id"],
            "display_name": row["display_name"],
            "member_type": row["member_type"],
            "role_keys": row.get("role_keys") or [],
            "permissions": row.get("permissions") or [],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
