from __future__ import annotations

import json
from time import perf_counter
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, json_hash, make_id
from .receipts import ReceiptService


class AccountConnectLinkNotFoundError(RuntimeError):
    pass


class AccountConnectConfigurationError(RuntimeError):
    pass


class AccountConnectSyncError(RuntimeError):
    pass


class AccountConnectAuthSessionStateError(RuntimeError):
    pass


class AccountConnectAuthSessionNotFoundError(RuntimeError):
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
            entry = dict(entitlement)
            entitlement_key = str(entry.get("entitlement_key") or "").strip()
            if not entry.get("capability_key") and entitlement_key:
                entry["capability_key"] = entitlement_key
            if not entry.get("id") and entitlement_key:
                entry["id"] = f"ent_{entitlement_key}"
            if not entry.get("source"):
                entry["source"] = "account_connect_hosted"
            normalized.append(entry)
        return normalized

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

    def create_auth_session(
        self,
        *,
        project_id: str,
        return_url: Optional[str],
        state: str,
        metadata: dict[str, Any],
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        path = "/v1/auth-sessions"
        if hosted_org_id:
            path = f"/v1/orgs/{quote(hosted_org_id, safe='')}/auth-sessions"
        payload = self._post_json(
            path,
            {
                "project_id": project_id,
                "hosted_org_id": hosted_org_id,
                "return_url": return_url,
                "state": state,
                "metadata": metadata,
            },
        )
        authorization_url = payload.get("authorization_url")
        if not isinstance(authorization_url, str) or not authorization_url.strip():
            raise AccountConnectSyncError(
                "Account Connect auth session response must include authorization_url."
            )
        return payload

    def complete_auth_session(
        self,
        *,
        project_id: str,
        provider_ref: str,
        code: str,
        state: str,
        metadata: dict[str, Any],
        hosted_org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if hosted_org_id:
            path = (
                f"/v1/orgs/{quote(hosted_org_id, safe='')}/auth-sessions/"
                f"{quote(provider_ref, safe='')}/complete"
            )
        else:
            path = f"/v1/auth-sessions/{quote(provider_ref, safe='')}/complete"
        payload = self._post_json(
            path,
            {
                "project_id": project_id,
                "code": code,
                "state": state,
                "metadata": metadata,
            },
        )
        hosted_ref = payload.get("hosted_org_id")
        if not isinstance(hosted_ref, str) or not hosted_ref.strip():
            raise AccountConnectSyncError(
                "Account Connect auth completion response must include hosted_org_id."
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

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.account_connect_url:
            raise AccountConnectConfigurationError(
                "BOSS_ACCOUNT_CONNECT_URL is required before hosted Account Connect writes."
            )
        base_url = self.settings.account_connect_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "boss-control-plane/0.3",
        }
        if self.settings.account_connect_token:
            headers["Authorization"] = f"Bearer {self.settings.account_connect_token}"
        request = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(  # noqa: S310 - configured trusted Account Connect endpoint.
                request,
                timeout=self.settings.account_connect_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            raise AccountConnectSyncError(
                f"Account Connect write failed with HTTP {error.code}."
            ) from error
        except URLError as error:
            raise AccountConnectSyncError(
                f"Account Connect write failed: {error.reason}."
            ) from error
        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise AccountConnectSyncError(
                "Account Connect write returned invalid JSON."
            ) from error
        if not isinstance(decoded, dict):
            raise AccountConnectSyncError(
                "Account Connect write response must be a JSON object."
            )
        return decoded

    def _get_json(self, path: str) -> dict[str, Any]:
        if not self.settings.account_connect_url:
            raise AccountConnectConfigurationError(
                "BOSS_ACCOUNT_CONNECT_URL is required before hosted Account Connect checks."
            )
        base_url = self.settings.account_connect_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "User-Agent": "boss-control-plane/0.3",
        }
        if self.settings.account_connect_token:
            headers["Authorization"] = f"Bearer {self.settings.account_connect_token}"
        request = Request(f"{base_url}{path}", headers=headers, method="GET")
        try:
            with urlopen(  # noqa: S310 - configured trusted Account Connect endpoint.
                request,
                timeout=self.settings.account_connect_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            raise AccountConnectSyncError(
                f"Account Connect check failed with HTTP {error.code}."
            ) from error
        except URLError as error:
            raise AccountConnectSyncError(
                f"Account Connect check failed: {error.reason}."
            ) from error
        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise AccountConnectSyncError(
                "Account Connect check returned invalid JSON."
            ) from error
        if not isinstance(decoded, dict):
            raise AccountConnectSyncError(
                "Account Connect check response must be a JSON object."
            )
        return decoded


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
        self.receipts = ReceiptService()

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
        # Paid editions extend Community rather than replacing its baseline
        # capabilities. The pack declares that relationship via
        # ``requires: [community_edition]``; make it operational so a paid
        # edition can still install the default Community agent modules.
        if "community_edition" in (payload.get("requires") or []):
            community = yaml.safe_load(
                self._pack_path("community").read_text(encoding="utf-8")
            )
            inherited = (
                community.get("includes", []) if isinstance(community, dict) else []
            )
            payload["includes"] = list(
                dict.fromkeys([*inherited, *(payload.get("includes") or [])])
            )
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

    def _project_scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            return {
                "org_id": self.settings.default_org_id,
                "workspace_id": self.settings.default_workspace_id,
            }
        return {"org_id": row["org_id"], "workspace_id": row["workspace_id"]}

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
                UPDATE account_connect_orgs
                SET status = 'unlinked',
                    unlinked_by = :actor,
                    unlinked_at = now(),
                    metadata = metadata || CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id
                  AND status = 'linked'
                  AND hosted_org_id <> :hosted_org_id
                """
            ),
            {
                "project_id": project_id,
                "hosted_org_id": hosted_org_id,
                "actor": actor,
                "metadata": json.dumps(
                    {
                        "unlink_reason": "account_connect_link_superseded",
                        "superseded_by_hosted_org_id": hosted_org_id,
                    }
                ),
            },
        )
        connection.execute(
            text(
                """
                UPDATE license_entitlements
                SET status = 'detached',
                    metadata = metadata || CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id
                  AND source IN ('account_connect', 'account_connect_hosted')
                """
            ),
            {
                "project_id": project_id,
                "metadata": json.dumps(
                    {
                        "detached_reason": "account_connect_link_superseded",
                        "superseded_by_hosted_org_id": hosted_org_id,
                    }
                ),
            },
        )
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
                WHERE project_id = :project_id
                  AND status = 'linked'
                """
            ),
            {
                "project_id": project_id,
                "actor": actor,
                "metadata": json.dumps({"unlink_reason": reason or "operator_unlinked"}),
            },
        )
        connection.execute(
            text(
                """
                UPDATE license_entitlements
                SET status = 'detached',
                    metadata = metadata || CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id
                  AND source IN ('account_connect', 'account_connect_hosted')
                """
            ),
            {
                "project_id": project_id,
                "metadata": json.dumps({"detached_reason": reason or "operator_unlinked"}),
            },
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
        synced_capability_keys: set[str] = set()
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
            source = "account_connect_hosted"
            hosted_entitlement_id = str(entitlement.get("id") or "").strip()
            entitlement_id = f"lic_{project_id}_hosted_{capability_key}"
            metadata = {
                "hosted_org_id": active["hosted_org_id"],
                "hosted_entitlement_id": hosted_entitlement_id or None,
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
            synced_capability_keys.add(capability_key)
        stale_rows = connection.execute(
            text(
                """
                SELECT id, capability_key
                FROM license_entitlements
                WHERE project_id = :project_id
                  AND source = 'account_connect_hosted'
                  AND account_connect_org_id = :account_connect_org_id
                  AND status IN ('active', 'trial')
                """
            ),
            {
                "project_id": project_id,
                "account_connect_org_id": active["hosted_org_id"],
            },
        ).mappings().all()
        for row in stale_rows:
            if row["capability_key"] in synced_capability_keys:
                continue
            connection.execute(
                text(
                    """
                    UPDATE license_entitlements
                    SET status = 'detached',
                        metadata = metadata || CAST(:metadata AS JSONB),
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": row["id"],
                    "metadata": json.dumps(
                        {"detached_reason": "account_connect_sync_missing"}
                    ),
                },
            )
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

    def create_auth_session(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str,
        return_url: Optional[str] = None,
        hosted_org_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        active = self.active_account_connect_org(connection, project_id)
        if active and hosted_org_id and active["hosted_org_id"] != hosted_org_id:
            emit_event(
                connection,
                project_id,
                "account_connect.auth_session_denied",
                actor,
                "account_connect_auth_session",
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
        session_id = make_id("acctauth")
        state = make_id("acctstate")
        metadata_payload = metadata or {}
        dedupe_payload = {
            "hosted_org_id": effective_hosted_org_id,
            "return_url": return_url,
            "created_by": actor,
            "metadata": metadata_payload,
        }
        receipt, created = self._create_account_connect_receipt(
            connection,
            project_id,
            dedupe_key=(
                f"account-connect-auth-session-create:{project_id}:"
                f"{json_hash(dedupe_payload)}"
            ),
            effect_kind="account_connect.auth_session.create",
            effect_scope={
                "hosted_org_id": effective_hosted_org_id,
                "data_boundary": "confidential",
            },
            payload=dedupe_payload,
        )
        if not created and receipt.get("status") == "succeeded":
            existing = self._auth_session_by_receipt(
                connection, project_id, receipt["id"]
            )
            if existing:
                return existing

        authorization_url = None
        provider_ref = None
        expires_at = None
        status = "ready"
        event_type = "account_connect.auth_session_created"
        record_metadata = dict(metadata_payload)
        try:
            hosted = self.account_connect_adapter.create_auth_session(
                project_id=project_id,
                return_url=return_url,
                state=state,
                metadata=metadata_payload,
                hosted_org_id=effective_hosted_org_id,
            )
            authorization_url = str(hosted["authorization_url"])
            provider_ref = (
                str(hosted.get("id") or hosted.get("session_id"))
                if hosted.get("id") or hosted.get("session_id")
                else None
            )
            hosted_org_response = hosted.get("hosted_org_id")
            if isinstance(hosted_org_response, str) and hosted_org_response.strip():
                effective_hosted_org_id = hosted_org_response.strip()
            expires_at = hosted.get("expires_at")
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref,
                {
                    "provider_ref": provider_ref,
                    "authorization_url_present": True,
                    "hosted_org_id": effective_hosted_org_id,
                    "expires_at": expires_at,
                },
            )
        except AccountConnectConfigurationError as error:
            status = "blocked_configuration"
            event_type = "account_connect.auth_session_blocked"
            record_metadata["blocked_reason"] = "account_connect_url_missing"
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
        except AccountConnectSyncError as error:
            status = "failed"
            event_type = "account_connect.auth_session_failed"
            record_metadata["failure_reason"] = str(error)
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})

        connection.execute(
            text(
                """
                INSERT INTO account_connect_auth_sessions (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    authorization_url, return_url, provider_ref, state, status,
                    receipt_id, created_by, expires_at, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :authorization_url, :return_url, :provider_ref, :state,
                    :status, :receipt_id, :created_by,
                    CAST(:expires_at AS TIMESTAMPTZ), CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": session_id,
                "project_id": project_id,
                "account_connect_org_id": active["id"] if active else None,
                "hosted_org_id": effective_hosted_org_id,
                "authorization_url": authorization_url,
                "return_url": return_url,
                "provider_ref": provider_ref,
                "state": state,
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
            "account_connect_auth_session",
            session_id,
            {
                "hosted_org_id": effective_hosted_org_id,
                "status": status,
                "receipt_id": receipt["id"],
                "authorization_url_present": bool(authorization_url),
            },
        )
        created_row = self._auth_session_by_id(connection, session_id)
        if not created_row:
            raise AccountConnectSyncError(
                "Account Connect auth session record was not saved."
            )
        return created_row

    def complete_auth_session(
        self,
        connection: Connection,
        session_id: str,
        *,
        project_id: str,
        actor: str,
        code: str,
        state: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        session = self._auth_session_by_id(connection, session_id)
        if not session or session["project_id"] != project_id:
            raise AccountConnectAuthSessionNotFoundError(
                f"Account Connect auth session '{session_id}' was not found."
            )
        if session["status"] == "completed":
            return session
        if session["state"] != state:
            emit_event(
                connection,
                project_id,
                "account_connect.auth_session_denied",
                actor,
                "account_connect_auth_session",
                session_id,
                {"reason": "state_mismatch", "status": session["status"]},
            )
            raise AccountConnectAuthSessionStateError(
                "Account Connect auth session state did not match."
            )

        metadata_payload = metadata or {}
        provider_ref = session.get("provider_ref") or session_id
        receipt, _created = self._create_account_connect_receipt(
            connection,
            project_id,
            dedupe_key=f"account-connect-auth-session-complete:{session_id}",
            effect_kind="account_connect.auth_session.complete",
            effect_scope={
                "hosted_org_id": session.get("hosted_org_id"),
                "provider_ref": provider_ref,
                "data_boundary": "confidential",
            },
            payload={
                "session_id": session_id,
                "state": state,
                "code_hash": json_hash({"code": code}),
                "metadata": metadata_payload,
            },
        )

        status = "completed"
        event_type = "account_connect.auth_session_completed"
        record_metadata = dict(session.get("metadata") or {})
        record_metadata.update(metadata_payload)
        mark_completed = True
        account_connect_org_id = session.get("account_connect_org_id")
        hosted_org_id = session.get("hosted_org_id")
        try:
            hosted = self.account_connect_adapter.complete_auth_session(
                project_id=project_id,
                provider_ref=provider_ref,
                code=code,
                state=state,
                metadata=metadata_payload,
                hosted_org_id=hosted_org_id,
            )
            hosted_org_id = str(hosted["hosted_org_id"]).strip()
            marketplace_identity = hosted.get("marketplace_identity")
            connector_authorization_refs = hosted.get("connector_authorization_refs")
            if not isinstance(marketplace_identity, dict):
                marketplace_identity = {}
            if not isinstance(connector_authorization_refs, list):
                connector_authorization_refs = []
            edition = self.link_account_connect_org(
                connection,
                project_id,
                hosted_org_id=hosted_org_id,
                actor=actor,
                connection_ref=hosted.get("connection_ref")
                if isinstance(hosted.get("connection_ref"), str)
                else None,
                marketplace_identity=marketplace_identity,
                billing_portal_ref=hosted.get("billing_portal_ref")
                if isinstance(hosted.get("billing_portal_ref"), str)
                else None,
                connector_authorization_refs=[
                    str(item)
                    for item in connector_authorization_refs
                    if isinstance(item, str) and item.strip()
                ],
                optional_telemetry_enabled=bool(
                    hosted.get("optional_telemetry_enabled", False)
                ),
                metadata={
                    "source": "hosted_auth_session",
                    "auth_session_id": session_id,
                    **(hosted.get("metadata") if isinstance(hosted.get("metadata"), dict) else {}),
                },
            )
            active = edition.get("account_connect_org")
            account_connect_org_id = active["id"] if active else account_connect_org_id
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref,
                {
                    "provider_ref": provider_ref,
                    "hosted_org_id": hosted_org_id,
                    "linked": bool(active),
                },
            )
        except AccountConnectConfigurationError as error:
            status = "blocked_configuration"
            event_type = "account_connect.auth_session_blocked"
            mark_completed = False
            record_metadata["blocked_reason"] = "account_connect_url_missing"
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
        except AccountConnectSyncError as error:
            status = "failed"
            event_type = "account_connect.auth_session_failed"
            mark_completed = False
            record_metadata["failure_reason"] = str(error)
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})

        connection.execute(
            text(
                """
                UPDATE account_connect_auth_sessions
                SET account_connect_org_id = :account_connect_org_id,
                    hosted_org_id = :hosted_org_id,
                    status = :status,
                    receipt_id = :receipt_id,
                    completed_at = CASE
                      WHEN :mark_completed THEN now()
                      ELSE completed_at
                    END,
                    metadata = CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "id": session_id,
                "account_connect_org_id": account_connect_org_id,
                "hosted_org_id": hosted_org_id,
                "status": status,
                "receipt_id": receipt["id"],
                "mark_completed": mark_completed,
                "metadata": json.dumps(record_metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "account_connect_auth_session",
            session_id,
            {
                "hosted_org_id": hosted_org_id,
                "status": status,
                "receipt_id": receipt["id"],
            },
        )
        updated = self._auth_session_by_id(connection, session_id)
        if not updated:
            raise AccountConnectSyncError(
                "Account Connect auth session record was not saved."
            )
        return updated

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

    def _create_account_connect_receipt(
        self,
        connection: Connection,
        project_id: str,
        *,
        dedupe_key: str,
        effect_kind: str,
        effect_scope: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        scope = self._project_scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            effect_kind,
            effect_scope,
            payload,
            data_boundary=effect_scope.get("data_boundary", "confidential"),
        )
        return receipt, created

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

    def _auth_session_by_id(
        self, connection: Connection, session_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       authorization_url, return_url, provider_ref, state, status,
                       receipt_id, created_by, expires_at, completed_at, metadata,
                       created_at, updated_at
                FROM account_connect_auth_sessions
                WHERE id = :id
                """
            ),
            {"id": session_id},
        ).mappings().first()
        return self._auth_session_from_row(dict(row)) if row else None

    def _auth_session_by_receipt(
        self, connection: Connection, project_id: str, receipt_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, account_connect_org_id, hosted_org_id,
                       authorization_url, return_url, provider_ref, state, status,
                       receipt_id, created_by, expires_at, completed_at, metadata,
                       created_at, updated_at
                FROM account_connect_auth_sessions
                WHERE project_id = :project_id AND receipt_id = :receipt_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "receipt_id": receipt_id},
        ).mappings().first()
        return self._auth_session_from_row(dict(row)) if row else None

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

    def _auth_session_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "expires_at": row["expires_at"].isoformat()
            if row.get("expires_at") is not None
            else None,
            "completed_at": row["completed_at"].isoformat()
            if row.get("completed_at") is not None
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
