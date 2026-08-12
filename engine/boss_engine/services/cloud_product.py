"""BOSS Cloud / Community product distinction and Easy Connect.

Community runs fully independently. Cloud/Enterprise add hosted trust and
managed capacity. Easy Connect lets a self-hosted Community install link to
BOSS Cloud without pasting long-lived secrets into the browser.
"""

from __future__ import annotations

import hashlib
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
    AccountConnectSyncError,
    CommercialService,
)
from .core import emit_event, make_id
from .receipts import ReceiptService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            "description": "Receipts and audit trails locally; retention SLAs and support on Cloud/Enterprise.",
            "status": "partial",
            "editions": ["community", "cloud", "enterprise_cloud", "enterprise_self_hosted"],
            "community_local": True,
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
            features.append(
                {
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
            )
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
        return {
            "project_id": project_id,
            "edition": edition,
            "independent": edition["edition_id"] == "community"
            or not edition.get("account_connect_required")
            or bool(edition.get("account_connect_org")),
            "linked": bool(edition.get("account_connect_org")),
            "easy_connect": {
                "ready": bool(ac_url),
                "methods": ["browser", "device"],
                "blocked_reason": (
                    None
                    if ac_url
                    else "BOSS_ACCOUNT_CONNECT_URL is not configured on this control plane."
                ),
            },
            "message": (
                "This install runs independently. Connect to BOSS Cloud only when you want "
                "hosted team, marketplace, or managed capacity features."
                if edition["edition_id"] == "community"
                else edition.get("blocked_reason")
                or "Edition status active."
            ),
        }

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
            {"session_id": session_id, "hosted_org_id": hosted_org_id},
        )
        return {
            "session_id": session_id,
            "status": "completed",
            "method": "device",
            "hosted_org_id": hosted_org_id,
            "linked": True,
            "edition": edition_after,
            "account_connect_org": (edition_after or {}).get("account_connect_org"),
        }

    @staticmethod
    def _mint_user_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        return f"{raw[:4]}-{raw[4:]}"
