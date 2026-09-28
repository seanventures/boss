from __future__ import annotations

import json
from time import perf_counter
from typing import Any, Optional
from urllib.parse import quote, urlencode

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, json_hash, make_id
from .community_edition import (
    CloudConnectAdapter,
    CloudConnectAuthSessionNotFoundError as AccountConnectAuthSessionNotFoundError,
    CloudConnectAuthSessionStateError as AccountConnectAuthSessionStateError,
    CloudConnectConfigurationError as AccountConnectConfigurationError,
    CloudConnectLinkNotFoundError as AccountConnectLinkNotFoundError,
    CloudConnectSyncError as AccountConnectSyncError,
    CommunityEditionService,
)

__all__ = [
    "AccountConnectAdapter",
    "AccountConnectAuthSessionNotFoundError",
    "AccountConnectAuthSessionStateError",
    "AccountConnectConfigurationError",
    "AccountConnectLinkNotFoundError",
    "AccountConnectSyncError",
    "CommercialService",
]

class AccountConnectAdapter(CloudConnectAdapter):
    def check_health(
        self,
        *,
        project_id: str,
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        query = urlencode({"project_id": project_id})
        path = "/v1/health"
        if hosted_org_id:
            path = f"/v1/orgs/{quote(hosted_org_id, safe='')}/health"
        payload = self._get_json(f"{path}?{query}")
        status = payload.get("status")
        if not isinstance(status, str) or not status.strip():
            raise AccountConnectSyncError(
                "Account Connect health response must include a status string."
            )
        return payload

    def fetch_marketplace_pack_listings(
        self,
        *,
        hosted_org_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        query = urlencode({"project_id": project_id})
        payload = self._get_json(
            f"/v1/orgs/{quote(hosted_org_id, safe='')}/marketplace/packs?{query}"
        )
        raw_items = (
            payload.get("items")
            or payload.get("listings")
            or payload.get("packs")
        )
        if not isinstance(raw_items, list):
            raise AccountConnectSyncError(
                "Account Connect marketplace response must include an items list."
            )
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise AccountConnectSyncError(
                    "Account Connect marketplace listings must be objects."
                )
            items.append(item)
        return {
            "provider_ref": payload.get("provider_ref") or payload.get("sync_id"),
            "items": items,
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        }

    def create_team_invite(
        self,
        *,
        hosted_org_id: str,
        project_id: str,
        email: str,
        display_name: Optional[str],
        role_keys: list[str],
        permissions: list[str],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._post_json(
            f"/v1/orgs/{quote(hosted_org_id, safe='')}/team-invites",
            {
                "project_id": project_id,
                "email": email,
                "display_name": display_name,
                "role_keys": role_keys,
                "permissions": permissions,
                "metadata": metadata,
            },
        )
        hosted_ref = payload.get("id") or payload.get("invite_id")
        if not isinstance(hosted_ref, str) or not hosted_ref.strip():
            raise AccountConnectSyncError(
                "Account Connect team invite response must include id or invite_id."
        )
        return payload


    def revoke_team_invite(
        self,
        *,
        hosted_org_id: str,
        hosted_invite_ref: str,
        project_id: str,
        reason: Optional[str],
    ) -> dict[str, Any]:
        return self._post_json(
            (
                f"/v1/orgs/{quote(hosted_org_id, safe='')}/team-invites/"
                f"{quote(hosted_invite_ref, safe='')}/revoke"
            ),
            {"project_id": project_id, "reason": reason},
        )

    def create_billing_portal_session(
        self,
        *,
        hosted_org_id: str,
        project_id: str,
        return_url: Optional[str],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._post_json(
            f"/v1/orgs/{quote(hosted_org_id, safe='')}/billing-portal-sessions",
            {
                "project_id": project_id,
                "return_url": return_url,
                "metadata": metadata,
            },
        )
        portal_url = payload.get("portal_url")
        if not isinstance(portal_url, str) or not portal_url.strip():
            raise AccountConnectSyncError(
                "Account Connect billing portal response must include portal_url."
            )
        return payload


class CommercialService(CommunityEditionService):
    EDITION_PACKS = {
        "community": "community_edition.yaml",
        "cloud": "cloud_edition.yaml",
        "enterprise_cloud": "enterprise_edition.yaml",
        "enterprise_self_hosted": "enterprise_edition.yaml",
    }

    DISPLAY_NAMES = {
        "community": "BOSS Community",
        "cloud": "BOSS Cloud",
        "enterprise_cloud": "BOSS Enterprise Cloud",
        "enterprise_self_hosted": "BOSS Enterprise Self-Hosted",
    }


    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.account_connect_adapter = AccountConnectAdapter(settings)

    def list_auth_sessions(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       authorization_url, return_url, provider_ref, state, status,
                       receipt_id, created_by, expires_at, completed_at, metadata,
                       created_at, updated_at
                FROM account_connect_auth_sessions
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._auth_session_from_row(dict(row)) for row in rows]

    def list_health_checks(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       check_kind, status, latency_ms, provider_ref,
                       response_metadata, error, checked_by, metadata, created_at
                FROM account_connect_health_checks
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._health_check_from_row(dict(row)) for row in rows]

    def run_health_check(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        hosted_org_id: Optional[str] = None,
        check_kind: str = "hosted_service",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        active = self.active_account_connect_org(connection, project_id)
        if active and hosted_org_id and active["hosted_org_id"] != hosted_org_id:
            emit_event(
                connection,
                project_id,
                "account_connect.health_check_denied",
                actor,
                "account_connect_health_check",
                project_id,
                {
                    "reason": "account_connect_hosted_org_mismatch",
                    "requested_hosted_org_id": hosted_org_id,
                    "active_hosted_org_id": active["hosted_org_id"],
                },
            )
            raise AccountConnectAuthSessionStateError(
                "Hosted org ID does not match the active Account Connect link."
            )
        effective_hosted_org_id = hosted_org_id or (
            active["hosted_org_id"] if active else None
        )
        started = perf_counter()
        status = "healthy"
        provider_ref = None
        response_metadata: dict[str, Any] = {}
        error_payload = None
        event_type = "account_connect.health_check_passed"
        try:
            response_metadata = self.account_connect_adapter.check_health(
                project_id=project_id,
                hosted_org_id=effective_hosted_org_id,
            )
            hosted_status = str(response_metadata.get("status") or "").strip().lower()
            status = hosted_status if hosted_status in {
                "healthy",
                "ok",
                "degraded",
                "unhealthy",
            } else "healthy"
            if status == "ok":
                status = "healthy"
            provider_candidate = response_metadata.get("provider_ref") or response_metadata.get(
                "id"
            )
            provider_ref = (
                str(provider_candidate).strip()
                if isinstance(provider_candidate, str) and provider_candidate.strip()
                else None
            )
            if status in {"degraded", "unhealthy"}:
                event_type = "account_connect.health_check_degraded"
        except AccountConnectConfigurationError as error:
            status = "blocked_configuration"
            event_type = "account_connect.health_check_blocked"
            error_payload = {
                "reason": "account_connect_url_missing",
                "message": str(error),
            }
        except AccountConnectSyncError as error:
            status = "failed"
            event_type = "account_connect.health_check_failed"
            error_payload = {"reason": "account_connect_check_failed", "message": str(error)}

        latency_ms = max(0, int((perf_counter() - started) * 1000))
        check_id = make_id("accthealth")
        metadata_payload = metadata or {}
        readiness_packet_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "account_connect_readiness_packet",
            f"Account Connect readiness: {check_kind.strip() or 'hosted_service'}",
            {
                "object_type": "account_connect_readiness_packet",
                "health_check_id": check_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"] if active else None,
                "hosted_org_id": effective_hosted_org_id,
                "check_kind": check_kind.strip() or "hosted_service",
                "status": status,
                "latency_ms": latency_ms,
                "provider_ref": provider_ref,
                "response_metadata": response_metadata,
                "error": error_payload,
                "checked_by": actor,
                "external_service_configured": bool(self.settings.account_connect_url),
                "account_connect_required": self._account_connect_required(
                    self.normalize_edition()
                ),
                "data_boundary": "internal",
            },
            data_boundary="internal",
        )
        metadata_with_artifact = {
            **metadata_payload,
            "readiness_packet_artifact_id": readiness_packet_artifact_id,
        }
        connection.execute(
            text(
                """
                INSERT INTO account_connect_health_checks (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    check_kind, status, latency_ms, provider_ref,
                    response_metadata, error, checked_by, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :check_kind, :status, :latency_ms, :provider_ref,
                    CAST(:response_metadata AS JSONB), CAST(:error AS JSONB),
                    :checked_by, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": check_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"] if active else None,
                "hosted_org_id": effective_hosted_org_id,
                "check_kind": check_kind.strip() or "hosted_service",
                "status": status,
                "latency_ms": latency_ms,
                "provider_ref": provider_ref,
                "response_metadata": json.dumps(response_metadata),
                "error": json.dumps(error_payload) if error_payload else None,
                "checked_by": actor,
                "metadata": json.dumps(metadata_with_artifact),
            },
        )
        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "account_connect_health_check",
            check_id,
            {
                "hosted_org_id": effective_hosted_org_id,
                "status": status,
                "latency_ms": latency_ms,
                "check_kind": check_kind,
                "provider_ref": provider_ref,
                "readiness_packet_artifact_id": readiness_packet_artifact_id,
                "error_reason": error_payload.get("reason") if error_payload else None,
            },
        )
        row = self._health_check_by_id(connection, check_id)
        if not row:
            raise AccountConnectSyncError(
                "Account Connect health check record was not saved."
            )
        return row

    def list_marketplace_pack_listings(
        self,
        connection: Connection,
        project_id: str,
        *,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       listing_id, pack_id, version, display_name, publisher,
                       category, description, status, price_model,
                       price_usd_micros, required_entitlements, capabilities,
                       source_url, metadata, last_sync_run_id, created_at, updated_at
                FROM hosted_marketplace_pack_listings
                WHERE project_id = :project_id
                  AND (:include_inactive OR status IN ('active', 'available'))
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            {
                "project_id": project_id,
                "include_inactive": include_inactive,
                "limit": limit,
            },
        ).mappings().all()
        return [self._marketplace_listing_from_row(dict(row)) for row in rows]

    def list_marketplace_sync_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       status, listing_count, provider_ref, error, synced_by,
                       metadata, created_at
                FROM hosted_marketplace_sync_runs
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._marketplace_sync_run_from_row(dict(row)) for row in rows]

    def sync_marketplace_pack_listings(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        hosted_org_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        active = self.active_account_connect_org(connection, project_id)
        if not active:
            emit_event(
                connection,
                project_id,
                "marketplace.sync_denied",
                actor,
                "hosted_marketplace_sync_run",
                project_id,
                {"reason": "account_connect_not_linked"},
            )
            raise AccountConnectLinkNotFoundError(
                f"No active Account Connect org is linked for project '{project_id}'."
            )
        if hosted_org_id and active["hosted_org_id"] != hosted_org_id:
            emit_event(
                connection,
                project_id,
                "marketplace.sync_denied",
                actor,
                "hosted_marketplace_sync_run",
                project_id,
                {
                    "reason": "account_connect_hosted_org_mismatch",
                    "requested_hosted_org_id": hosted_org_id,
                    "active_hosted_org_id": active["hosted_org_id"],
                },
            )
            raise AccountConnectAuthSessionStateError(
                "Hosted org ID does not match the active Account Connect link."
            )

        sync_id = make_id("mktsync")
        status = "synced"
        provider_ref = None
        error_payload = None
        listings: list[dict[str, Any]] = []
        response_metadata: dict[str, Any] = {}
        event_type = "marketplace.synced"
        try:
            hosted = self.account_connect_adapter.fetch_marketplace_pack_listings(
                hosted_org_id=active["hosted_org_id"],
                project_id=project_id,
            )
            provider_ref_candidate = hosted.get("provider_ref")
            provider_ref = (
                str(provider_ref_candidate).strip()
                if isinstance(provider_ref_candidate, str)
                and provider_ref_candidate.strip()
                else None
            )
            response_metadata = hosted.get("metadata") or {}
            raw_listings = hosted.get("items") or []
            listings = [
                self._normalize_marketplace_listing(item)
                for item in raw_listings
            ]
        except AccountConnectConfigurationError as error:
            status = "blocked_configuration"
            event_type = "marketplace.sync_blocked"
            error_payload = {
                "reason": "account_connect_url_missing",
                "message": str(error),
            }
        except AccountConnectSyncError as error:
            status = "failed"
            event_type = "marketplace.sync_failed"
            error_payload = {
                "reason": "account_connect_marketplace_sync_failed",
                "message": str(error),
            }

        run_metadata = {
            **(metadata or {}),
            "hosted_metadata": response_metadata,
        }
        sync_packet_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "hosted_marketplace_sync_packet",
            f"Hosted marketplace sync: {active['hosted_org_id']}",
            {
                "object_type": "hosted_marketplace_sync_packet",
                "sync_run_id": sync_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"],
                "hosted_org_id": active["hosted_org_id"],
                "status": status,
                "listing_count": len(listings),
                "provider_ref": provider_ref,
                "response_metadata": response_metadata,
                "error": error_payload,
                "synced_by": actor,
                "request_metadata": metadata or {},
                "external_service_configured": bool(self.settings.account_connect_url),
                "data_boundary": "internal",
            },
            data_boundary="internal",
        )
        run_metadata["sync_packet_artifact_id"] = sync_packet_artifact_id
        connection.execute(
            text(
                """
                INSERT INTO hosted_marketplace_sync_runs (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    status, listing_count, provider_ref, error, synced_by, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :status, :listing_count, :provider_ref, CAST(:error AS JSONB),
                    :synced_by, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": sync_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"],
                "hosted_org_id": active["hosted_org_id"],
                "status": status,
                "listing_count": len(listings),
                "provider_ref": provider_ref,
                "error": json.dumps(error_payload) if error_payload else None,
                "synced_by": actor,
                "metadata": json.dumps(run_metadata),
            },
        )

        if status == "synced":
            for listing in listings:
                self._upsert_marketplace_listing(
                    connection,
                    project_id,
                    account_connect_org_id=active["id"],
                    hosted_org_id=active["hosted_org_id"],
                    sync_run_id=sync_id,
                    listing=listing,
                )

        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "hosted_marketplace_sync_run",
            sync_id,
            {
                "hosted_org_id": active["hosted_org_id"],
                "status": status,
                "listing_count": len(listings),
                "provider_ref": provider_ref,
                "sync_packet_artifact_id": sync_packet_artifact_id,
                "error_reason": error_payload.get("reason") if error_payload else None,
            },
        )
        run = self._marketplace_sync_run_by_id(connection, sync_id)
        if not run:
            raise AccountConnectSyncError(
                "Hosted marketplace sync run record was not saved."
            )
        return {
            "project_id": project_id,
            "sync_run": run,
            "listing_count": len(listings),
        }


    def list_team_invites(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id, email,
                       display_name, role_keys, permissions, invite_url,
                       hosted_invite_ref, status, delivery_status, receipt_id,
                       created_by, accepted_by, accepted_at, revoked_by, revoked_at,
                       metadata, created_at, updated_at
                FROM account_connect_team_invites
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._team_invite_from_row(dict(row)) for row in rows]

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
        active = self.active_account_connect_org(connection, project_id)
        if not active:
            emit_event(
                connection,
                project_id,
                "account_connect.team_invite_denied",
                actor,
                "account_connect_team_invite",
                project_id,
                {"reason": "account_connect_not_linked", "email": email},
            )
            raise AccountConnectLinkNotFoundError(
                f"No active Account Connect org is linked for project '{project_id}'."
            )

        role_payload = role_keys or []
        permission_payload = permissions or []
        metadata_payload = metadata or {}
        invite_id = make_id("acctinvite")
        dedupe_payload = {
            "hosted_org_id": active["hosted_org_id"],
            "email": email.lower(),
            "role_keys": role_payload,
            "permissions": permission_payload,
        }
        receipt, created = self._create_account_connect_receipt(
            connection,
            project_id,
            dedupe_key=(
                f"account-connect-team-invite:{project_id}:"
                f"{json_hash(dedupe_payload)}"
            ),
            effect_kind="account_connect.team_invite.create",
            effect_scope={
                "hosted_org_id": active["hosted_org_id"],
                "email": email,
                "data_boundary": "confidential",
            },
            payload={
                **dedupe_payload,
                "display_name": display_name,
                "metadata": metadata_payload,
            },
        )
        if not created and receipt.get("status") == "succeeded":
            existing = self._team_invite_by_receipt(
                connection, project_id, receipt["id"]
            )
            if existing:
                return existing

        status = "pending_acceptance"
        delivery_status = "sent"
        hosted_invite_ref = None
        invite_url = None
        event_type = "account_connect.team_invite_created"
        record_metadata = dict(metadata_payload)
        try:
            hosted = self.account_connect_adapter.create_team_invite(
                hosted_org_id=active["hosted_org_id"],
                project_id=project_id,
                email=email,
                display_name=display_name,
                role_keys=role_payload,
                permissions=permission_payload,
                metadata=metadata_payload,
            )
            hosted_invite_ref = str(hosted.get("id") or hosted.get("invite_id"))
            invite_url = hosted.get("invite_url") if isinstance(hosted.get("invite_url"), str) else None
            status = str(hosted.get("status") or "pending_acceptance")
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                hosted_invite_ref,
                {
                    "hosted_invite_ref": hosted_invite_ref,
                    "status": status,
                    "invite_url_present": bool(invite_url),
                },
            )
        except AccountConnectConfigurationError as error:
            status = "blocked_configuration"
            delivery_status = "blocked"
            event_type = "account_connect.team_invite_blocked"
            record_metadata["blocked_reason"] = "account_connect_url_missing"
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
        except AccountConnectSyncError as error:
            status = "failed"
            delivery_status = "failed"
            event_type = "account_connect.team_invite_failed"
            record_metadata["failure_reason"] = str(error)
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})

        connection.execute(
            text(
                """
                INSERT INTO account_connect_team_invites (
                    id, project_id, account_connect_org_id, hosted_org_id, email,
                    display_name, role_keys, permissions, invite_url,
                    hosted_invite_ref, status, delivery_status, receipt_id,
                    created_by, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :email, :display_name, CAST(:role_keys AS JSONB),
                    CAST(:permissions AS JSONB), :invite_url, :hosted_invite_ref,
                    :status, :delivery_status, :receipt_id, :created_by,
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": invite_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"],
                "hosted_org_id": active["hosted_org_id"],
                "email": email,
                "display_name": display_name,
                "role_keys": json.dumps(role_payload),
                "permissions": json.dumps(permission_payload),
                "invite_url": invite_url,
                "hosted_invite_ref": hosted_invite_ref,
                "status": status,
                "delivery_status": delivery_status,
                "receipt_id": receipt["id"],
                "created_by": actor,
                "metadata": json.dumps(record_metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "account_connect_team_invite",
            invite_id,
            {
                "hosted_org_id": active["hosted_org_id"],
                "email": email,
                "status": status,
                "delivery_status": delivery_status,
                "receipt_id": receipt["id"],
            },
        )
        created_row = self._team_invite_by_id(connection, invite_id)
        if not created_row:
            raise AccountConnectSyncError("Account Connect invite record was not saved.")
        return created_row

    def revoke_team_invite(
        self,
        connection: Connection,
        invite_id: str,
        *,
        project_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        invite = self._team_invite_by_id(connection, invite_id)
        if not invite or invite["project_id"] != project_id:
            raise AccountConnectLinkNotFoundError(
                f"Account Connect team invite '{invite_id}' was not found."
            )
        if invite["status"] in {"revoked", "revoke_blocked_configuration"}:
            return invite

        status = "revoked"
        metadata = dict(invite.get("metadata") or {})
        receipt_id = invite.get("receipt_id")
        if invite.get("hosted_invite_ref"):
            receipt, _created = self._create_account_connect_receipt(
                connection,
                project_id,
                dedupe_key=f"account-connect-team-invite-revoke:{invite_id}",
                effect_kind="account_connect.team_invite.revoke",
                effect_scope={
                    "hosted_org_id": invite["hosted_org_id"],
                    "hosted_invite_ref": invite["hosted_invite_ref"],
                    "data_boundary": "confidential",
                },
                payload={"invite_id": invite_id, "reason": reason},
            )
            receipt_id = receipt["id"]
            try:
                hosted = self.account_connect_adapter.revoke_team_invite(
                    hosted_org_id=invite["hosted_org_id"],
                    hosted_invite_ref=invite["hosted_invite_ref"],
                    project_id=project_id,
                    reason=reason,
                )
                self.receipts.mark_succeeded(
                    connection,
                    receipt["id"],
                    invite["hosted_invite_ref"],
                    {"status": hosted.get("status") or "revoked"},
                )
            except AccountConnectConfigurationError as error:
                status = "revoke_blocked_configuration"
                metadata["revoke_blocked_reason"] = "account_connect_url_missing"
                self.receipts.mark_failed(
                    connection, receipt["id"], {"message": str(error)}
                )
            except AccountConnectSyncError as error:
                status = "revoke_failed"
                metadata["revoke_failure_reason"] = str(error)
                self.receipts.mark_failed(
                    connection, receipt["id"], {"message": str(error)}
                )

        connection.execute(
            text(
                """
                UPDATE account_connect_team_invites
                SET status = :status,
                    delivery_status = CASE
                      WHEN :status = 'revoked' THEN 'revoked'
                      ELSE delivery_status
                    END,
                    receipt_id = :receipt_id,
                    revoked_by = :actor,
                    revoked_at = CASE
                      WHEN :status = 'revoked' THEN now()
                      ELSE revoked_at
                    END,
                    metadata = CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": invite_id,
                "status": status,
                "receipt_id": receipt_id,
                "actor": actor,
                "metadata": json.dumps(metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            "account_connect.team_invite_revoked"
            if status == "revoked"
            else "account_connect.team_invite_revoke_blocked",
            actor,
            "account_connect_team_invite",
            invite_id,
            {"status": status, "reason": reason, "receipt_id": receipt_id},
        )
        updated = self._team_invite_by_id(connection, invite_id)
        if not updated:
            raise AccountConnectSyncError("Account Connect invite record was not saved.")
        return updated

    def list_billing_portal_sessions(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       portal_url, return_url, provider_ref, status, receipt_id,
                       created_by, expires_at, metadata, created_at, updated_at
                FROM account_connect_billing_portal_sessions
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._billing_session_from_row(dict(row)) for row in rows]

    def create_billing_portal_session(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        return_url: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        active = self.active_account_connect_org(connection, project_id)
        if not active:
            emit_event(
                connection,
                project_id,
                "account_connect.billing_portal_denied",
                actor,
                "account_connect_billing_portal_session",
                project_id,
                {"reason": "account_connect_not_linked"},
            )
            raise AccountConnectLinkNotFoundError(
                f"No active Account Connect org is linked for project '{project_id}'."
            )

        session_id = make_id("acctbill")
        metadata_payload = metadata or {}
        receipt, created = self._create_account_connect_receipt(
            connection,
            project_id,
            dedupe_key=(
                f"account-connect-billing-portal:{project_id}:"
                f"{active['hosted_org_id']}:{actor}:{json_hash(metadata_payload)}"
            ),
            effect_kind="account_connect.billing_portal_session.create",
            effect_scope={
                "hosted_org_id": active["hosted_org_id"],
                "data_boundary": "confidential",
            },
            payload={"return_url": return_url, "metadata": metadata_payload},
        )
        if not created and receipt.get("status") == "succeeded":
            existing = self._billing_session_by_receipt(
                connection, project_id, receipt["id"]
            )
            if existing:
                return existing

        portal_url = None
        provider_ref = None
        expires_at = None
        status = "ready"
        event_type = "account_connect.billing_portal_session_created"
        record_metadata = dict(metadata_payload)
        try:
            hosted = self.account_connect_adapter.create_billing_portal_session(
                hosted_org_id=active["hosted_org_id"],
                project_id=project_id,
                return_url=return_url,
                metadata=metadata_payload,
            )
            portal_url = str(hosted["portal_url"])
            provider_ref = (
                str(hosted.get("id") or hosted.get("session_id"))
                if hosted.get("id") or hosted.get("session_id")
                else None
            )
            expires_at = hosted.get("expires_at")
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref,
                {
                    "provider_ref": provider_ref,
                    "portal_url_present": True,
                    "expires_at": expires_at,
                },
            )
        except AccountConnectConfigurationError as error:
            status = "blocked_configuration"
            event_type = "account_connect.billing_portal_blocked"
            record_metadata["blocked_reason"] = "account_connect_url_missing"
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
        except AccountConnectSyncError as error:
            status = "failed"
            event_type = "account_connect.billing_portal_failed"
            record_metadata["failure_reason"] = str(error)
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})

        connection.execute(
            text(
                """
                INSERT INTO account_connect_billing_portal_sessions (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    portal_url, return_url, provider_ref, status, receipt_id,
                    created_by, expires_at, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :portal_url, :return_url, :provider_ref, :status, :receipt_id,
                    :created_by, CAST(:expires_at AS TIMESTAMPTZ),
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": session_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"],
                "hosted_org_id": active["hosted_org_id"],
                "portal_url": portal_url,
                "return_url": return_url,
                "provider_ref": provider_ref,
                "status": status,
                "receipt_id": receipt["id"],
                "created_by": actor,
                "expires_at": expires_at,
                "metadata": json.dumps(record_metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "account_connect_billing_portal_session",
            session_id,
            {
                "hosted_org_id": active["hosted_org_id"],
                "status": status,
                "receipt_id": receipt["id"],
            },
        )
        created_row = self._billing_session_by_id(connection, session_id)
        if not created_row:
            raise AccountConnectSyncError(
                "Account Connect billing portal session was not saved."
            )
        return created_row


    def _team_invite_by_id(
        self, connection: Connection, invite_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id, email,
                       display_name, role_keys, permissions, invite_url,
                       hosted_invite_ref, status, delivery_status, receipt_id,
                       created_by, accepted_by, accepted_at, revoked_by, revoked_at,
                       metadata, created_at, updated_at
                FROM account_connect_team_invites
                WHERE id = :id
                """
            ),
            {"id": invite_id},
        ).mappings().first()
        return self._team_invite_from_row(dict(row)) if row else None

    def _team_invite_by_receipt(
        self, connection: Connection, project_id: str, receipt_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id, email,
                       display_name, role_keys, permissions, invite_url,
                       hosted_invite_ref, status, delivery_status, receipt_id,
                       created_by, accepted_by, accepted_at, revoked_by, revoked_at,
                       metadata, created_at, updated_at
                FROM account_connect_team_invites
                WHERE project_id = :project_id AND receipt_id = :receipt_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "receipt_id": receipt_id},
        ).mappings().first()
        return self._team_invite_from_row(dict(row)) if row else None

    def _billing_session_by_id(
        self, connection: Connection, session_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       portal_url, return_url, provider_ref, status, receipt_id,
                       created_by, expires_at, metadata, created_at, updated_at
                FROM account_connect_billing_portal_sessions
                WHERE id = :id
                """
            ),
            {"id": session_id},
        ).mappings().first()
        return self._billing_session_from_row(dict(row)) if row else None

    def _billing_session_by_receipt(
        self, connection: Connection, project_id: str, receipt_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       portal_url, return_url, provider_ref, status, receipt_id,
                       created_by, expires_at, metadata, created_at, updated_at
                FROM account_connect_billing_portal_sessions
                WHERE project_id = :project_id AND receipt_id = :receipt_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "receipt_id": receipt_id},
        ).mappings().first()
        return self._billing_session_from_row(dict(row)) if row else None


    def _health_check_by_id(
        self, connection: Connection, check_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       check_kind, status, latency_ms, provider_ref,
                       response_metadata, error, checked_by, metadata, created_at
                FROM account_connect_health_checks
                WHERE id = :id
                """
            ),
            {"id": check_id},
        ).mappings().first()
        return self._health_check_from_row(dict(row)) if row else None

    def _marketplace_sync_run_by_id(
        self, connection: Connection, sync_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       status, listing_count, provider_ref, error, synced_by,
                       metadata, created_at
                FROM hosted_marketplace_sync_runs
                WHERE id = :id
                """
            ),
            {"id": sync_id},
        ).mappings().first()
        return self._marketplace_sync_run_from_row(dict(row)) if row else None

    def _normalize_marketplace_listing(self, item: dict[str, Any]) -> dict[str, Any]:
        listing_id = str(item.get("listing_id") or item.get("id") or "").strip()
        pack_id = str(item.get("pack_id") or item.get("module_id") or "").strip()
        if not pack_id:
            raise AccountConnectSyncError(
                "Hosted marketplace listing is missing pack_id."
            )
        if not listing_id:
            listing_id = pack_id
        version = str(item.get("version") or "0.0.0").strip() or "0.0.0"
        display_name = (
            str(item.get("display_name") or item.get("name") or pack_id)
            .strip()
            or pack_id
        )
        publisher = str(item.get("publisher") or "Unknown").strip() or "Unknown"
        category = str(item.get("category") or "general").strip() or "general"
        status = str(item.get("status") or "active").strip() or "active"
        price_model = item.get("price_model")
        if price_model is not None:
            price_model = str(price_model).strip() or None
        price_usd_micros = item.get("price_usd_micros")
        if price_usd_micros is not None:
            try:
                price_usd_micros = int(price_usd_micros)
            except (TypeError, ValueError) as error:
                raise AccountConnectSyncError(
                    f"Hosted marketplace listing '{pack_id}' has invalid price_usd_micros."
                ) from error
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        return {
            "listing_id": listing_id,
            "pack_id": pack_id,
            "version": version,
            "display_name": display_name,
            "publisher": publisher,
            "category": category,
            "description": item.get("description")
            if isinstance(item.get("description"), str)
            else None,
            "status": status,
            "price_model": price_model,
            "price_usd_micros": price_usd_micros,
            "required_entitlements": self._string_list(item.get("required_entitlements")),
            "capabilities": self._string_list(item.get("capabilities")),
            "source_url": item.get("source_url")
            if isinstance(item.get("source_url"), str)
            else None,
            "metadata": metadata,
        }

    def _string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list):
            raise AccountConnectSyncError("Hosted marketplace list fields must be arrays.")
        return [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]

    def _upsert_marketplace_listing(
        self,
        connection: Connection,
        project_id: str,
        *,
        account_connect_org_id: str,
        hosted_org_id: str,
        sync_run_id: str,
        listing: dict[str, Any],
    ) -> None:
        listing_id = f"mklisting_{project_id}_{listing['listing_id']}"
        connection.execute(
            text(
                """
                INSERT INTO hosted_marketplace_pack_listings (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    listing_id, pack_id, version, display_name, publisher,
                    category, description, status, price_model, price_usd_micros,
                    required_entitlements, capabilities, source_url, metadata,
                    last_sync_run_id
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :listing_id, :pack_id, :version, :display_name, :publisher,
                    :category, :description, :status, :price_model,
                    :price_usd_micros, CAST(:required_entitlements AS JSONB),
                    CAST(:capabilities AS JSONB), :source_url,
                    CAST(:metadata AS JSONB), :last_sync_run_id
                )
                ON CONFLICT (project_id, listing_id) DO UPDATE
                SET account_connect_org_id = EXCLUDED.account_connect_org_id,
                    hosted_org_id = EXCLUDED.hosted_org_id,
                    pack_id = EXCLUDED.pack_id,
                    version = EXCLUDED.version,
                    display_name = EXCLUDED.display_name,
                    publisher = EXCLUDED.publisher,
                    category = EXCLUDED.category,
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    price_model = EXCLUDED.price_model,
                    price_usd_micros = EXCLUDED.price_usd_micros,
                    required_entitlements = EXCLUDED.required_entitlements,
                    capabilities = EXCLUDED.capabilities,
                    source_url = EXCLUDED.source_url,
                    metadata = EXCLUDED.metadata,
                    last_sync_run_id = EXCLUDED.last_sync_run_id,
                    updated_at = now()
                """
            ),
            {
                "id": listing_id,
                "project_id": project_id,
                "account_connect_org_id": account_connect_org_id,
                "hosted_org_id": hosted_org_id,
                "listing_id": listing["listing_id"],
                "pack_id": listing["pack_id"],
                "version": listing["version"],
                "display_name": listing["display_name"],
                "publisher": listing["publisher"],
                "category": listing["category"],
                "description": listing["description"],
                "status": listing["status"],
                "price_model": listing["price_model"],
                "price_usd_micros": listing["price_usd_micros"],
                "required_entitlements": json.dumps(listing["required_entitlements"]),
                "capabilities": json.dumps(listing["capabilities"]),
                "source_url": listing["source_url"],
                "metadata": json.dumps(listing["metadata"]),
                "last_sync_run_id": sync_run_id,
            },
        )


    def _team_invite_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "accepted_at": row["accepted_at"].isoformat()
            if row.get("accepted_at") is not None
            else None,
            "revoked_at": row["revoked_at"].isoformat()
            if row.get("revoked_at") is not None
            else None,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _billing_session_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "expires_at": row["expires_at"].isoformat()
            if row.get("expires_at") is not None
            else None,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }


    def _health_check_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        readiness_packet_artifact_id = metadata.get("readiness_packet_artifact_id")
        return {
            **row,
            "readiness_packet_artifact_id": (
                readiness_packet_artifact_id
                if isinstance(readiness_packet_artifact_id, str)
                else None
            ),
            "created_at": row["created_at"].isoformat(),
        }

    def _marketplace_sync_run_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        sync_packet_artifact_id = metadata.get("sync_packet_artifact_id")
        return {
            **row,
            "sync_packet_artifact_id": (
                sync_packet_artifact_id
                if isinstance(sync_packet_artifact_id, str)
                else None
            ),
            "created_at": row["created_at"].isoformat(),
        }

    def _marketplace_listing_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "price_usd_micros": int(row["price_usd_micros"])
            if row.get("price_usd_micros") is not None
            else None,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
