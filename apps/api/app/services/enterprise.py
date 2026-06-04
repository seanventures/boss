from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event
from .members import MemberDirectoryService


class EnterpriseService:
    DEPLOYMENT_MODES = {
        "enterprise_cloud",
        "self_hosted",
        "private_cloud",
        "air_gapped",
    }
    ACCOUNT_CONNECT_MODES = {"attached", "detached"}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.members = MemberDirectoryService(settings)

    def _policy_path(self) -> Path:
        return self.settings.specpack_root / "policies" / "enterprise_governance_policy.yaml"

    def _load_policy(self) -> dict[str, Any]:
        payload = yaml.safe_load(self._policy_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return payload

    def _project_org_id(self, connection: Connection, project_id: str) -> str:
        row = connection.execute(
            text(
                """
                SELECT w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        return row["org_id"] if row else self.settings.default_org_id

    def list_scim_groups(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, external_id, display_name,
                       role_keys, permissions, raw_group, status, created_by,
                       created_at, updated_at
                FROM enterprise_scim_groups
                WHERE project_id = :project_id
                ORDER BY display_name ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._scim_group_from_row(dict(row)) for row in rows]

    def upsert_scim_group(
        self,
        connection: Connection,
        project_id: str,
        *,
        display_name: str,
        actor: str,
        external_id: Optional[str] = None,
        role_keys: Optional[list[str]] = None,
        permissions: Optional[list[str]] = None,
        raw_group: Optional[dict[str, Any]] = None,
        status: str = "active",
    ) -> dict[str, Any]:
        org_id = self._project_org_id(connection, project_id)
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("SCIM group display_name must not be blank.")
        normalized_roles = self._normalize_tokens(role_keys or [])
        normalized_permissions = self._normalize_tokens(permissions or [])
        group_id = f"scimgroup_{project_id}_{self._id_token(normalized_name)}"
        connection.execute(
            text(
                """
                INSERT INTO enterprise_scim_groups (
                    id, org_id, project_id, external_id, display_name,
                    role_keys, permissions, raw_group, status, created_by
                )
                VALUES (
                    :id, :org_id, :project_id, :external_id, :display_name,
                    CAST(:role_keys AS JSONB), CAST(:permissions AS JSONB),
                    CAST(:raw_group AS JSONB), :status, :created_by
                )
                ON CONFLICT (project_id, display_name) DO UPDATE
                SET external_id = EXCLUDED.external_id,
                    role_keys = EXCLUDED.role_keys,
                    permissions = EXCLUDED.permissions,
                    raw_group = EXCLUDED.raw_group,
                    status = EXCLUDED.status,
                    created_by = EXCLUDED.created_by,
                    updated_at = now()
                """
            ),
            {
                "id": group_id,
                "org_id": org_id,
                "project_id": project_id,
                "external_id": external_id,
                "display_name": normalized_name,
                "role_keys": json.dumps(normalized_roles),
                "permissions": json.dumps(normalized_permissions),
                "raw_group": json.dumps(raw_group or {}),
                "status": status,
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.scim_group_upserted",
            actor,
            "enterprise_scim_group",
            group_id,
            {
                "display_name": normalized_name,
                "role_keys": normalized_roles,
                "permissions": normalized_permissions,
                "status": status,
            },
        )
        return self._get_scim_group_by_name(connection, project_id, normalized_name)

    def list_scim_users(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, external_id, user_name,
                       display_name, member_id, role_keys, permissions, groups,
                       raw_profile, status, created_by, created_at, updated_at
                FROM enterprise_scim_users
                WHERE project_id = :project_id
                ORDER BY user_name ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._scim_user_from_row(dict(row)) for row in rows]

    def upsert_scim_user(
        self,
        connection: Connection,
        project_id: str,
        *,
        user_name: str,
        display_name: str,
        actor: str,
        external_id: Optional[str] = None,
        active: bool = True,
        role_keys: Optional[list[str]] = None,
        permissions: Optional[list[str]] = None,
        groups: Optional[list[str]] = None,
        raw_profile: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        org_id = self._project_org_id(connection, project_id)
        normalized_user_name = user_name.strip().lower()
        normalized_display_name = display_name.strip()
        if not normalized_user_name:
            raise ValueError("SCIM user user_name must not be blank.")
        if not normalized_display_name:
            raise ValueError("SCIM user display_name must not be blank.")
        requested_groups = self._normalize_tokens(groups or [])
        group_controls = self._group_controls(connection, project_id, requested_groups)
        effective_roles = self._merge_tokens(role_keys or [], group_controls["role_keys"])
        effective_permissions = self._merge_tokens(
            permissions or [],
            group_controls["permissions"],
        )
        member_id = f"scim_{self._id_token(normalized_user_name)}"
        user_id = f"scimuser_{project_id}_{self._id_token(normalized_user_name)}"
        status = "active" if active else "inactive"
        connection.execute(
            text(
                """
                INSERT INTO enterprise_scim_users (
                    id, org_id, project_id, external_id, user_name, display_name,
                    member_id, role_keys, permissions, groups, raw_profile,
                    status, created_by
                )
                VALUES (
                    :id, :org_id, :project_id, :external_id, :user_name,
                    :display_name, :member_id, CAST(:role_keys AS JSONB),
                    CAST(:permissions AS JSONB), CAST(:groups AS JSONB),
                    CAST(:raw_profile AS JSONB), :status, :created_by
                )
                ON CONFLICT (project_id, user_name) DO UPDATE
                SET external_id = EXCLUDED.external_id,
                    display_name = EXCLUDED.display_name,
                    member_id = EXCLUDED.member_id,
                    role_keys = EXCLUDED.role_keys,
                    permissions = EXCLUDED.permissions,
                    groups = EXCLUDED.groups,
                    raw_profile = EXCLUDED.raw_profile,
                    status = EXCLUDED.status,
                    created_by = EXCLUDED.created_by,
                    updated_at = now()
                """
            ),
            {
                "id": user_id,
                "org_id": org_id,
                "project_id": project_id,
                "external_id": external_id,
                "user_name": normalized_user_name,
                "display_name": normalized_display_name,
                "member_id": member_id,
                "role_keys": json.dumps(effective_roles),
                "permissions": json.dumps(effective_permissions),
                "groups": json.dumps(requested_groups),
                "raw_profile": json.dumps(raw_profile or {}),
                "status": status,
                "created_by": actor,
            },
        )
        if active:
            self.members.upsert_member(
                connection,
                project_id=project_id,
                member_id=member_id,
                display_name=normalized_display_name,
                member_type="human",
                role_keys=effective_roles,
                permissions=effective_permissions,
                actor=actor,
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE project_members
                    SET status = 'inactive', updated_at = now()
                    WHERE project_id = :project_id AND member_id = :member_id
                    """
                ),
                {"project_id": project_id, "member_id": member_id},
            )
        emit_event(
            connection,
            project_id,
            "enterprise.scim_user_upserted",
            actor,
            "enterprise_scim_user",
            user_id,
            {
                "user_name": normalized_user_name,
                "member_id": member_id,
                "status": status,
                "groups": requested_groups,
                "role_keys": effective_roles,
                "permissions": effective_permissions,
                "mapped_to_project_member": active,
            },
        )
        return self._get_scim_user_by_name(connection, project_id, normalized_user_name)

    def list_clusters(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, deployment_mode, account_connect_mode,
                       worker_pool_strategy, ha_profile, data_residency,
                       identity_controls, governance_controls, audit_controls,
                       private_worker_pools, compliance_packs, support_tier,
                       support_sla, status, created_by, metadata, created_at, updated_at
                FROM enterprise_clusters
                WHERE project_id = :project_id
                ORDER BY updated_at DESC, id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._cluster_from_row(dict(row)) for row in rows]

    def upsert_cluster(
        self,
        connection: Connection,
        project_id: str,
        *,
        deployment_mode: str,
        account_connect_mode: str,
        actor: str,
        worker_pool_strategy: str = "managed_workers",
        ha_profile: str = "standard",
        data_residency: str = "us",
        identity_controls: Optional[dict[str, Any]] = None,
        governance_controls: Optional[dict[str, Any]] = None,
        audit_controls: Optional[dict[str, Any]] = None,
        private_worker_pools: Optional[list[dict[str, Any]]] = None,
        compliance_packs: Optional[list[str]] = None,
        support_tier: str = "standard",
        support_sla: Optional[dict[str, Any]] = None,
        status: str = "planned",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if deployment_mode not in self.DEPLOYMENT_MODES:
            raise ValueError(f"Unsupported enterprise deployment mode: {deployment_mode}")
        if account_connect_mode not in self.ACCOUNT_CONNECT_MODES:
            raise ValueError(
                f"Unsupported Account Connect mode for enterprise cluster: {account_connect_mode}"
            )
        org_id = self._project_org_id(connection, project_id)
        policy = self._load_policy().get("rules", {})
        normalized_identity = self._normalize_identity_controls(
            identity_controls or {},
            policy.get("identity") or {},
        )
        normalized_governance = self._normalize_governance_controls(
            governance_controls or {},
            policy,
        )
        normalized_audit = self._normalize_audit_controls(
            audit_controls or {},
            policy.get("audit") or {},
        )
        normalized_support = self._normalize_support_sla(
            support_sla or {},
            policy.get("support") or {},
            support_tier,
        )
        pools = private_worker_pools or []
        packs = compliance_packs or []
        blocking_gaps = self._blocking_gaps(
            deployment_mode=deployment_mode,
            account_connect_mode=account_connect_mode,
            identity_controls=normalized_identity,
            audit_controls=normalized_audit,
            private_worker_pools=pools,
            compliance_packs=packs,
        )
        effective_status = "blocked" if blocking_gaps else status
        cluster_id = f"entcluster_{project_id}_{deployment_mode}_{account_connect_mode}"
        cluster_metadata = {
            **(metadata or {}),
            "blocking_gaps": blocking_gaps,
            "policy_id": "enterprise_governance_policy",
        }
        connection.execute(
            text(
                """
                INSERT INTO enterprise_clusters (
                    id, org_id, project_id, deployment_mode, account_connect_mode,
                    worker_pool_strategy, ha_profile, data_residency,
                    identity_controls, governance_controls, audit_controls,
                    private_worker_pools, compliance_packs, support_tier,
                    support_sla, status, created_by, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :deployment_mode,
                    :account_connect_mode, :worker_pool_strategy, :ha_profile,
                    :data_residency, CAST(:identity_controls AS JSONB),
                    CAST(:governance_controls AS JSONB),
                    CAST(:audit_controls AS JSONB),
                    CAST(:private_worker_pools AS JSONB),
                    CAST(:compliance_packs AS JSONB), :support_tier,
                    CAST(:support_sla AS JSONB), :status, :created_by,
                    CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, deployment_mode, account_connect_mode) DO UPDATE
                SET worker_pool_strategy = EXCLUDED.worker_pool_strategy,
                    ha_profile = EXCLUDED.ha_profile,
                    data_residency = EXCLUDED.data_residency,
                    identity_controls = EXCLUDED.identity_controls,
                    governance_controls = EXCLUDED.governance_controls,
                    audit_controls = EXCLUDED.audit_controls,
                    private_worker_pools = EXCLUDED.private_worker_pools,
                    compliance_packs = EXCLUDED.compliance_packs,
                    support_tier = EXCLUDED.support_tier,
                    support_sla = EXCLUDED.support_sla,
                    status = EXCLUDED.status,
                    created_by = EXCLUDED.created_by,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": cluster_id,
                "org_id": org_id,
                "project_id": project_id,
                "deployment_mode": deployment_mode,
                "account_connect_mode": account_connect_mode,
                "worker_pool_strategy": worker_pool_strategy,
                "ha_profile": ha_profile,
                "data_residency": data_residency,
                "identity_controls": json.dumps(normalized_identity),
                "governance_controls": json.dumps(normalized_governance),
                "audit_controls": json.dumps(normalized_audit),
                "private_worker_pools": json.dumps(pools),
                "compliance_packs": json.dumps(packs),
                "support_tier": support_tier,
                "support_sla": json.dumps(normalized_support),
                "status": effective_status,
                "created_by": actor,
                "metadata": json.dumps(cluster_metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.cluster_upserted",
            actor,
            "enterprise_cluster",
            cluster_id,
            {
                "deployment_mode": deployment_mode,
                "account_connect_mode": account_connect_mode,
                "status": effective_status,
                "blocking_gaps": blocking_gaps,
            },
        )
        return self.get_cluster(connection, cluster_id, project_id)

    def get_cluster(
        self,
        connection: Connection,
        cluster_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, deployment_mode, account_connect_mode,
                       worker_pool_strategy, ha_profile, data_residency,
                       identity_controls, governance_controls, audit_controls,
                       private_worker_pools, compliance_packs, support_tier,
                       support_sla, status, created_by, metadata, created_at, updated_at
                FROM enterprise_clusters
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": cluster_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Enterprise cluster '{cluster_id}' was not found.")
        return self._cluster_from_row(dict(row))

    def _normalize_identity_controls(
        self,
        controls: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "sso_oidc_required": bool(policy.get("sso_oidc", True)),
            "sso_oidc_configured": bool(
                controls.get(
                    "sso_oidc_configured",
                    self.settings.oidc_enabled and self.settings.oidc_configured,
                )
            ),
            "saml_required": bool(policy.get("saml", True)),
            "saml_configured": bool(controls.get("saml_configured", False)),
            "scim_required": bool(policy.get("scim", True)),
            "scim_configured": bool(controls.get("scim_configured", False)),
            "rbac_enabled": bool(controls.get("rbac_enabled", policy.get("rbac", True))),
            "abac_enabled": bool(controls.get("abac_enabled", policy.get("abac", True))),
            "principal_binding_required": bool(
                controls.get("principal_binding_required", True)
            ),
        }

    def _normalize_governance_controls(
        self,
        controls: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "rbac_abac_policy_evaluation": bool(
                controls.get("rbac_abac_policy_evaluation", True)
            ),
            "connector_permission_governance": bool(
                controls.get("connector_permission_governance", True)
            ),
            "budget_governor_precedence": True,
            "agents_receive_raw_provider_secrets": False,
            "private_model_gateway": bool(
                controls.get(
                    "private_model_gateway",
                    (policy.get("infrastructure") or {}).get("private_model_gateway", True),
                )
            ),
        }

    def _normalize_audit_controls(
        self,
        controls: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "audit_exports_enabled": bool(
                controls.get("audit_exports_enabled", policy.get("exports_required", True))
            ),
            "retention_policy_required": bool(
                policy.get("retention_policy_required", True)
            ),
            "retention_days": int(controls.get("retention_days", 365)),
            "compliance_evidence_workflows": bool(
                controls.get(
                    "compliance_evidence_workflows",
                    policy.get("compliance_evidence_workflows", True),
                )
            ),
            "audit_export_mode": controls.get("audit_export_mode", "on_demand"),
        }

    def _normalize_support_sla(
        self,
        support_sla: dict[str, Any],
        policy: dict[str, Any],
        support_tier: str,
    ) -> dict[str, Any]:
        default_response_hours = 4 if support_tier in {"premium", "enterprise"} else 24
        return {
            "support_sla_required": bool(policy.get("support_sla", True)),
            "support_tier": support_tier,
            "response_time_hours": int(
                support_sla.get("response_time_hours", default_response_hours)
            ),
            "uptime_target": support_sla.get("uptime_target", "99.9%"),
            "lts_builds": bool(support_sla.get("lts_builds", policy.get("lts_builds", True))),
        }

    def _blocking_gaps(
        self,
        *,
        deployment_mode: str,
        account_connect_mode: str,
        identity_controls: dict[str, Any],
        audit_controls: dict[str, Any],
        private_worker_pools: list[dict[str, Any]],
        compliance_packs: list[str],
    ) -> list[str]:
        gaps: list[str] = []
        if (
            identity_controls["sso_oidc_required"]
            and not identity_controls["sso_oidc_configured"]
            and not identity_controls["saml_configured"]
        ):
            gaps.append("sso_or_saml_not_configured")
        if identity_controls["scim_required"] and not identity_controls["scim_configured"]:
            gaps.append("scim_not_configured")
        if audit_controls["retention_policy_required"] and audit_controls["retention_days"] <= 0:
            gaps.append("retention_policy_missing")
        if audit_controls["audit_exports_enabled"] is False:
            gaps.append("audit_exports_disabled")
        if deployment_mode in {"self_hosted", "private_cloud", "air_gapped"}:
            if not private_worker_pools:
                gaps.append("private_worker_pool_missing")
            if account_connect_mode == "detached" and not compliance_packs:
                gaps.append("detached_enterprise_compliance_pack_missing")
        return gaps

    def _cluster_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "blocking_gaps": (row.get("metadata") or {}).get("blocking_gaps", []),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _get_scim_group_by_name(
        self,
        connection: Connection,
        project_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, external_id, display_name,
                       role_keys, permissions, raw_group, status, created_by,
                       created_at, updated_at
                FROM enterprise_scim_groups
                WHERE project_id = :project_id AND display_name = :display_name
                """
            ),
            {"project_id": project_id, "display_name": display_name},
        ).mappings().first()
        if row is None:
            raise ValueError(f"SCIM group '{display_name}' was not found.")
        return self._scim_group_from_row(dict(row))

    def _get_scim_user_by_name(
        self,
        connection: Connection,
        project_id: str,
        user_name: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, external_id, user_name,
                       display_name, member_id, role_keys, permissions, groups,
                       raw_profile, status, created_by, created_at, updated_at
                FROM enterprise_scim_users
                WHERE project_id = :project_id AND user_name = :user_name
                """
            ),
            {"project_id": project_id, "user_name": user_name},
        ).mappings().first()
        if row is None:
            raise ValueError(f"SCIM user '{user_name}' was not found.")
        return self._scim_user_from_row(dict(row))

    def _group_controls(
        self,
        connection: Connection,
        project_id: str,
        groups: list[str],
    ) -> dict[str, list[str]]:
        if not groups:
            return {"role_keys": [], "permissions": []}
        rows = connection.execute(
            text(
                """
                SELECT id, external_id, display_name, role_keys, permissions
                FROM enterprise_scim_groups
                WHERE project_id = :project_id
                  AND status = 'active'
                  AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(CAST(:groups AS JSONB)) AS requested(value)
                    WHERE requested.value = id
                       OR requested.value = display_name
                       OR requested.value = external_id
                  )
                """
            ),
            {"project_id": project_id, "groups": json.dumps(groups)},
        ).mappings().all()
        role_keys: list[str] = []
        permissions: list[str] = []
        for row in rows:
            role_keys.extend(row["role_keys"] or [])
            permissions.extend(row["permissions"] or [])
        return {
            "role_keys": self._normalize_tokens(role_keys),
            "permissions": self._normalize_tokens(permissions),
        }

    def _normalize_tokens(self, values: list[str]) -> list[str]:
        return sorted({value.strip() for value in values if value and value.strip()})

    def _merge_tokens(self, primary: list[str], secondary: list[str]) -> list[str]:
        return self._normalize_tokens([*primary, *secondary])

    def _id_token(self, value: str) -> str:
        token = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
        return token or "unknown"

    def _scim_group_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _scim_user_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "active": row["status"] == "active",
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
