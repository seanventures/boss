"""BOSS Cloud / Community product distinction and Easy Connect.

Community runs fully independently. Cloud/Enterprise add hosted trust and
managed capacity. Easy Connect lets a self-hosted Community install link to
BOSS Cloud without pasting long-lived secrets into the browser.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .commercial import (
    AccountConnectAuthSessionNotFoundError,
    AccountConnectAuthSessionStateError,
    AccountConnectConfigurationError,
    AccountConnectLinkNotFoundError,
    AccountConnectSyncError,
    CommercialService,
)
from .core import emit_event, make_id
from .receipts import ReceiptService

# Cloud-linked evidence retention bounds (days). Marketing partial SLA surface.
RETENTION_DAYS_MIN = 30
RETENTION_DAYS_MAX = 2555
DEFAULT_CLOUD_RETENTION_DAYS = 365


class CloudLinkRequiredError(RuntimeError):
    """Raised when a post-link Cloud capability is invoked without an AC link."""

    def __init__(self, message: str, *, blocked_reason: str = "cloud_not_linked"):
        super().__init__(message)
        self.blocked_reason = blocked_reason


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Marketing comparison matrix (boss.sean.network #opensource / #cloud / #enterprise).
# status: shipped | partial | planned | community_only | cloud_only | enterprise_only
EDITION_COMPARISON: list[dict[str, Any]] = [
    {
        "capability": "Self-hosted workflow engine (CLI + MCP)",
        "community": "yes",
        "cloud": "yes (hosted + attach)",
        "enterprise": "yes",
        "status": "shipped",
    },
    {
        "capability": "BYOK models & infra keys (SecretRefs)",
        "community": "yes",
        "cloud": "yes (or managed)",
        "enterprise": "yes",
        "status": "shipped",
    },
    {
        "capability": "Budgets, decisions, receipts, leases",
        "community": "yes",
        "cloud": "yes",
        "enterprise": "yes",
        "status": "shipped",
    },
    {
        "capability": "Self-host Mission Control console",
        "community": "yes",
        "cloud": "hosted MC",
        "enterprise": "yes (self-host or hosted)",
        "status": "shipped",
    },
    {
        "capability": "Software Factory (governed PRs)",
        "community": "yes",
        "cloud": "yes",
        "enterprise": "yes",
        "status": "shipped",
    },
    {
        "capability": "ProviderOps kernel (your infra keys)",
        "community": "yes",
        "cloud": "yes (+ hosted)",
        "enterprise": "yes",
        "status": "shipped",
    },
    {
        "capability": "Hosted Account Connect & marketplace",
        "community": "opt-in Easy Connect",
        "cloud": "required",
        "enterprise": "optional / required by deploy mode",
        "status": "partial",
    },
    {
        "capability": "Team collaboration (invites, seats)",
        "community": "via Cloud link",
        "cloud": "yes",
        "enterprise": "yes",
        "status": "partial",
    },
    {
        "capability": "Managed workers & elastic fleets",
        "community": "no (local runners)",
        "cloud": "planned",
        "enterprise": "hybrid planned",
        "status": "planned",
    },
    {
        "capability": "Managed model & skill routing",
        "community": "local broker + BYOK",
        "cloud": "partial",
        "enterprise": "custom policies",
        "status": "partial",
    },
    {
        "capability": "SSO / SCIM",
        "community": "no",
        "cloud": "no",
        "enterprise": "partial (routes, fail-closed)",
        "status": "partial",
    },
    {
        "capability": "HA / clustering",
        "community": "ops DIY",
        "cloud": "managed later",
        "enterprise": "planned",
        "status": "planned",
    },
    {
        "capability": "Evidence retention SLAs & support",
        "community": "local receipts",
        "cloud": "partial",
        "enterprise": "partial + design partnership",
        "status": "partial",
    },
]


class CloudProductService:
    """Catalog + Easy Connect orchestration for Community ↔ Cloud."""

    # Marketing #cloud claims mapped to capability keys + honesty status.
    CLOUD_FEATURES: list[dict[str, Any]] = [
        {
            "key": "hosted_mission_control",
            "title": "Hosted Mission Control",
            "description": "Managed console for teams without operating the web tier.",
            "status": "partial",
            "editions": ["cloud", "enterprise_cloud"],
            "community_local": False,
        },
        {
            "key": "durable_control_plane",
            "title": "Durable task & decision state",
            "description": "Tasks, decisions, plans, and receipts survive restarts.",
            "status": "shipped",
            "editions": ["community", "cloud", "enterprise_cloud", "enterprise_self_hosted"],
            "community_local": True,
        },
        {
            "key": "managed_workers",
            "title": "Managed workers & elastic infra",
            "description": "Cloud-operated worker capacity and scale-out.",
            "status": "planned",
            "editions": ["cloud", "enterprise_cloud"],
            "community_local": False,
        },
        {
            "key": "team_collaboration",
            "title": "Team collaboration",
            "description": "Hosted team invites, seats, and org membership via Account Connect.",
            "status": "partial",
            "editions": ["cloud", "enterprise_cloud", "enterprise_self_hosted"],
            "community_local": False,
            "requires_account_connect": True,
        },
        {
            "key": "managed_model_gateway",
            "title": "Model & skill routing",
            "description": "Managed model routing and skill packs; Community uses local entitlement broker + BYOK.",
            "status": "partial",
            "editions": ["community", "cloud", "enterprise_cloud", "enterprise_self_hosted"],
            "community_local": True,
        },
        {
            "key": "managed_connector_gateway",
            "title": "Connectors & integrations",
            "description": "Marketplace packs and hosted connector authorizations when linked to Cloud.",
            "status": "partial",
            "editions": ["cloud", "enterprise_cloud"],
            "community_local": False,
            "requires_account_connect": True,
        },
        {
            "key": "approvals_policy",
            "title": "Approvals & policy management",
            "description": "Decision inbox, dual-control ChangeSets, and policy packs.",
            "status": "shipped",
            "editions": ["community", "cloud", "enterprise_cloud", "enterprise_self_hosted"],
            "community_local": True,
        },
        {
            "key": "evidence_retention_support",
            "title": "Evidence retention & support",
            "description": (
                "Local receipts always available; when Cloud-linked, configurable "
                "retention policy and design-partner support SLA metadata."
            ),
            "status": "partial",
            "editions": ["community", "cloud", "enterprise_cloud", "enterprise_self_hosted"],
            "community_local": True,
            # Local receipt surface is always available_now; Cloud SLA fields only when linked.
            "available_now": True,
        },
    ]

    ENTERPRISE_FEATURES: list[dict[str, Any]] = [
        {
            "key": "sso_oidc_saml",
            "title": "SSO (OIDC/SAML)",
            "status": "partial",
            "editions": ["enterprise_cloud", "enterprise_self_hosted"],
        },
        {
            "key": "scim",
            "title": "SCIM provisioning",
            "status": "partial",
            "editions": ["enterprise_cloud", "enterprise_self_hosted"],
        },
        {
            "key": "enterprise_budget_controls",
            "title": "Enterprise budget controls",
            "status": "partial",
            "editions": ["cloud", "enterprise_cloud", "enterprise_self_hosted"],
        },
        {
            "key": "ha_clustering",
            "title": "HA / clustering",
            "status": "planned",
            "editions": ["enterprise_cloud", "enterprise_self_hosted"],
        },
        {
            "key": "hybrid_workers",
            "title": "Hybrid local & cloud workers",
            "status": "planned",
            "editions": ["enterprise_cloud", "enterprise_self_hosted"],
        },
    ]

    def __init__(self, settings: Settings, commercial: CommercialService):
        self.settings = settings
        self.commercial = commercial
        self.receipts = ReceiptService()

    def catalog(self, connection: Connection, project_id: str) -> dict[str, Any]:
        edition = self.commercial.edition_status(connection, project_id)
        edition_id = edition["edition_id"]
        linked = bool(edition.get("account_connect_org"))
        features = []
        for item in self.CLOUD_FEATURES:
            available_local = bool(item.get("community_local"))
            available_now = (
                available_local
                or (
                    edition_id in (item.get("editions") or [])
                    and (
                        not item.get("requires_account_connect")
                        or linked
                        or edition_id in {"cloud", "enterprise_cloud"}
                        and linked
                    )
                )
            )
            # Community: local features always available_now when community_local
            if edition_id == "community" and available_local:
                available_now = True
            if edition_id == "community" and item.get("requires_account_connect"):
                available_now = linked
            # Explicit catalog overrides (e.g. evidence_retention_support local always on)
            if item.get("available_now") is True:
                available_now = True
            feature_row = {
                **item,
                "available_now": bool(available_now),
                "unlock": (
                    "included"
                    if available_now and available_local
                    else "connect_cloud"
                    if item.get("requires_account_connect") or not available_local
                    else "edition_upgrade"
                ),
            }
            if item.get("key") == "evidence_retention_support":
                policy = self.get_retention_policy(connection, project_id)
                feature_row["retention"] = {
                    "tier": policy.get("tier"),
                    "retention_days": policy.get("retention_days"),
                    "support_sla": policy.get("support_sla"),
                    "configurable": policy.get("configurable"),
                }
            features.append(feature_row)
        enterprise = [
            {
                **item,
                "available_now": edition_id in (item.get("editions") or []),
            }
            for item in self.ENTERPRISE_FEATURES
        ]
        return {
            "project_id": project_id,
            "edition_id": edition_id,
            "independence": {
                "community_runs_without_cloud": True,
                "account_connect_required_for_local_work": False,
                "easy_connect_available": True,
                "account_connect_url_configured": bool(
                    (self.settings.account_connect_url or "").strip()
                ),
            },
            "cloud_features": features,
            "enterprise_features": enterprise,
            "packaging": {
                "community": "Self-host engine + Mission Control; BYOK; full control-plane kernel.",
                "cloud": "Managed control plane; Account Connect required; elastic workers planned.",
                "enterprise": "SSO/SCIM, cluster/HA, policy packs, hybrid workers — design partnership.",
            },
        }

    def status(self, connection: Connection, project_id: str) -> dict[str, Any]:
        edition = self.commercial.edition_status(connection, project_id)
        ac_url = (self.settings.account_connect_url or "").strip()
        linked = bool(edition.get("account_connect_org"))
        independent = (
            edition["edition_id"] == "community"
            or not edition.get("account_connect_required")
            or linked
        )
        pending = self.list_pending_easy_connect(connection, project_id)
        return {
            "project_id": project_id,
            "edition": edition,
            "independent": independent,
            "linked": linked,
            "easy_connect": {
                "ready": bool(ac_url),
                "methods": ["browser", "device"],
                "blocked_reason": (
                    None
                    if ac_url
                    else "BOSS_ACCOUNT_CONNECT_URL is not configured on this control plane."
                ),
                "pending_sessions": pending,
            },
            "managed_workers": self.managed_workers_status(
                connection, project_id, edition=edition
            ),
            "linked_capabilities": self.capabilities(
                connection, project_id, edition=edition
            )["summary"],
            "message": (
                "This install runs independently. Connect to BOSS Cloud only when you want "
                "hosted team, marketplace, or managed capacity features."
                if edition["edition_id"] == "community"
                else edition.get("blocked_reason")
                or "Edition status active."
            ),
        }

    def comparison(self, connection: Connection, project_id: str) -> dict[str, Any]:
        """Community vs Cloud vs Enterprise packaging matrix (marketing parity)."""
        edition = self.commercial.edition_status(connection, project_id)
        return {
            "project_id": project_id,
            "edition_id": edition["edition_id"],
            "linked": bool(edition.get("account_connect_org")),
            "rows": list(EDITION_COMPARISON),
            "marketing_url": "https://boss.sean.network/#cloud",
            "principle": (
                "Community is a real product (not a trial). Cloud is the managed control "
                "plane. Enterprise adds identity, fleets, and design-partner wedges."
            ),
            "connect_path": {
                "from_community": "POST /v1/cloud/easy-connect",
                "methods": ["browser", "device"],
                "requires": "BOSS_ACCOUNT_CONNECT_URL on the control plane",
            },
        }

    def capabilities(
        self,
        connection: Connection,
        project_id: str,
        *,
        edition: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """What this project can do locally vs only after Cloud link."""
        edition = edition or self.commercial.edition_status(connection, project_id)
        linked = bool(edition.get("account_connect_org"))
        catalog = self.catalog(connection, project_id)
        local = [
            f
            for f in catalog["cloud_features"]
            if f.get("community_local") and f.get("available_now")
        ]
        cloud_locked = [
            f
            for f in catalog["cloud_features"]
            if not f.get("available_now")
            and (
                f.get("requires_account_connect")
                or not f.get("community_local")
            )
        ]
        cloud_unlocked = [
            f
            for f in catalog["cloud_features"]
            if f.get("available_now")
            and (
                f.get("requires_account_connect")
                or not f.get("community_local")
            )
        ]
        return {
            "project_id": project_id,
            "edition_id": edition["edition_id"],
            "linked": linked,
            "local_capabilities": [
                {"key": f["key"], "title": f["title"], "status": f["status"]}
                for f in local
            ],
            "cloud_unlocked": [
                {"key": f["key"], "title": f["title"], "status": f["status"]}
                for f in cloud_unlocked
            ],
            "cloud_locked": [
                {
                    "key": f["key"],
                    "title": f["title"],
                    "status": f["status"],
                    "unlock": f.get("unlock"),
                }
                for f in cloud_locked
            ],
            "enterprise_features": catalog["enterprise_features"],
            "summary": {
                "local_count": len(local),
                "cloud_unlocked_count": len(cloud_unlocked),
                "cloud_locked_count": len(cloud_locked),
                "independent_without_cloud": not edition.get(
                    "account_connect_required"
                ),
            },
        }

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

    def _require_cloud_link(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        active = self.commercial.active_account_connect_org(connection, project_id)
        if not active:
            raise CloudLinkRequiredError(
                (
                    f"Project '{project_id}' is not linked to BOSS Cloud. "
                    "Complete Easy Connect (POST /v1/cloud/easy-connect) first."
                ),
                blocked_reason="cloud_not_linked",
            )
        return active

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

    def get_retention_policy(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        """Project evidence retention policy (local receipts + optional Cloud SLA).

        Community default: local receipts retained indefinitely (or
        ``BOSS_RECEIPT_RETENTION_DAYS`` when set). When Cloud-linked, tier becomes
        ``cloud_linked`` with design-partner support SLA and configurable
        ``retention_days`` (org metadata or default 365).
        """
        edition = self.commercial.edition_status(connection, project_id)
        active = edition.get("account_connect_org")
        linked = bool(active)
        env_days = self.settings.receipt_retention_days
        env_configured = env_days is not None and int(env_days) > 0

        base: dict[str, Any] = {
            "project_id": project_id,
            "edition_id": edition["edition_id"],
            "linked": linked,
            "feature_key": "evidence_retention_support",
            "status": "partial",
            "local_receipts": {
                "available_now": True,
                "description": "Receipts and audit trails are stored on this control plane.",
            },
            "message": None,
        }

        if not linked:
            if env_configured:
                base.update(
                    {
                        "tier": "community_local",
                        "retention_days": int(env_days),
                        "retention_mode": "env_configured",
                        "indefinite": False,
                        "support_sla": None,
                        "configurable": False,
                        "source": "BOSS_RECEIPT_RETENTION_DAYS",
                        "message": (
                            f"Local receipts retained for {int(env_days)} days "
                            "(BOSS_RECEIPT_RETENTION_DAYS). Link to BOSS Cloud for "
                            "design-partner support SLA and operator-configurable retention."
                        ),
                    }
                )
            else:
                base.update(
                    {
                        "tier": "community_local",
                        "retention_days": None,
                        "retention_mode": "indefinite",
                        "indefinite": True,
                        "support_sla": None,
                        "configurable": False,
                        "source": "community_default",
                        "message": (
                            "Local receipts retained indefinitely on this control plane. "
                            "Link to BOSS Cloud for design-partner support SLA and "
                            "operator-configurable retention (30–2555 days)."
                        ),
                    }
                )
            return base

        metadata = active.get("metadata") if isinstance(active, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        retention_meta = metadata.get("evidence_retention")
        if not isinstance(retention_meta, dict):
            retention_meta = {}

        retention_days = retention_meta.get("retention_days")
        if retention_days is None:
            retention_days = metadata.get("retention_days")
        if retention_days is None:
            retention_days = DEFAULT_CLOUD_RETENTION_DAYS
        try:
            retention_days = int(retention_days)
        except (TypeError, ValueError):
            retention_days = DEFAULT_CLOUD_RETENTION_DAYS

        reason = retention_meta.get("reason") or metadata.get("retention_reason")
        updated_by = retention_meta.get("updated_by")
        updated_at = retention_meta.get("updated_at")
        source = (
            "account_connect_org.metadata"
            if retention_meta.get("retention_days") is not None
            or metadata.get("retention_days") is not None
            else "cloud_default"
        )

        base.update(
            {
                "tier": "cloud_linked",
                "retention_days": retention_days,
                "retention_mode": "cloud_policy",
                "indefinite": False,
                "support_sla": "design_partner",
                "configurable": True,
                "source": source,
                "hosted_org_id": active.get("hosted_org_id")
                if isinstance(active, dict)
                else None,
                "account_connect_org_id": active.get("id")
                if isinstance(active, dict)
                else None,
                "reason": reason,
                "updated_by": updated_by,
                "updated_at": updated_at,
                "bounds": {
                    "min_days": RETENTION_DAYS_MIN,
                    "max_days": RETENTION_DAYS_MAX,
                    "default_days": DEFAULT_CLOUD_RETENTION_DAYS,
                },
                "message": (
                    f"Cloud-linked evidence retention: {retention_days} days with "
                    "design_partner support SLA. PUT /v1/cloud/retention to change "
                    f"({RETENTION_DAYS_MIN}–{RETENTION_DAYS_MAX} days)."
                ),
            }
        )
        return base

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


    def list_pending_easy_connect(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, status, authorization_url, return_url, hosted_org_id,
                       metadata, created_at, expires_at
                FROM account_connect_auth_sessions
                WHERE project_id = :project_id
                  AND status IN ('pending', 'ready', 'authorized', 'blocked', 'blocked_configuration')
                  AND (
                    metadata->>'source' = 'cloud_easy_connect'
                    OR metadata->>'flow' IN ('device', 'browser')
                  )
                ORDER BY created_at DESC
                LIMIT 10
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        items: list[dict[str, Any]] = []
        for row in rows:
            metadata = row["metadata"] or {}
            if not isinstance(metadata, dict):
                metadata = {}
            items.append(
                {
                    "session_id": row["id"],
                    "status": row["status"],
                    "method": metadata.get("flow") or "browser",
                    "user_code": metadata.get("user_code"),
                    "verification_uri": metadata.get("verification_uri"),
                    "authorization_url": row["authorization_url"],
                    "expires_at": row["expires_at"].isoformat()
                    if row["expires_at"]
                    else None,
                    "message": metadata.get("last_error"),
                }
            )
        return items

    def unlink(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Detach Cloud/Account Connect link; Community remains independent."""
        edition_after = self.commercial.unlink_account_connect_org(
            connection,
            project_id,
            actor=actor,
            reason=reason or "cloud_easy_disconnect",
        )
        emit_event(
            connection,
            project_id,
            "cloud.unlinked",
            actor,
            "project",
            project_id,
            {
                "reason": reason or "cloud_easy_disconnect",
                "edition_id": edition_after.get("edition_id"),
            },
        )
        return {
            "project_id": project_id,
            "linked": False,
            "edition": edition_after,
            "message": (
                "Disconnected from BOSS Cloud. This control plane continues as Community "
                "with local BYOK and ProviderOps."
            ),
            "status": self.status(connection, project_id),
        }

    def _sync_entitlements_after_link(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        hosted_org_id: Optional[str],
        session_id: str,
        method: str,
    ) -> dict[str, Any]:
        """Pull hosted entitlements after a successful Cloud link.

        Sync failures must not reverse the link; they surface in response metadata
        so operators can retry via ``POST /v1/account-connect/sync``.
        """
        if not hosted_org_id:
            return {
                "entitlements_synced": False,
                "synced_entitlement_count": 0,
                "entitlements_sync_error": "missing_hosted_org_id",
            }
        try:
            sync_result = self.commercial.sync_account_connect_entitlements(
                connection,
                project_id,
                actor=actor,
                hosted_org_id=hosted_org_id,
            )
        except (
            AccountConnectConfigurationError,
            AccountConnectSyncError,
            AccountConnectLinkNotFoundError,
        ) as error:
            emit_event(
                connection,
                project_id,
                "cloud.entitlements_sync_failed",
                actor,
                "account_connect_org",
                hosted_org_id,
                {
                    "session_id": session_id,
                    "method": method,
                    "error": str(error)[:300],
                    "error_type": error.__class__.__name__,
                },
            )
            return {
                "entitlements_synced": False,
                "synced_entitlement_count": 0,
                "entitlements_sync_error": str(error)[:300],
            }
        count = int(sync_result.get("synced_entitlement_count") or 0)
        emit_event(
            connection,
            project_id,
            "cloud.entitlements_synced",
            actor,
            "account_connect_org",
            hosted_org_id,
            {
                "session_id": session_id,
                "method": method,
                "synced_entitlement_count": count,
                "hosted_org_id": hosted_org_id,
            },
        )
        return {
            "entitlements_synced": True,
            "synced_entitlement_count": count,
            "entitlements_sync_error": None,
            "sync_edition": sync_result.get("edition"),
        }

    def complete_easy_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        session_id: Optional[str] = None,
        code: Optional[str] = None,
        state: Optional[str] = None,
    ) -> dict[str, Any]:
        """Complete browser Easy Connect after hosted Account Connect callback.

        Accepts either local ``session_id`` or ``state`` (from the hosted redirect).
        On success, auto-syncs hosted entitlements (failures surface in metadata).
        """
        if session_id:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, status, provider_ref, state,
                           authorization_url, return_url, hosted_org_id, metadata,
                           created_at, updated_at, expires_at
                    FROM account_connect_auth_sessions
                    WHERE project_id = :project_id AND id = :session_id
                    """
                ),
                {"project_id": project_id, "session_id": session_id},
            ).mappings().first()
        elif state:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, status, provider_ref, state,
                           authorization_url, return_url, hosted_org_id, metadata,
                           created_at, updated_at, expires_at
                    FROM account_connect_auth_sessions
                    WHERE project_id = :project_id AND state = :state
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"project_id": project_id, "state": state},
            ).mappings().first()
        else:
            raise ValueError("session_id or state is required to complete Easy Connect.")
        if row is None:
            raise AccountConnectAuthSessionNotFoundError(
                "Cloud Easy Connect session not found."
            )
        session_id = row["id"]
        metadata = row["metadata"] or {}
        if not isinstance(metadata, dict):
            metadata = {}
        flow = metadata.get("flow") or "browser"
        if flow == "device":
            # Device completion is poll-driven
            return self.poll_easy_connect(
                connection,
                project_id=project_id,
                session_id=session_id,
                actor=actor,
            )
        if row["status"] == "completed" and row["hosted_org_id"]:
            edition = self.commercial.edition_status(connection, project_id)
            return {
                "session_id": session_id,
                "status": "completed",
                "method": "browser",
                "hosted_org_id": row["hosted_org_id"],
                "linked": bool(edition.get("account_connect_org")),
                "edition": edition,
                "account_connect_org": edition.get("account_connect_org"),
                "entitlements_synced": None,
                "synced_entitlement_count": None,
                "entitlements_sync_error": None,
            }
        # Browser sessions start as "ready" once hosted AC returns authorization_url;
        # device sessions use "pending" until the user_code is authorized.
        if row["status"] not in {"pending", "ready", "authorized"}:
            raise AccountConnectAuthSessionStateError(
                f"Easy Connect session is {row['status']}; cannot complete."
            )
        if not code or not state:
            raise ValueError("code and state are required to complete browser Easy Connect.")
        if state != row["state"]:
            raise AccountConnectAuthSessionStateError(
                "Easy Connect state mismatch; deny without linking."
            )
        # Prefer commercial complete path (receipted + hosted org link)
        completed_session = self.commercial.complete_auth_session(
            connection,
            session_id,
            project_id=project_id,
            actor=actor,
            code=code,
            state=state,
            metadata={"source": "cloud_easy_connect_browser"},
        )
        # Re-read edition after commercial complete (may be linked or failed)
        edition_after = self.commercial.edition_status(connection, project_id)
        session_status = completed_session.get("status") or "completed"
        hosted = (
            completed_session.get("hosted_org_id")
            or (edition_after.get("account_connect_org") or {}).get("hosted_org_id")
        )
        linked = bool(edition_after.get("account_connect_org")) and session_status == "completed"
        emit_event(
            connection,
            project_id,
            "cloud.easy_connect_completed",
            actor,
            "account_connect_org",
            hosted or session_id,
            {
                "session_id": session_id,
                "method": "browser",
                "linked": linked,
                "status": session_status,
            },
        )
        result: dict[str, Any] = {
            "session_id": session_id,
            "status": session_status,
            "method": "browser",
            "hosted_org_id": hosted,
            "linked": linked,
            "edition": edition_after,
            "account_connect_org": edition_after.get("account_connect_org"),
            "entitlements_synced": False,
            "synced_entitlement_count": 0,
            "entitlements_sync_error": None,
        }
        if linked and hosted:
            sync_meta = self._sync_entitlements_after_link(
                connection,
                project_id=project_id,
                actor=actor,
                hosted_org_id=str(hosted),
                session_id=session_id,
                method="browser",
            )
            result.update(
                {
                    "entitlements_synced": sync_meta["entitlements_synced"],
                    "synced_entitlement_count": sync_meta["synced_entitlement_count"],
                    "entitlements_sync_error": sync_meta.get("entitlements_sync_error"),
                }
            )
            if sync_meta.get("sync_edition"):
                result["edition"] = sync_meta["sync_edition"]
                result["account_connect_org"] = sync_meta["sync_edition"].get(
                    "account_connect_org"
                )
        elif not linked:
            # Surface commercial failure without raising so callers see status
            failure = (completed_session.get("metadata") or {}).get(
                "failure_reason"
            ) or (completed_session.get("metadata") or {}).get("blocked_reason")
            if failure:
                result["message"] = str(failure)
        return result

    def easy_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        method: str,
        actor: str,
        return_url: Optional[str] = None,
    ) -> dict[str, Any]:
        method_normalized = (method or "browser").strip().lower()
        if method_normalized not in {"browser", "device"}:
            raise ValueError("method must be browser or device.")
        scope = self.commercial._project_scope(connection, project_id)
        if method_normalized == "device":
            return self._start_device_connect(
                connection,
                project_id=project_id,
                org_id=scope["org_id"],
                workspace_id=scope["workspace_id"],
                actor=actor,
                return_url=return_url,
            )
        return self._start_browser_connect(
            connection,
            project_id=project_id,
            org_id=scope["org_id"],
            workspace_id=scope["workspace_id"],
            actor=actor,
            return_url=return_url,
        )

    def poll_easy_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        session_id: str,
        actor: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, status, provider_ref, state,
                       authorization_url, return_url, hosted_org_id, metadata,
                       created_at, updated_at, expires_at
                FROM account_connect_auth_sessions
                WHERE project_id = :project_id AND id = :session_id
                """
            ),
            {"project_id": project_id, "session_id": session_id},
        ).mappings().first()
        if row is None:
            raise AccountConnectAuthSessionNotFoundError(
                "Cloud Easy Connect session not found."
            )
        metadata = row["metadata"] or {}
        if not isinstance(metadata, dict):
            metadata = {}
        status = row["status"]
        # Device sessions complete when hosted Account Connect authorizes the user_code
        if status == "pending" and metadata.get("flow") == "device":
            if metadata.get("authorized_hosted_org_id"):
                return self._complete_device_session(
                    connection,
                    project_id=project_id,
                    session_id=session_id,
                    hosted_org_id=str(metadata["authorized_hosted_org_id"]),
                    actor=actor,
                )
            user_code = metadata.get("user_code")
            if user_code and (self.settings.account_connect_url or "").strip():
                try:
                    authz = self.commercial.account_connect_adapter.get_device_authorization(
                        str(user_code)
                    )
                    if authz.get("status") == "authorized" and authz.get(
                        "hosted_org_id"
                    ):
                        return self._complete_device_session(
                            connection,
                            project_id=project_id,
                            session_id=session_id,
                            hosted_org_id=str(authz["hosted_org_id"]),
                            actor=actor,
                        )
                except AccountConnectSyncError:
                    pass
                except AccountConnectConfigurationError:
                    pass
        return {
            "session_id": row["id"],
            "status": status,
            "method": metadata.get("flow") or "browser",
            "user_code": metadata.get("user_code"),
            "verification_uri": metadata.get("verification_uri"),
            "authorization_url": row["authorization_url"],
            "hosted_org_id": row["hosted_org_id"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "poll_interval_seconds": int(metadata.get("poll_interval_seconds") or 5),
        }

    def _start_browser_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        org_id: str,
        workspace_id: str,
        actor: str,
        return_url: Optional[str],
    ) -> dict[str, Any]:
        # Reuse commercial auth session path (receipted, fail-closed without URL)
        session = self.commercial.create_auth_session(
            connection,
            project_id,
            actor=actor,
            return_url=return_url,
            hosted_org_id=None,
            metadata={"flow": "browser", "source": "cloud_easy_connect"},
        )
        return {
            "session_id": session["id"],
            "status": session["status"],
            "method": "browser",
            "authorization_url": session.get("authorization_url"),
            "user_code": None,
            "verification_uri": None,
            "device_code": None,
            "poll_interval_seconds": 5,
            "expires_at": session.get("expires_at"),
            "message": (
                "Open authorization_url to sign in to BOSS Cloud, then complete the callback."
                if session.get("authorization_url")
                else session.get("last_error")
                or "Easy Connect could not start; configure BOSS_ACCOUNT_CONNECT_URL."
            ),
        }

    def _start_device_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        org_id: str,
        workspace_id: str,
        actor: str,
        return_url: Optional[str],
    ) -> dict[str, Any]:
        user_code = self._mint_user_code()
        device_code = secrets.token_urlsafe(24)
        state = secrets.token_urlsafe(18)
        ac_base = (self.settings.account_connect_url or "").rstrip("/")
        verification_uri = (
            f"{ac_base}/device"
            if ac_base
            else "https://boss.sean.network/#early"
        )
        expires_at = _utcnow() + timedelta(minutes=15)
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            org_id,
            workspace_id,
            project_id,
            f"cloud-easy-connect-device:{project_id}:{device_code}",
            "account_connect.auth_session",
            {"flow": "device", "project_id": project_id},
            {"operation": "device_easy_connect", "data_boundary": "internal"},
        )
        session_id = make_id("acauth")
        metadata = {
            "flow": "device",
            "source": "cloud_easy_connect",
            "user_code": user_code,
            "user_code_hash": hashlib.sha256(user_code.encode()).hexdigest(),
            "device_code": device_code,
            "verification_uri": verification_uri,
            "poll_interval_seconds": 5,
            "return_url": return_url,
        }
        authorization_url = None
        status = "pending"
        last_error = None
        if ac_base:
            try:
                hosted = self.commercial.account_connect_adapter.create_auth_session(
                    project_id=project_id,
                    return_url=return_url or verification_uri,
                    state=state,
                    metadata={
                        "flow": "device",
                        "user_code": user_code,
                        "device_code": device_code,
                    },
                )
                authorization_url = hosted.get("authorization_url")
                metadata["provider_session"] = hosted.get("id")
            except (
                AccountConnectConfigurationError,
                AccountConnectSyncError,
            ) as error:
                status = "blocked"
                last_error = str(error)
                self.receipts.mark_failed(
                    connection,
                    receipt["id"],
                    {"type": error.__class__.__name__, "message": str(error)[:300]},
                )
        else:
            status = "blocked"
            last_error = (
                "BOSS_ACCOUNT_CONNECT_URL is not configured. "
                "Community continues to run independently; set the URL to enable Easy Connect."
            )
            self.receipts.mark_failed(
                connection,
                receipt["id"],
                {"type": "AccountConnectConfigurationError", "message": last_error},
            )

        if status == "pending":
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                f"device:{user_code}",
                {"status": "pending", "user_code": user_code},
            )

        if last_error:
            metadata["last_error"] = last_error
        connection.execute(
            text(
                """
                INSERT INTO account_connect_auth_sessions (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    authorization_url, return_url, provider_ref, state, status,
                    receipt_id, created_by, expires_at, metadata
                )
                VALUES (
                    :id, :project_id, NULL, NULL,
                    :authorization_url, :return_url, :provider_ref, :state,
                    :status, :receipt_id, :actor,
                    :expires_at, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": session_id,
                "project_id": project_id,
                "status": status,
                "provider_ref": metadata.get("provider_session") or device_code,
                "state": state,
                "authorization_url": authorization_url or verification_uri,
                "return_url": return_url,
                "receipt_id": receipt["id"],
                "metadata": __import__("json").dumps(metadata),
                "actor": actor,
                "expires_at": expires_at,
            },
        )
        emit_event(
            connection,
            project_id,
            "cloud.easy_connect_started",
            actor,
            "account_connect_auth_session",
            session_id,
            {
                "method": "device",
                "status": status,
                "user_code": user_code if status == "pending" else None,
            },
        )
        return {
            "session_id": session_id,
            "status": status,
            "method": "device",
            "authorization_url": authorization_url or verification_uri,
            "user_code": user_code if status != "blocked" else None,
            "verification_uri": verification_uri,
            "device_code": device_code if status != "blocked" else None,
            "poll_interval_seconds": 5,
            "expires_at": expires_at.isoformat(),
            "message": (
                f"On any device, open {verification_uri} and enter code {user_code}."
                if status == "pending"
                else last_error
            ),
            "receipt_id": receipt["id"],
        }

    def _complete_device_session(
        self,
        connection: Connection,
        *,
        project_id: str,
        session_id: str,
        hosted_org_id: str,
        actor: str,
    ) -> dict[str, Any]:
        edition_after = self.commercial.link_account_connect_org(
            connection,
            project_id,
            hosted_org_id=hosted_org_id,
            actor=actor,
            marketplace_identity=None,
            billing_portal_ref=None,
            connector_authorization_refs=[],
            optional_telemetry_enabled=False,
            connection_ref=None,
            metadata={"source": "cloud_easy_connect_device"},
        )
        connection.execute(
            text(
                """
                UPDATE account_connect_auth_sessions
                SET status = 'completed',
                    hosted_org_id = :hosted_org_id,
                    completed_at = now(),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": session_id,
                "project_id": project_id,
                "hosted_org_id": hosted_org_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "cloud.easy_connect_completed",
            actor,
            "account_connect_org",
            hosted_org_id,
            {
                "session_id": session_id,
                "hosted_org_id": hosted_org_id,
                "method": "device",
                "linked": True,
            },
        )
        sync_meta = self._sync_entitlements_after_link(
            connection,
            project_id=project_id,
            actor=actor,
            hosted_org_id=hosted_org_id,
            session_id=session_id,
            method="device",
        )
        if sync_meta.get("sync_edition"):
            edition_after = sync_meta["sync_edition"]
        return {
            "session_id": session_id,
            "status": "completed",
            "method": "device",
            "hosted_org_id": hosted_org_id,
            "linked": True,
            "edition": edition_after,
            "account_connect_org": (edition_after or {}).get("account_connect_org"),
            "entitlements_synced": sync_meta["entitlements_synced"],
            "synced_entitlement_count": sync_meta["synced_entitlement_count"],
            "entitlements_sync_error": sync_meta.get("entitlements_sync_error"),
        }

    @staticmethod
    def _mint_user_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        return f"{raw[:4]}-{raw[4:]}"
