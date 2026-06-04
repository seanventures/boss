from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event


class AccountConnectLinkNotFoundError(RuntimeError):
    pass


class AccountConnectConfigurationError(RuntimeError):
    pass


class AccountConnectSyncError(RuntimeError):
    pass


class AccountConnectAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch_entitlements(
        self,
        *,
        hosted_org_id: str,
        project_id: str,
    ) -> list[dict[str, Any]]:
        if not self.settings.account_connect_url:
            raise AccountConnectConfigurationError(
                "BOSS_ACCOUNT_CONNECT_URL is required before hosted Account Connect sync."
            )
        base_url = self.settings.account_connect_url.rstrip("/")
        query = urlencode({"project_id": project_id})
        url = (
            f"{base_url}/v1/orgs/{quote(hosted_org_id, safe='')}"
            f"/entitlements?{query}"
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": "boss-control-plane/0.3",
        }
        if self.settings.account_connect_token:
            headers["Authorization"] = f"Bearer {self.settings.account_connect_token}"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(  # noqa: S310 - configured trusted Account Connect endpoint.
                request,
                timeout=self.settings.account_connect_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            raise AccountConnectSyncError(
                f"Account Connect sync failed with HTTP {error.code}."
            ) from error
        except URLError as error:
            raise AccountConnectSyncError(
                f"Account Connect sync failed: {error.reason}."
            ) from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AccountConnectSyncError(
                "Account Connect sync returned invalid JSON."
            ) from error
        entitlements = payload.get("entitlements")
        if not isinstance(entitlements, list):
            raise AccountConnectSyncError(
                "Account Connect sync response must include an entitlements list."
            )
        normalized: list[dict[str, Any]] = []
        for entitlement in entitlements:
            if not isinstance(entitlement, dict):
                raise AccountConnectSyncError(
                    "Account Connect entitlement entries must be objects."
                )
            normalized.append(entitlement)
        return normalized


class CommercialService:
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
        self.settings = settings
        self.account_connect_adapter = AccountConnectAdapter(settings)

    def normalize_edition(self, edition: Optional[str] = None) -> str:
        candidate = (edition or self.settings.boss_edition or "community").strip()
        normalized = candidate.lower().replace("-", "_")
        if normalized == "enterprise":
            return "enterprise_self_hosted"
        if normalized not in self.EDITION_PACKS:
            return "community"
        return normalized

    def _pack_path(self, edition_id: str) -> Path:
        return self.settings.specpack_root / "packs" / self.EDITION_PACKS[edition_id]

    def _load_pack(self, edition_id: str) -> dict[str, Any]:
        payload = yaml.safe_load(self._pack_path(edition_id).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return payload

    def _account_connect_required(self, edition_id: str) -> bool:
        return edition_id in {"cloud", "enterprise_cloud"}

    def _connection_mode(self, edition_id: str) -> str:
        if edition_id == "community":
            return "disabled"
        if edition_id == "enterprise_self_hosted":
            return "detached"
        return "cloud_required"

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

    def active_account_connect_org(
        self, connection: Connection, project_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, hosted_org_id, local_org_id, connection_mode,
                       marketplace_identity, billing_portal_ref,
                       connector_authorization_refs, optional_telemetry_enabled,
                       connection_ref, status, linked_by, linked_at, unlinked_by,
                       unlinked_at, metadata, created_at, updated_at
                FROM account_connect_orgs
                WHERE project_id = :project_id AND status = 'linked'
                ORDER BY linked_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            return None
        return self._account_connect_org_from_row(dict(row))

    def edition_status(self, connection: Connection, project_id: str) -> dict[str, Any]:
        edition_id = self.normalize_edition()
        pack = self._load_pack(edition_id)
        active_link = self.active_account_connect_org(connection, project_id)
        account_connect_required = self._account_connect_required(edition_id)
        account_connect_status = "linked" if active_link else "not_linked"
        if not account_connect_required:
            account_connect_status = "not_required" if not active_link else "linked"
        effective_status = (
            "blocked_account_connect_required"
            if account_connect_required and not active_link
            else "active"
        )
        return {
            "project_id": project_id,
            "edition_id": edition_id,
            "display_name": self.DISPLAY_NAMES[edition_id],
            "status": effective_status,
            "account_connect_required": account_connect_required,
            "account_connect_status": account_connect_status,
            "account_connect_org": active_link,
            "local_auth_allowed": edition_id in {"community", "enterprise_self_hosted"},
            "hosted_auth_required": account_connect_required,
            "connection_mode": active_link["connection_mode"]
            if active_link
            else self._connection_mode(edition_id),
            "license_candidate": pack.get("license_candidate"),
            "legal_review_required": bool(pack.get("legal_review_required", False)),
            "included_capabilities": pack.get("includes", []) or [],
            "commercial_capabilities": self._commercial_capabilities(edition_id, pack),
            "blocked_reason": "account_connect_required"
            if effective_status == "blocked_account_connect_required"
            else None,
            "source_pack": self.EDITION_PACKS[edition_id],
        }

    def list_license_entitlements(
        self,
        connection: Connection,
        project_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        edition = self.edition_status(connection, project_id)
        capability_status = (
            "blocked_account_connect_required"
            if edition["status"] == "blocked_account_connect_required"
            else "active"
        )
        items = [
            {
                "id": f"ent_local_{project_id}_{capability}",
                "project_id": project_id,
                "edition_id": edition["edition_id"],
                "account_connect_org_id": (
                    edition["account_connect_org"] or {}
                ).get("hosted_org_id"),
                "scope_type": "project",
                "scope_id": project_id,
                "capability_key": capability,
                "capabilities": [capability],
                "seats": None,
                "status": capability_status,
                "source": "edition_pack",
                "expires_at": None,
                "metadata": {"source_pack": edition["source_pack"]},
                "created_at": None,
                "updated_at": None,
            }
            for capability in edition["included_capabilities"]
        ]
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, edition_id, account_connect_org_id,
                       scope_type, scope_id, capability_key, capabilities, seats,
                       status, source, expires_at, metadata, created_at, updated_at
                FROM license_entitlements
                WHERE project_id = :project_id
                  AND (:include_inactive OR status IN ('active', 'trial', 'detached'))
                ORDER BY source ASC, capability_key ASC, created_at DESC
                """
            ),
            {"project_id": project_id, "include_inactive": include_inactive},
        ).mappings().all()
        items.extend(self._license_entitlement_from_row(dict(row)) for row in rows)
        return items

    def link_account_connect_org(
        self,
        connection: Connection,
        project_id: str,
        *,
        hosted_org_id: str,
        actor: str,
        connection_ref: Optional[str] = None,
        marketplace_identity: Optional[dict[str, Any]] = None,
        billing_portal_ref: Optional[str] = None,
        connector_authorization_refs: Optional[list[str]] = None,
        optional_telemetry_enabled: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        edition_id = self.normalize_edition()
        pack = self._load_pack(edition_id)
        local_org_id = self._project_org_id(connection, project_id)
        link_id = f"acctconn_{project_id}_{hosted_org_id}"
        connection_mode = self._connection_mode(edition_id)
        connection.execute(
            text(
                """
                INSERT INTO account_connect_orgs (
                    id, project_id, hosted_org_id, local_org_id, connection_mode,
                    marketplace_identity, billing_portal_ref,
                    connector_authorization_refs, optional_telemetry_enabled,
                    connection_ref, status, linked_by, linked_at, unlinked_by,
                    unlinked_at, metadata
                )
                VALUES (
                    :id, :project_id, :hosted_org_id, :local_org_id,
                    :connection_mode, CAST(:marketplace_identity AS JSONB),
                    :billing_portal_ref,
                    CAST(:connector_authorization_refs AS JSONB),
                    :optional_telemetry_enabled, :connection_ref, 'linked',
                    :linked_by, now(), NULL, NULL, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, hosted_org_id) DO UPDATE
                SET local_org_id = EXCLUDED.local_org_id,
                    connection_mode = EXCLUDED.connection_mode,
                    marketplace_identity = EXCLUDED.marketplace_identity,
                    billing_portal_ref = EXCLUDED.billing_portal_ref,
                    connector_authorization_refs = EXCLUDED.connector_authorization_refs,
                    optional_telemetry_enabled = EXCLUDED.optional_telemetry_enabled,
                    connection_ref = EXCLUDED.connection_ref,
                    status = 'linked',
                    linked_by = EXCLUDED.linked_by,
                    linked_at = now(),
                    unlinked_by = NULL,
                    unlinked_at = NULL,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": link_id,
                "project_id": project_id,
                "hosted_org_id": hosted_org_id,
                "local_org_id": local_org_id,
                "connection_mode": connection_mode,
                "marketplace_identity": json.dumps(marketplace_identity or {}),
                "billing_portal_ref": billing_portal_ref,
                "connector_authorization_refs": json.dumps(
                    connector_authorization_refs or []
                ),
                "optional_telemetry_enabled": optional_telemetry_enabled,
                "connection_ref": connection_ref,
                "linked_by": actor,
                "metadata": json.dumps(metadata or {}),
            },
        )
        capabilities = pack.get("includes", []) or []
        entitlement_id = f"lic_{project_id}_account_connect"
        connection.execute(
            text(
                """
                INSERT INTO license_entitlements (
                    id, project_id, edition_id, account_connect_org_id,
                    scope_type, scope_id, capability_key, capabilities, seats,
                    status, source, metadata
                )
                VALUES (
                    :id, :project_id, :edition_id, :account_connect_org_id,
                    'project', :scope_id, 'account_connect',
                    CAST(:capabilities AS JSONB), NULL, 'active',
                    'account_connect', CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, capability_key, source) DO UPDATE
                SET edition_id = EXCLUDED.edition_id,
                    account_connect_org_id = EXCLUDED.account_connect_org_id,
                    capabilities = EXCLUDED.capabilities,
                    status = 'active',
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": entitlement_id,
                "project_id": project_id,
                "edition_id": edition_id,
                "account_connect_org_id": hosted_org_id,
                "scope_id": project_id,
                "capabilities": json.dumps(capabilities),
                "metadata": json.dumps(
                    {
                        "source_pack": self.EDITION_PACKS[edition_id],
                        "hosted_org_id": hosted_org_id,
                    }
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "account_connect.linked",
            actor,
            "account_connect_org",
            link_id,
            {
                "hosted_org_id": hosted_org_id,
                "edition_id": edition_id,
                "connection_mode": connection_mode,
            },
        )
        return self.edition_status(connection, project_id)

    def unlink_account_connect_org(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        hosted_org_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        active = self.active_account_connect_org(connection, project_id)
        if not active:
            raise AccountConnectLinkNotFoundError(
                f"No active Account Connect org is linked for project '{project_id}'."
            )
        if hosted_org_id and active["hosted_org_id"] != hosted_org_id:
            raise AccountConnectLinkNotFoundError(
                f"Account Connect org '{hosted_org_id}' is not active for project '{project_id}'."
            )
        connection.execute(
            text(
                """
                UPDATE account_connect_orgs
                SET status = 'unlinked',
                    unlinked_by = :actor,
                    unlinked_at = now(),
                    metadata = metadata || CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": active["id"],
                "actor": actor,
                "metadata": json.dumps({"unlink_reason": reason or "operator_unlinked"}),
            },
        )
        connection.execute(
            text(
                """
                UPDATE license_entitlements
                SET status = 'detached',
                    updated_at = now()
                WHERE project_id = :project_id AND source = 'account_connect'
                """
            ),
            {"project_id": project_id},
        )
        emit_event(
            connection,
            project_id,
            "account_connect.unlinked",
            actor,
            "account_connect_org",
            active["id"],
            {
                "hosted_org_id": active["hosted_org_id"],
                "reason": reason or "operator_unlinked",
            },
        )
        return self.edition_status(connection, project_id)

    def sync_account_connect_entitlements(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        active = self.active_account_connect_org(connection, project_id)
        if not active:
            self._emit_sync_denied(
                connection,
                project_id,
                actor,
                "account_connect_not_linked",
            )
            raise AccountConnectLinkNotFoundError(
                f"No active Account Connect org is linked for project '{project_id}'."
            )
        if hosted_org_id and active["hosted_org_id"] != hosted_org_id:
            self._emit_sync_denied(
                connection,
                project_id,
                actor,
                "account_connect_hosted_org_mismatch",
            )
            raise AccountConnectLinkNotFoundError(
                f"Account Connect org '{hosted_org_id}' is not active for project '{project_id}'."
            )
        try:
            entitlements = self.account_connect_adapter.fetch_entitlements(
                hosted_org_id=active["hosted_org_id"],
                project_id=project_id,
            )
        except AccountConnectConfigurationError:
            self._emit_sync_denied(
                connection,
                project_id,
                actor,
                "account_connect_url_missing",
            )
            raise
        except AccountConnectSyncError:
            self._emit_sync_denied(
                connection,
                project_id,
                actor,
                "account_connect_sync_failed",
            )
            raise
        synced_count = 0
        for entitlement in entitlements:
            capability_key = str(entitlement.get("capability_key") or "").strip()
            if not capability_key:
                raise AccountConnectSyncError(
                    "Hosted entitlement is missing capability_key."
                )
            capabilities = entitlement.get("capabilities") or [capability_key]
            if not isinstance(capabilities, list):
                raise AccountConnectSyncError(
                    f"Hosted entitlement '{capability_key}' has invalid capabilities."
                )
            status = str(entitlement.get("status") or "active").strip() or "active"
            source = str(entitlement.get("source") or "account_connect_hosted")
            entitlement_id = (
                str(entitlement.get("id") or "").strip()
                or f"lic_{project_id}_hosted_{capability_key}"
            )
            metadata = {
                "hosted_org_id": active["hosted_org_id"],
                **(entitlement.get("metadata") or {}),
            }
            connection.execute(
                text(
                    """
                    INSERT INTO license_entitlements (
                        id, project_id, edition_id, account_connect_org_id,
                        scope_type, scope_id, capability_key, capabilities, seats,
                        status, source, expires_at, metadata
                    )
                    VALUES (
                        :id, :project_id, :edition_id, :account_connect_org_id,
                        :scope_type, :scope_id, :capability_key,
                        CAST(:capabilities AS JSONB), :seats, :status, :source,
                        CAST(:expires_at AS TIMESTAMPTZ), CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (project_id, capability_key, source) DO UPDATE
                    SET edition_id = EXCLUDED.edition_id,
                        account_connect_org_id = EXCLUDED.account_connect_org_id,
                        scope_type = EXCLUDED.scope_type,
                        scope_id = EXCLUDED.scope_id,
                        capabilities = EXCLUDED.capabilities,
                        seats = EXCLUDED.seats,
                        status = EXCLUDED.status,
                        expires_at = EXCLUDED.expires_at,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """
                ),
                {
                    "id": entitlement_id,
                    "project_id": project_id,
                    "edition_id": entitlement.get("edition_id")
                    or self.normalize_edition(),
                    "account_connect_org_id": active["hosted_org_id"],
                    "scope_type": entitlement.get("scope_type") or "project",
                    "scope_id": entitlement.get("scope_id") or project_id,
                    "capability_key": capability_key,
                    "capabilities": json.dumps(capabilities),
                    "seats": entitlement.get("seats"),
                    "status": status,
                    "source": source,
                    "expires_at": entitlement.get("expires_at"),
                    "metadata": json.dumps(metadata),
                },
            )
            synced_count += 1
        emit_event(
            connection,
            project_id,
            "account_connect.synced",
            actor,
            "account_connect_org",
            active["id"],
            {
                "hosted_org_id": active["hosted_org_id"],
                "synced_entitlement_count": synced_count,
            },
        )
        return {
            "project_id": project_id,
            "edition": self.edition_status(connection, project_id),
            "synced_entitlement_count": synced_count,
        }

    def _emit_sync_denied(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
        reason: str,
    ) -> None:
        emit_event(
            connection,
            project_id,
            "account_connect.sync_denied",
            actor,
            "account_connect_org",
            project_id,
            {"reason": reason},
        )

    def _commercial_capabilities(
        self, edition_id: str, pack: dict[str, Any]
    ) -> list[str]:
        if edition_id == "community":
            return []
        return pack.get("includes", []) or []

    def _account_connect_org_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "linked_at": row["linked_at"].isoformat()
            if row.get("linked_at") is not None
            else None,
            "unlinked_at": row["unlinked_at"].isoformat()
            if row.get("unlinked_at") is not None
            else None,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _license_entitlement_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "expires_at": row["expires_at"].isoformat()
            if row.get("expires_at") is not None
            else None,
            "created_at": row["created_at"].isoformat()
            if row.get("created_at") is not None
            else None,
            "updated_at": row["updated_at"].isoformat()
            if row.get("updated_at") is not None
            else None,
        }
