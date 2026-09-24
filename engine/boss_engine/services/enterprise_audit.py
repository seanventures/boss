"""Enterprise audit controls kept outside the Community runtime."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .audit import AuditTrailService


class EnterpriseAuditTrailService(AuditTrailService):
    def _audit_governance(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, deployment_mode, account_connect_mode, audit_controls,
                       status, metadata, updated_at
                FROM enterprise_clusters
                WHERE project_id = :project_id
                ORDER BY
                  CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                  updated_at DESC,
                  id ASC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            governance = super()._audit_governance(connection, project_id)
            governance["edition_scope"] = "community_or_unconfigured_enterprise"
            return governance
        row = dict(row)
        controls = row.get("audit_controls") or {}
        metadata = row.get("metadata") or {}
        audit_exports_enabled = bool(controls.get("audit_exports_enabled", True))
        return {
            "edition_scope": "enterprise",
            "cluster_id": row["id"],
            "deployment_mode": row["deployment_mode"],
            "account_connect_mode": row["account_connect_mode"],
            "cluster_status": row["status"],
            "export_allowed": audit_exports_enabled,
            "audit_exports_enabled": audit_exports_enabled,
            "retention_policy_required": bool(
                controls.get("retention_policy_required", True)
            ),
            "retention_days": controls.get("retention_days"),
            "compliance_evidence_workflows": bool(
                controls.get("compliance_evidence_workflows", True)
            ),
            "audit_export_mode": controls.get("audit_export_mode", "on_demand"),
            "blocking_gaps": metadata.get("blocking_gaps", []),
        }
