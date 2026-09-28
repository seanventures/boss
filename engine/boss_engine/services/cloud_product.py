"""BOSS Cloud / Community product distinction and Easy Connect.

Community runs fully independently. Cloud/Enterprise add hosted trust and
managed capacity. Easy Connect lets a self-hosted Community install link to
BOSS Cloud without pasting long-lived secrets into the browser.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .commercial import AccountConnectLinkNotFoundError
from .core import emit_event, make_id
from .community_cloud import (
    CloudLinkRequiredError,
    CommunityCloudService,
    RETENTION_DAYS_MAX,
    RETENTION_DAYS_MIN,
    _utcnow,
)

__all__ = ["CloudLinkRequiredError", "CloudProductService"]


class CloudProductService(CommunityCloudService):
    """Paid hosted Cloud features layered over the Community client."""

    def managed_workers_status(
        self,
        connection: Connection,
        project_id: str,
        *,
        edition: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Honest managed-worker fabric status (planned; no fake capacity)."""
        edition = edition or self.commercial.edition_status(connection, project_id)
        linked = bool(edition.get("account_connect_org"))
        edition_id = edition["edition_id"]
        cloud_eligible = edition_id in {
            "cloud",
            "enterprise_cloud",
            "enterprise_self_hosted",
        } or linked
        local_enrollments = self.list_worker_enrollments(connection, project_id)
        return {
            "project_id": project_id,
            "status": "planned",
            "available_now": False,
            "cloud_eligible": cloud_eligible,
            "linked": linked,
            "local_runners": "Use Community local workers / worker pools (ProviderOps).",
            "local_enrollments": local_enrollments,
            "managed_capacity": {
                "regions": [],
                "pools": [],
                "message": (
                    "Elastic managed workers are specified for BOSS Cloud but not "
                    "provisioned yet. Catalog status remains planned — not fake-done."
                ),
            },
            "hybrid": {
                "status": "partial" if local_enrollments else "planned",
                "description": (
                    "Local worker nodes can enroll for hybrid attach after Cloud link; "
                    "elastic managed fleets remain planned."
                ),
                "local_enrollment_count": len(local_enrollments),
            },
            "enrollment": {
                # Local hybrid enroll is ready when linked; managed fleets are not.
                "ready": linked,
                "local_worker_enroll_available": linked,
                "managed_fleets_available": False,
                "blocked_reason": (
                    None
                    if linked
                    else "cloud_not_linked"
                ),
                "managed_blocked_reason": "managed_workers_not_shipped",
                "next_step": (
                    "Link to BOSS Cloud via Easy Connect, then POST /v1/cloud/workers/enroll "
                    "for local hybrid attach. Managed fleets remain planned."
                    if not linked
                    else (
                        "Cloud linked; enroll local workers via POST /v1/cloud/workers/enroll. "
                        "Managed fleets still planned — never claim available_now."
                    )
                ),
            },
        }

    # ------------------------------------------------------------------
    # Post-link Cloud surface (always mounted under /v1/cloud/*)
    # ------------------------------------------------------------------


    def sync_entitlements(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Sync hosted Account Connect entitlements into the local control plane."""
        self._require_cloud_link(connection, project_id)
        result = self.commercial.sync_account_connect_entitlements(
            connection,
            project_id,
            actor=actor,
            hosted_org_id=hosted_org_id,
        )
        emit_event(
            connection,
            project_id,
            "cloud.entitlements_synced",
            actor,
            "project",
            project_id,
            {
                "synced_entitlement_count": result.get("synced_entitlement_count"),
                "hosted_org_id": (result.get("edition") or {})
                .get("account_connect_org", {})
                .get("hosted_org_id")
                if isinstance(result.get("edition"), dict)
                else None,
            },
        )
        return {
            "project_id": result["project_id"],
            "synced_entitlement_count": result["synced_entitlement_count"],
            "edition": result["edition"],
            "linked": True,
            "blocked_reason": None,
        }

    def list_entitlements(
        self,
        connection: Connection,
        project_id: str,
        *,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """List effective entitlements (local edition + hosted when linked)."""
        edition = self.commercial.edition_status(connection, project_id)
        linked = bool(edition.get("account_connect_org"))
        items = self.commercial.list_license_entitlements(
            connection,
            project_id,
            include_inactive=include_inactive,
        )
        hosted = [
            item
            for item in items
            if str(item.get("source") or "").startswith("account_connect")
        ]
        local = [item for item in items if item not in hosted]
        return {
            "project_id": project_id,
            "linked": linked,
            "edition_id": edition["edition_id"],
            "items": items,
            "local_count": len(local),
            "hosted_count": len(hosted),
            "blocked_reason": None if linked else "cloud_not_linked",
            "message": (
                "Effective entitlements include local edition capabilities"
                + (" and hosted Account Connect grants." if linked else ".")
            ),
        }

    def create_team_invite(
        self,
        connection: Connection,
        project_id: str,
        *,
        email: str,
        actor: str,
        display_name: Optional[str] = None,
        role_keys: Optional[list[str]] = None,
        permissions: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create a hosted team invite via Account Connect when linked."""
        self._require_cloud_link(connection, project_id)
        invite = self.commercial.create_team_invite(
            connection,
            project_id,
            email=email,
            actor=actor,
            display_name=display_name,
            role_keys=role_keys,
            permissions=permissions,
            metadata={
                **(metadata or {}),
                "source": (metadata or {}).get("source") or "cloud_post_link",
            },
        )
        emit_event(
            connection,
            project_id,
            "cloud.team_invite_created",
            actor,
            "account_connect_team_invite",
            invite.get("id") or project_id,
            {
                "email": email,
                "status": invite.get("status"),
                "receipt_id": invite.get("receipt_id"),
            },
        )
        return invite

    def list_team_invites(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List local team invite records; requires Cloud link."""
        self._require_cloud_link(connection, project_id)
        items = self.commercial.list_team_invites(
            connection, project_id, limit=limit
        )
        return {
            "project_id": project_id,
            "linked": True,
            "items": items,
            "blocked_reason": None,
        }

    def list_marketplace(
        self,
        connection: Connection,
        project_id: str,
        *,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List hosted marketplace packs when linked; empty + blocked when not."""
        edition = self.commercial.edition_status(connection, project_id)
        linked = bool(edition.get("account_connect_org"))
        if not linked:
            return {
                "project_id": project_id,
                "linked": False,
                "items": [],
                "blocked_reason": "cloud_not_linked",
                "message": (
                    "Marketplace listings require a BOSS Cloud link. "
                    "Complete Easy Connect, then sync marketplace packs."
                ),
            }
        items = self.commercial.list_marketplace_pack_listings(
            connection,
            project_id,
            include_inactive=include_inactive,
            limit=limit,
        )
        return {
            "project_id": project_id,
            "linked": True,
            "items": items,
            "blocked_reason": None,
            "message": None,
        }

    def list_worker_enrollments(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, worker_node_id,
                       display_name, capabilities, status, receipt_id,
                       enrolled_by, metadata, created_at, updated_at
                FROM cloud_worker_enrollments
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._enrollment_from_row(dict(row)) for row in rows]

    def enroll_worker(
        self,
        connection: Connection,
        project_id: str,
        *,
        worker_node_id: str,
        actor: str,
        display_name: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Enroll a **local** worker node for hybrid attach (receipted).

        Does NOT provision or claim elastic managed fleets. ``available_now`` for
        managed capacity stays false.
        """
        node_id = (worker_node_id or "").strip()
        if not node_id:
            raise ValueError("worker_node_id is required.")
        active = self._require_cloud_link(connection, project_id)
        scope = self.commercial._project_scope(connection, project_id)
        caps = list(capabilities or [])
        meta = dict(metadata or {})
        meta.setdefault("source", "cloud_post_link")
        meta["kind"] = "local_hybrid"
        meta["managed_fleet"] = False
        enrollment_id = make_id("cwenroll")
        dedupe_key = f"cloud-worker-enroll:{project_id}:{node_id}"
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "cloud.worker_enroll",
            {
                "worker_node_id": node_id,
                "hosted_org_id": active["hosted_org_id"],
                "data_boundary": "internal",
            },
            {
                "operation": "local_worker_enroll",
                "worker_node_id": node_id,
                "display_name": display_name,
                "capabilities": caps,
                "data_boundary": "internal",
            },
        )
        if not created and receipt.get("status") == "succeeded":
            existing = connection.execute(
                text(
                    """
                    SELECT id, project_id, account_connect_org_id, worker_node_id,
                           display_name, capabilities, status, receipt_id,
                           enrolled_by, metadata, created_at, updated_at
                    FROM cloud_worker_enrollments
                    WHERE project_id = :project_id AND worker_node_id = :worker_node_id
                    """
                ),
                {"project_id": project_id, "worker_node_id": node_id},
            ).mappings().first()
            if existing:
                return {
                    **self._enrollment_from_row(dict(existing)),
                    "reused": True,
                    "managed_fleets_available": False,
                }

        connection.execute(
            text(
                """
                INSERT INTO cloud_worker_enrollments (
                    id, project_id, account_connect_org_id, worker_node_id,
                    display_name, capabilities, status, receipt_id,
                    enrolled_by, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :worker_node_id,
                    :display_name, CAST(:capabilities AS JSONB), 'enrolled',
                    :receipt_id, :enrolled_by, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, worker_node_id) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    capabilities = EXCLUDED.capabilities,
                    status = 'enrolled',
                    receipt_id = EXCLUDED.receipt_id,
                    enrolled_by = EXCLUDED.enrolled_by,
                    account_connect_org_id = EXCLUDED.account_connect_org_id,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": enrollment_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"],
                "worker_node_id": node_id,
                "display_name": display_name or node_id,
                "capabilities": json.dumps(caps),
                "receipt_id": receipt["id"],
                "enrolled_by": actor,
                "metadata": json.dumps(meta),
            },
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            f"local:{node_id}",
            {
                "enrollment_id": enrollment_id,
                "worker_node_id": node_id,
                "managed_fleet": False,
            },
        )
        emit_event(
            connection,
            project_id,
            "cloud.worker_enrolled",
            actor,
            "cloud_worker_enrollment",
            enrollment_id,
            {
                "worker_node_id": node_id,
                "receipt_id": receipt["id"],
                "managed_fleet": False,
            },
        )
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, worker_node_id,
                       display_name, capabilities, status, receipt_id,
                       enrolled_by, metadata, created_at, updated_at
                FROM cloud_worker_enrollments
                WHERE project_id = :project_id AND worker_node_id = :worker_node_id
                """
            ),
            {"project_id": project_id, "worker_node_id": node_id},
        ).mappings().first()
        enrollment = self._enrollment_from_row(dict(row)) if row else {
            "id": enrollment_id,
            "project_id": project_id,
            "worker_node_id": node_id,
            "status": "enrolled",
            "receipt_id": receipt["id"],
        }
        return {
            **enrollment,
            "reused": False,
            "managed_fleets_available": False,
            "message": (
                "Local worker enrolled for hybrid attach. Managed elastic fleets "
                "remain planned (available_now=false)."
            ),
        }

    @staticmethod
    def _enrollment_from_row(row: dict[str, Any]) -> dict[str, Any]:
        caps = row.get("capabilities") or []
        if not isinstance(caps, list):
            caps = []
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "account_connect_org_id": row.get("account_connect_org_id"),
            "worker_node_id": row["worker_node_id"],
            "display_name": row.get("display_name"),
            "capabilities": caps,
            "status": row["status"],
            "receipt_id": row.get("receipt_id"),
            "enrolled_by": row.get("enrolled_by"),
            "metadata": meta,
            "created_at": row["created_at"].isoformat()
            if row.get("created_at")
            else None,
            "updated_at": row["updated_at"].isoformat()
            if row.get("updated_at")
            else None,
            "kind": "local_hybrid",
            "managed_fleet": False,
        }


    def update_retention_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        retention_days: int,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Set Cloud-linked evidence retention_days (receipted into org metadata).

        Requires an active Account Connect / Cloud link. Range: 30–2555 days.
        """
        try:
            days = int(retention_days)
        except (TypeError, ValueError) as error:
            raise ValueError("retention_days must be an integer.") from error
        if days < RETENTION_DAYS_MIN or days > RETENTION_DAYS_MAX:
            raise ValueError(
                f"retention_days must be between {RETENTION_DAYS_MIN} and "
                f"{RETENTION_DAYS_MAX} inclusive."
            )
        reason_text = (reason or "").strip() or "operator_updated_retention"
        if len(reason_text) > 500:
            raise ValueError("reason must be 500 characters or fewer.")

        active = self.commercial.active_account_connect_org(connection, project_id)
        if not active:
            raise AccountConnectLinkNotFoundError(
                "Cloud evidence retention is configurable only when this project is "
                "linked to BOSS Cloud. Local receipts remain available without a link."
            )

        scope = self.commercial._project_scope(connection, project_id)
        dedupe_key = (
            f"cloud-retention:{project_id}:{active['hosted_org_id']}:{days}:"
            f"{reason_text[:80]}"
        )
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "cloud.evidence_retention",
            {
                "project_id": project_id,
                "hosted_org_id": active["hosted_org_id"],
                "operation": "set_retention_days",
            },
            {
                "retention_days": days,
                "reason": reason_text,
                "actor": actor,
                "data_boundary": "internal",
            },
        )

        now_iso = _utcnow().isoformat()
        retention_blob = {
            "retention_days": days,
            "reason": reason_text,
            "updated_by": actor,
            "updated_at": now_iso,
            "receipt_id": receipt["id"],
            "support_sla": "design_partner",
        }
        patch = {
            "evidence_retention": retention_blob,
            "retention_days": days,
            "retention_reason": reason_text,
        }
        connection.execute(
            text(
                """
                UPDATE account_connect_orgs
                SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id AND status = 'linked'
                """
            ),
            {
                "id": active["id"],
                "project_id": project_id,
                "metadata": json.dumps(patch),
            },
        )

        if created or receipt.get("status") == "in_progress":
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                f"retention:{active['hosted_org_id']}:{days}",
                {
                    "retention_days": days,
                    "reason": reason_text,
                    "tier": "cloud_linked",
                    "support_sla": "design_partner",
                },
            )

        emit_event(
            connection,
            project_id,
            "cloud.retention_updated",
            actor,
            "account_connect_org",
            active["id"],
            {
                "retention_days": days,
                "reason": reason_text,
                "receipt_id": receipt["id"],
                "hosted_org_id": active["hosted_org_id"],
                "support_sla": "design_partner",
            },
        )

        policy = self.get_retention_policy(connection, project_id)
        policy["receipt_id"] = receipt["id"]
        policy["updated"] = True
        return policy
