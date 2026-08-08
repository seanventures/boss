from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    json_hash,
    make_id,
    now_utc,
)
from .members import MemberDirectoryService
from .receipts import ReceiptService


class EnterpriseSupportConfigurationError(RuntimeError):
    pass


class EnterpriseSupportEscalationError(RuntimeError):
    pass


class EnterpriseSupportAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def create_hosted_case(
        self,
        *,
        hosted_org_id: str,
        project_id: str,
        title: str,
        severity: str,
        category: str,
        description: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.settings.account_connect_url:
            raise EnterpriseSupportConfigurationError(
                "BOSS_ACCOUNT_CONNECT_URL is required before hosted support escalation."
            )
        base_url = self.settings.account_connect_url.rstrip("/")
        url = (
            f"{base_url}/v1/orgs/{quote(hosted_org_id, safe='')}"
            "/support-cases"
        )
        payload = json.dumps(
            {
                "project_id": project_id,
                "title": title,
                "severity": severity,
                "category": category,
                "description": description,
                "metadata": metadata,
            }
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "boss-control-plane/0.4",
        }
        if self.settings.account_connect_token:
            headers["Authorization"] = f"Bearer {self.settings.account_connect_token}"
        request = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(  # noqa: S310 - configured trusted Account Connect endpoint.
                request,
                timeout=self.settings.account_connect_timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            raise EnterpriseSupportEscalationError(
                f"Hosted support escalation failed with HTTP {error.code}."
            ) from error
        except URLError as error:
            raise EnterpriseSupportEscalationError(
                f"Hosted support escalation failed: {error.reason}."
            ) from error
        try:
            response_payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise EnterpriseSupportEscalationError(
                "Hosted support escalation returned invalid JSON."
            ) from error
        case_ref = response_payload.get("id") or response_payload.get("case_id")
        if not isinstance(case_ref, str) or not case_ref.strip():
            raise EnterpriseSupportEscalationError(
                "Hosted support escalation response did not include a case id."
            )
        return response_payload


class EnterpriseService:
    DEPLOYMENT_MODES = {
        "enterprise_cloud",
        "self_hosted",
        "private_cloud",
        "air_gapped",
    }
    ACCOUNT_CONNECT_MODES = {"attached", "detached"}
    IDENTITY_PROVIDER_TYPES = {"oidc", "saml", "scim"}
    IDENTITY_PROVIDER_STATUSES = {"planned", "active", "blocked", "disabled"}
    IDENTITY_ROLLOUT_MODES = {
        "disabled",
        "metadata_only",
        "auth_allowed",
        "provisioning_allowed",
    }
    CLUSTER_VALIDATION_STATUSES = {"passed", "warning", "failed", "blocked"}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.members = MemberDirectoryService(settings)
        self.receipts = ReceiptService()
        self.support_adapter = EnterpriseSupportAdapter(settings)

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

    def list_identity_providers(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, provider_key, provider_type,
                       display_name, issuer, audience, entity_id, sso_url,
                       jwks_url, certificate_fingerprint, scim_base_url,
                       secret_ref, status, test_status, blocking_gaps,
                       metadata, created_by, created_at, updated_at
                FROM enterprise_identity_providers
                WHERE project_id = :project_id
                ORDER BY provider_type ASC, display_name ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._identity_provider_from_row(dict(row)) for row in rows]

    def list_identity_rollout_policies(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, provider_type, runtime_mode,
                       allowed_domains, rollout_notes, status, created_by,
                       metadata, created_at, updated_at
                FROM enterprise_identity_rollout_policies
                WHERE project_id = :project_id
                ORDER BY provider_type ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._identity_rollout_policy_from_row(dict(row)) for row in rows]

    def upsert_identity_rollout_policy(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider_type: str,
        runtime_mode: str,
        actor: str,
        allowed_domains: Optional[list[str]] = None,
        rollout_notes: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        org_id = self._project_org_id(connection, project_id)
        normalized_type = provider_type.strip().lower()
        normalized_mode = runtime_mode.strip().lower()
        if normalized_type not in self.IDENTITY_PROVIDER_TYPES:
            raise ValueError(f"Unsupported identity provider type: {provider_type}")
        if normalized_mode not in self.IDENTITY_ROLLOUT_MODES:
            raise ValueError(f"Unsupported identity rollout mode: {runtime_mode}")
        domains = self._normalize_tokens(allowed_domains or [])
        policy_id = f"entidpol_{project_id}_{normalized_type}"
        connection.execute(
            text(
                """
                INSERT INTO enterprise_identity_rollout_policies (
                    id, org_id, project_id, provider_type, runtime_mode,
                    allowed_domains, rollout_notes, status, created_by, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :provider_type, :runtime_mode,
                    CAST(:allowed_domains AS JSONB), :rollout_notes, 'active',
                    :created_by, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, provider_type) DO UPDATE
                SET runtime_mode = EXCLUDED.runtime_mode,
                    allowed_domains = EXCLUDED.allowed_domains,
                    rollout_notes = EXCLUDED.rollout_notes,
                    status = 'active',
                    created_by = EXCLUDED.created_by,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": policy_id,
                "org_id": org_id,
                "project_id": project_id,
                "provider_type": normalized_type,
                "runtime_mode": normalized_mode,
                "allowed_domains": json.dumps(domains),
                "rollout_notes": rollout_notes,
                "created_by": actor,
                "metadata": json.dumps(metadata or {}),
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.identity_rollout_policy_upserted",
            actor,
            "enterprise_identity_rollout_policy",
            policy_id,
            {
                "provider_type": normalized_type,
                "runtime_mode": normalized_mode,
                "allowed_domains": domains,
            },
        )
        return self._get_identity_rollout_policy(connection, project_id, normalized_type)

    def identity_rollout_status(
        self,
        connection: Connection,
        project_id: str,
        provider_type: str,
        required_mode: str,
    ) -> dict[str, Any]:
        normalized_type = provider_type.strip().lower()
        normalized_required_mode = required_mode.strip().lower()
        policy = self._get_identity_rollout_policy(
            connection, project_id, normalized_type
        )
        if not policy:
            return {"enabled": False, "reason": "identity_rollout_policy_missing"}
        if policy["status"] != "active":
            return {
                "enabled": False,
                "reason": "identity_rollout_policy_inactive",
                "policy": policy,
            }
        allowed_modes = {
            "metadata_only": {"metadata_only", "auth_allowed", "provisioning_allowed"},
            "auth_allowed": {"auth_allowed"},
            "provisioning_allowed": {"provisioning_allowed"},
        }.get(normalized_required_mode, {normalized_required_mode})
        if policy["runtime_mode"] not in allowed_modes:
            return {
                "enabled": False,
                "reason": "identity_rollout_policy_mode_blocked",
                "policy": policy,
            }
        return {"enabled": True, "policy": policy}

    def upsert_identity_provider(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider_key: str,
        provider_type: str,
        display_name: str,
        actor: str,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
        entity_id: Optional[str] = None,
        sso_url: Optional[str] = None,
        jwks_url: Optional[str] = None,
        certificate_fingerprint: Optional[str] = None,
        scim_base_url: Optional[str] = None,
        secret_ref: Optional[str] = None,
        status: str = "planned",
        test_status: str = "not_tested",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        org_id = self._project_org_id(connection, project_id)
        normalized_key = self._id_token(provider_key)
        normalized_type = provider_type.strip().lower()
        normalized_display_name = display_name.strip()
        normalized_status = status.strip().lower()
        if not normalized_key:
            raise ValueError("identity provider provider_key must not be blank.")
        if normalized_type not in self.IDENTITY_PROVIDER_TYPES:
            raise ValueError(f"Unsupported identity provider type: {provider_type}")
        if not normalized_display_name:
            raise ValueError("identity provider display_name must not be blank.")
        if normalized_status not in self.IDENTITY_PROVIDER_STATUSES:
            raise ValueError(f"Unsupported identity provider status: {status}")
        gaps = self._identity_provider_gaps(
            provider_type=normalized_type,
            issuer=issuer,
            audience=audience,
            entity_id=entity_id,
            sso_url=sso_url,
            jwks_url=jwks_url,
            certificate_fingerprint=certificate_fingerprint,
            scim_base_url=scim_base_url,
            secret_ref=secret_ref,
        )
        effective_status = (
            "blocked" if normalized_status == "active" and gaps else normalized_status
        )
        provider_id = f"entidp_{project_id}_{normalized_key}"
        connection.execute(
            text(
                """
                INSERT INTO enterprise_identity_providers (
                    id, org_id, project_id, provider_key, provider_type,
                    display_name, issuer, audience, entity_id, sso_url,
                    jwks_url, certificate_fingerprint, scim_base_url,
                    secret_ref, status, test_status, blocking_gaps,
                    metadata, created_by
                )
                VALUES (
                    :id, :org_id, :project_id, :provider_key, :provider_type,
                    :display_name, :issuer, :audience, :entity_id, :sso_url,
                    :jwks_url, :certificate_fingerprint, :scim_base_url,
                    :secret_ref, :status, :test_status,
                    CAST(:blocking_gaps AS JSONB), CAST(:metadata AS JSONB),
                    :created_by
                )
                ON CONFLICT (project_id, provider_key) DO UPDATE
                SET provider_type = EXCLUDED.provider_type,
                    display_name = EXCLUDED.display_name,
                    issuer = EXCLUDED.issuer,
                    audience = EXCLUDED.audience,
                    entity_id = EXCLUDED.entity_id,
                    sso_url = EXCLUDED.sso_url,
                    jwks_url = EXCLUDED.jwks_url,
                    certificate_fingerprint = EXCLUDED.certificate_fingerprint,
                    scim_base_url = EXCLUDED.scim_base_url,
                    secret_ref = EXCLUDED.secret_ref,
                    status = EXCLUDED.status,
                    test_status = EXCLUDED.test_status,
                    blocking_gaps = EXCLUDED.blocking_gaps,
                    metadata = EXCLUDED.metadata,
                    created_by = EXCLUDED.created_by,
                    updated_at = now()
                """
            ),
            {
                "id": provider_id,
                "org_id": org_id,
                "project_id": project_id,
                "provider_key": normalized_key,
                "provider_type": normalized_type,
                "display_name": normalized_display_name,
                "issuer": self._optional_text(issuer),
                "audience": self._optional_text(audience),
                "entity_id": self._optional_text(entity_id),
                "sso_url": self._optional_text(sso_url),
                "jwks_url": self._optional_text(jwks_url),
                "certificate_fingerprint": self._optional_text(
                    certificate_fingerprint
                ),
                "scim_base_url": self._optional_text(scim_base_url),
                "secret_ref": self._optional_text(secret_ref),
                "status": effective_status,
                "test_status": test_status.strip() if test_status.strip() else "not_tested",
                "blocking_gaps": json.dumps(gaps),
                "metadata": json.dumps(metadata or {}),
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.identity_provider_upserted",
            actor,
            "enterprise_identity_provider",
            provider_id,
            {
                "provider_key": normalized_key,
                "provider_type": normalized_type,
                "status": effective_status,
                "blocking_gaps": gaps,
            },
        )
        return self._get_identity_provider(connection, project_id, normalized_key)

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

    def scim_runtime_status(
        self,
        connection: Connection,
        project_id: str,
    ) -> dict[str, Any]:
        provider = connection.execute(
            text(
                """
                SELECT id, provider_key, display_name, scim_base_url, secret_ref
                FROM enterprise_identity_providers
                WHERE project_id = :project_id
                  AND provider_type = 'scim'
                  AND status = 'active'
                  AND jsonb_array_length(blocking_gaps) = 0
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not provider:
            return {
                "enabled": False,
                "reason": "active_scim_identity_provider_missing",
            }
        rollout = self.identity_rollout_status(
            connection,
            project_id,
            "scim",
            "provisioning_allowed",
        )
        if not rollout["enabled"]:
            return {
                "enabled": False,
                "reason": rollout["reason"],
                "provider": dict(provider),
                "policy": rollout.get("policy"),
            }
        return {
            "enabled": True,
            "provider": dict(provider),
            "policy": rollout.get("policy"),
        }

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
        member_id = f"scim_{project_id}_{self._id_token(normalized_user_name)}"
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
            # Older BOSS versions mirrored every project member into
            # org_members. MemberDirectory deliberately falls back to an active
            # org member when the project row is inactive, so only disabling the
            # project row leaves a deprovisioned SCIM user authorized and visible.
            # Retain both rows for audit history, but disable the matching
            # org-level mirror as part of the same deprovisioning transaction.
            connection.execute(
                text(
                    """
                    UPDATE org_members
                    SET status = 'inactive', updated_at = now()
                    WHERE org_id = :org_id AND member_id = :member_id
                    """
                ),
                {"org_id": org_id, "member_id": member_id},
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

    def get_scim_group_by_identifier(
        self,
        connection: Connection,
        project_id: str,
        identifier: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, external_id, display_name,
                       role_keys, permissions, raw_group, status, created_by,
                       created_at, updated_at
                FROM enterprise_scim_groups
                WHERE project_id = :project_id
                  AND (
                    id = :identifier
                    OR external_id = :identifier
                    OR display_name = :identifier
                  )
                """
            ),
            {"project_id": project_id, "identifier": identifier},
        ).mappings().first()
        if row is None:
            raise ValueError(f"SCIM group '{identifier}' was not found.")
        return self._scim_group_from_row(dict(row))

    def get_scim_user_by_identifier(
        self,
        connection: Connection,
        project_id: str,
        identifier: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, external_id, user_name,
                       display_name, member_id, role_keys, permissions, groups,
                       raw_profile, status, created_by, created_at, updated_at
                FROM enterprise_scim_users
                WHERE project_id = :project_id
                  AND (
                    id = :identifier
                    OR external_id = :identifier
                    OR user_name = :identifier
                    OR member_id = :identifier
                  )
                """
            ),
            {"project_id": project_id, "identifier": identifier.lower()},
        ).mappings().first()
        if row is None:
            raise ValueError(f"SCIM user '{identifier}' was not found.")
        return self._scim_user_from_row(dict(row))

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

    def list_cluster_validation_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        enterprise_cluster_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if enterprise_cluster_id:
            rows = connection.execute(
                text(
                    """
                    SELECT id, org_id, project_id, enterprise_cluster_id,
                           validation_kind, status, checks, evidence_refs,
                           blocking_gaps, validated_by, metadata, created_at
                    FROM enterprise_cluster_validation_runs
                    WHERE project_id = :project_id
                      AND enterprise_cluster_id = :enterprise_cluster_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {
                    "project_id": project_id,
                    "enterprise_cluster_id": enterprise_cluster_id,
                    "limit": limit,
                },
            ).mappings().all()
            return [self._cluster_validation_run_from_row(dict(row)) for row in rows]

        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, enterprise_cluster_id,
                       validation_kind, status, checks, evidence_refs,
                       blocking_gaps, validated_by, metadata, created_at
                FROM enterprise_cluster_validation_runs
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._cluster_validation_run_from_row(dict(row)) for row in rows]

    def record_cluster_validation_run(
        self,
        connection: Connection,
        project_id: str,
        *,
        enterprise_cluster_id: str,
        validation_kind: str,
        status: str,
        checks: list[dict[str, Any]],
        evidence_refs: Optional[list[str]],
        actor: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        cluster = self.get_cluster(connection, enterprise_cluster_id, project_id)
        normalized_kind = self._id_token(validation_kind)
        if not normalized_kind:
            raise ValueError("validation_kind must not be blank.")
        normalized_status = self._id_token(status)
        if normalized_status not in self.CLUSTER_VALIDATION_STATUSES:
            raise ValueError(f"Unsupported cluster validation status: {status}")
        normalized_checks = self._normalize_cluster_validation_checks(checks)
        refs = self._normalize_tokens(evidence_refs or [])
        blocking_gaps = self._cluster_validation_blocking_gaps(normalized_checks)
        effective_status = (
            "blocked"
            if blocking_gaps and normalized_status in {"passed", "warning"}
            else normalized_status
        )
        run_id = make_id("entclval")
        connection.execute(
            text(
                """
                INSERT INTO enterprise_cluster_validation_runs (
                    id, org_id, project_id, enterprise_cluster_id,
                    validation_kind, status, checks, evidence_refs,
                    blocking_gaps, validated_by, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :enterprise_cluster_id,
                    :validation_kind, :status, CAST(:checks AS JSONB),
                    CAST(:evidence_refs AS JSONB),
                    CAST(:blocking_gaps AS JSONB), :validated_by,
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": run_id,
                "org_id": cluster["org_id"],
                "project_id": project_id,
                "enterprise_cluster_id": enterprise_cluster_id,
                "validation_kind": normalized_kind,
                "status": effective_status,
                "checks": json.dumps(normalized_checks),
                "evidence_refs": json.dumps(refs),
                "blocking_gaps": json.dumps(blocking_gaps),
                "validated_by": actor,
                "metadata": json.dumps(metadata or {}),
            },
        )
        validation_packet = {
            "object_type": "enterprise_cluster_validation_packet",
            "enterprise_cluster_id": enterprise_cluster_id,
            "validation_run_id": run_id,
            "validation_kind": normalized_kind,
            "status": effective_status,
            "checks": normalized_checks,
            "evidence_refs": refs,
            "blocking_gaps": blocking_gaps,
            "validated_by": actor,
            "live_rollout_claim": False,
            "cluster": {
                "deployment_mode": cluster["deployment_mode"],
                "account_connect_mode": cluster["account_connect_mode"],
                "worker_pool_strategy": cluster["worker_pool_strategy"],
                "ha_profile": cluster["ha_profile"],
                "data_residency": cluster["data_residency"],
                "support_tier": cluster["support_tier"],
                "status": cluster["status"],
            },
            "metadata": metadata or {},
            "data_boundary": "internal",
        }
        validation_packet_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "enterprise_cluster_validation_packet",
            f"Enterprise cluster validation: {normalized_kind}",
            validation_packet,
            data_boundary="internal",
        )
        blocking_decision_id = None
        if blocking_gaps:
            blocking_decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Resolve enterprise cluster validation blockers for {normalized_kind}",
                {
                    "action": "resolve_cluster_validation_blockers",
                    "enterprise_cluster_id": enterprise_cluster_id,
                    "validation_run_id": run_id,
                    "validation_packet_artifact_id": validation_packet_artifact_id,
                    "blocking_gaps": blocking_gaps,
                    "reason": (
                        "Required enterprise cluster validation checks did not pass; "
                        "BOSS must hold rollout-sensitive work until the gaps are resolved."
                    ),
                },
                {
                    "estimated_cost_micros": 0,
                    "currency": "USD",
                    "confidence": "unknown",
                    "reason": "Cluster validation remediation is human/configuration work until a ChangeSet is proposed.",
                },
                "enterprise_cluster_validation_blocked",
                risk_tier=3,
                default_action="hold",
            )
        enriched_metadata = {
            **(metadata or {}),
            "validation_packet_artifact_id": validation_packet_artifact_id,
            "blocking_decision_id": blocking_decision_id,
            "live_rollout_claim": False,
        }
        connection.execute(
            text(
                """
                UPDATE enterprise_cluster_validation_runs
                SET metadata = CAST(:metadata AS JSONB)
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "metadata": json.dumps(enriched_metadata),
            },
        )
        connection.execute(
            text(
                """
                UPDATE enterprise_clusters
                SET metadata = metadata || CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {
                "id": enterprise_cluster_id,
                "project_id": project_id,
                "metadata": json.dumps(
                    {
                        "latest_validation": {
                            "id": run_id,
                            "validation_kind": normalized_kind,
                            "status": effective_status,
                            "blocking_gaps": blocking_gaps,
                            "validation_packet_artifact_id": validation_packet_artifact_id,
                            "blocking_decision_id": blocking_decision_id,
                            "live_rollout_claim": False,
                        }
                    }
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.cluster_validation_recorded",
            actor,
            "enterprise_cluster_validation_run",
            run_id,
            {
                "enterprise_cluster_id": enterprise_cluster_id,
                "validation_kind": normalized_kind,
                "status": effective_status,
                "blocking_gaps": blocking_gaps,
                "validation_packet_artifact_id": validation_packet_artifact_id,
                "blocking_decision_id": blocking_decision_id,
                "live_rollout_claim": False,
            },
        )
        return self._get_cluster_validation_run(connection, project_id, run_id)

    def list_support_cases(
        self,
        connection: Connection,
        project_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, enterprise_cluster_id,
                       account_connect_org_id, hosted_org_id, title, severity,
                       category, description, status, sla_due_at,
                       response_time_hours, hosted_case_ref, receipt_id,
                       assigned_to, created_by, metadata, created_at, updated_at
                FROM enterprise_support_cases
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._support_case_from_row(dict(row)) for row in rows]

    def create_support_case(
        self,
        connection: Connection,
        project_id: str,
        *,
        title: str,
        description: str,
        actor: str,
        severity: str = "normal",
        category: str = "support",
        enterprise_cluster_id: Optional[str] = None,
        escalate_to_hosted: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        cleaned_title = self._required_text(title, "support case title")
        cleaned_description = self._required_text(
            description, "support case description"
        )
        normalized_severity = self._id_token(severity)
        normalized_category = self._id_token(category)
        if normalized_severity not in {"critical", "high", "normal", "low"}:
            raise ValueError(f"Unsupported support severity: {severity}")
        scope = self._project_scope(connection, project_id)
        cluster = self._active_or_requested_cluster(
            connection, project_id, enterprise_cluster_id
        )
        response_hours = self._support_response_hours(cluster, normalized_severity)
        sla_due_at = now_utc() + timedelta(hours=response_hours)
        active_account = self._active_account_connect_org(connection, project_id)
        case_id = make_id("entsup")
        metadata_payload = {
            **(metadata or {}),
            "escalate_to_hosted": escalate_to_hosted,
            "support_tier": cluster.get("support_tier") if cluster else "community",
        }
        receipt_id = None
        hosted_case_ref = None
        hosted_org_id = active_account.get("hosted_org_id") if active_account else None
        account_connect_org_id = active_account.get("id") if active_account else None
        status = "open"
        event_type = "enterprise.support_case_created"

        if escalate_to_hosted:
            dedupe_payload = {
                "project_id": project_id,
                "title": cleaned_title,
                "severity": normalized_severity,
                "category": normalized_category,
                "description": cleaned_description,
                "hosted_org_id": hosted_org_id,
            }
            receipt, created = self.receipts.create_or_reuse(
                connection,
                scope["org_id"],
                scope["workspace_id"],
                project_id,
                f"enterprise-support-case:{project_id}:{json_hash(dedupe_payload)}",
                "enterprise.support_case.create",
                {
                    "hosted_org_id": hosted_org_id,
                    "severity": normalized_severity,
                    "data_boundary": "confidential",
                },
                dedupe_payload,
                data_boundary="confidential",
            )
            receipt_id = receipt["id"]
            if not created and receipt.get("status") == "succeeded":
                existing = self._support_case_by_receipt(
                    connection, project_id, receipt["id"]
                )
                if existing:
                    return existing
            if not active_account:
                status = "blocked_configuration"
                event_type = "enterprise.support_case_blocked"
                metadata_payload["blocked_reason"] = "account_connect_org_missing"
                self.receipts.mark_failed(
                    connection,
                    receipt["id"],
                    {"message": "No active Account Connect org is linked."},
                )
            else:
                try:
                    hosted = self.support_adapter.create_hosted_case(
                        hosted_org_id=active_account["hosted_org_id"],
                        project_id=project_id,
                        title=cleaned_title,
                        severity=normalized_severity,
                        category=normalized_category,
                        description=cleaned_description,
                        metadata=metadata_payload,
                    )
                    hosted_case_ref = str(hosted.get("id") or hosted.get("case_id"))
                    status = str(hosted.get("status") or "hosted_submitted")
                    event_type = "enterprise.support_case_escalated"
                    metadata_payload["hosted_status"] = status
                    self.receipts.mark_succeeded(
                        connection,
                        receipt["id"],
                        hosted_case_ref,
                        {
                            "hosted_case_ref": hosted_case_ref,
                            "status": status,
                        },
                    )
                except EnterpriseSupportConfigurationError as error:
                    status = "blocked_configuration"
                    event_type = "enterprise.support_case_blocked"
                    metadata_payload["blocked_reason"] = "account_connect_url_missing"
                    self.receipts.mark_failed(
                        connection, receipt["id"], {"message": str(error)}
                    )
                except EnterpriseSupportEscalationError as error:
                    status = "failed"
                    event_type = "enterprise.support_case_failed"
                    metadata_payload["failure_reason"] = str(error)
                    self.receipts.mark_failed(
                        connection, receipt["id"], {"message": str(error)}
                    )

        support_packet_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "enterprise_support_case_packet",
            f"Enterprise support case: {cleaned_title}",
            {
                "object_type": "enterprise_support_case_packet",
                "support_case_id": case_id,
                "project_id": project_id,
                "enterprise_cluster_id": cluster.get("id") if cluster else None,
                "account_connect_org_id": account_connect_org_id,
                "hosted_org_id": hosted_org_id,
                "title": cleaned_title,
                "severity": normalized_severity,
                "category": normalized_category,
                "description": cleaned_description,
                "status": status,
                "sla_due_at": sla_due_at.isoformat(),
                "response_time_hours": response_hours,
                "hosted_case_ref": hosted_case_ref,
                "receipt_id": receipt_id,
                "created_by": actor,
                "escalate_to_hosted": escalate_to_hosted,
                "hosted_support_claim": bool(hosted_case_ref),
                "metadata": metadata_payload,
                "data_boundary": "confidential",
            },
            data_boundary="confidential",
        )
        metadata_payload["support_packet_artifact_id"] = support_packet_artifact_id

        connection.execute(
            text(
                """
                INSERT INTO enterprise_support_cases (
                    id, org_id, project_id, enterprise_cluster_id,
                    account_connect_org_id, hosted_org_id, title, severity,
                    category, description, status, sla_due_at,
                    response_time_hours, hosted_case_ref, receipt_id,
                    assigned_to, created_by, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :enterprise_cluster_id,
                    :account_connect_org_id, :hosted_org_id, :title, :severity,
                    :category, :description, :status, :sla_due_at,
                    :response_time_hours, :hosted_case_ref, :receipt_id,
                    :assigned_to, :created_by, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": case_id,
                "org_id": scope["org_id"],
                "project_id": project_id,
                "enterprise_cluster_id": cluster.get("id") if cluster else None,
                "account_connect_org_id": account_connect_org_id,
                "hosted_org_id": hosted_org_id,
                "title": cleaned_title,
                "severity": normalized_severity,
                "category": normalized_category,
                "description": cleaned_description,
                "status": status,
                "sla_due_at": sla_due_at,
                "response_time_hours": response_hours,
                "hosted_case_ref": hosted_case_ref,
                "receipt_id": receipt_id,
                "assigned_to": None,
                "created_by": actor,
                "metadata": json.dumps(metadata_payload),
            },
        )
        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "enterprise_support_case",
            case_id,
            {
                "severity": normalized_severity,
                "status": status,
                "sla_due_at": sla_due_at.isoformat(),
                "receipt_id": receipt_id,
                "hosted_case_ref": hosted_case_ref,
                "support_packet_artifact_id": support_packet_artifact_id,
            },
        )
        return self.get_support_case(connection, project_id, case_id)

    def update_support_case(
        self,
        connection: Connection,
        project_id: str,
        case_id: str,
        *,
        actor: str,
        status: Optional[str] = None,
        assigned_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        existing = self.get_support_case(connection, project_id, case_id)
        normalized_status = self._id_token(status) if status else existing["status"]
        if normalized_status not in {
            "open",
            "hosted_submitted",
            "in_progress",
            "blocked_configuration",
            "failed",
            "resolved",
            "closed",
        }:
            raise ValueError(f"Unsupported support case status: {status}")
        merged_metadata = {
            **(existing.get("metadata") or {}),
            **(metadata or {}),
            "last_updated_by": actor,
        }
        connection.execute(
            text(
                """
                UPDATE enterprise_support_cases
                SET status = :status,
                    assigned_to = :assigned_to,
                    metadata = CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {
                "project_id": project_id,
                "id": case_id,
                "status": normalized_status,
                "assigned_to": assigned_to
                if assigned_to is not None
                else existing.get("assigned_to"),
                "metadata": json.dumps(merged_metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.support_case_updated",
            actor,
            "enterprise_support_case",
            case_id,
            {
                "status": normalized_status,
                "assigned_to": assigned_to
                if assigned_to is not None
                else existing.get("assigned_to"),
            },
        )
        return self.get_support_case(connection, project_id, case_id)

    def get_support_case(
        self, connection: Connection, project_id: str, case_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, enterprise_cluster_id,
                       account_connect_org_id, hosted_org_id, title, severity,
                       category, description, status, sla_due_at,
                       response_time_hours, hosted_case_ref, receipt_id,
                       assigned_to, created_by, metadata, created_at, updated_at
                FROM enterprise_support_cases
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": case_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Enterprise support case '{case_id}' was not found.")
        return self._support_case_from_row(dict(row))

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
        identity_provider_status = self._active_identity_provider_status(
            connection,
            project_id,
        )
        normalized_identity = self._normalize_identity_controls(
            identity_controls or {},
            policy.get("identity") or {},
            identity_provider_status,
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
            "identity_provider_status": identity_provider_status,
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

    def _project_scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Project '{project_id}' was not found.")
        return dict(row)

    def _active_account_connect_org(
        self, connection: Connection, project_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, hosted_org_id, local_org_id,
                       connection_mode, status
                FROM account_connect_orgs
                WHERE project_id = :project_id AND status IN ('linked', 'active')
                ORDER BY linked_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        return dict(row) if row else None

    def _active_or_requested_cluster(
        self,
        connection: Connection,
        project_id: str,
        enterprise_cluster_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if enterprise_cluster_id:
            return self.get_cluster(connection, enterprise_cluster_id, project_id)
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, deployment_mode, account_connect_mode,
                       worker_pool_strategy, ha_profile, data_residency,
                       identity_controls, governance_controls, audit_controls,
                       private_worker_pools, compliance_packs, support_tier,
                       support_sla, status, created_by, metadata, created_at, updated_at
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
        return self._cluster_from_row(dict(row)) if row else None

    def _support_response_hours(
        self, cluster: Optional[dict[str, Any]], severity: str
    ) -> int:
        if severity == "critical":
            return 1
        if severity == "high":
            return 4
        if severity == "low":
            return 48
        if cluster:
            support_sla = cluster.get("support_sla") or {}
            response_hours = support_sla.get("response_time_hours")
            if isinstance(response_hours, int) and response_hours > 0:
                return response_hours
        return 24

    def _support_case_by_receipt(
        self, connection: Connection, project_id: str, receipt_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, enterprise_cluster_id,
                       account_connect_org_id, hosted_org_id, title, severity,
                       category, description, status, sla_due_at,
                       response_time_hours, hosted_case_ref, receipt_id,
                       assigned_to, created_by, metadata, created_at, updated_at
                FROM enterprise_support_cases
                WHERE project_id = :project_id AND receipt_id = :receipt_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "receipt_id": receipt_id},
        ).mappings().first()
        return self._support_case_from_row(dict(row)) if row else None

    def _support_case_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        support_packet_artifact_id = metadata.get("support_packet_artifact_id")
        return {
            **row,
            "support_packet_artifact_id": (
                support_packet_artifact_id
                if isinstance(support_packet_artifact_id, str)
                else None
            ),
            "sla_due_at": row["sla_due_at"].isoformat()
            if row.get("sla_due_at")
            else None,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _get_cluster_validation_run(
        self, connection: Connection, project_id: str, run_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, enterprise_cluster_id,
                       validation_kind, status, checks, evidence_refs,
                       blocking_gaps, validated_by, metadata, created_at
                FROM enterprise_cluster_validation_runs
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": run_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Enterprise cluster validation run '{run_id}' was not found.")
        return self._cluster_validation_run_from_row(dict(row))

    def _cluster_validation_run_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
        }

    def _normalize_cluster_validation_checks(
        self, checks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not checks:
            raise ValueError("cluster validation checks must not be empty.")
        normalized: list[dict[str, Any]] = []
        for item in checks:
            if not isinstance(item, dict):
                raise ValueError("cluster validation checks must be objects.")
            name = self._id_token(str(item.get("name") or ""))
            status = self._id_token(str(item.get("status") or ""))
            if not name:
                raise ValueError("cluster validation check name must not be blank.")
            if status not in self.CLUSTER_VALIDATION_STATUSES:
                raise ValueError(
                    f"Unsupported cluster validation check status: {status}"
                )
            normalized.append(
                {
                    "name": name,
                    "status": status,
                    "required": bool(item.get("required", True)),
                    "message": self._optional_text(item.get("message")),
                    "evidence_ref": self._optional_text(item.get("evidence_ref")),
                }
            )
        return normalized

    def _cluster_validation_blocking_gaps(
        self, checks: list[dict[str, Any]]
    ) -> list[str]:
        gaps: list[str] = []
        for item in checks:
            if item["required"] and item["status"] in {"failed", "blocked"}:
                gaps.append(f"cluster_validation_{item['name']}_{item['status']}")
        return gaps

    def _normalize_identity_controls(
        self,
        controls: dict[str, Any],
        policy: dict[str, Any],
        identity_provider_status: dict[str, bool],
    ) -> dict[str, Any]:
        oidc_configured = bool(
            controls.get(
                "sso_oidc_configured",
                self.settings.oidc_enabled and self.settings.oidc_configured,
            )
        ) or bool(identity_provider_status.get("oidc"))
        saml_configured = bool(controls.get("saml_configured", False)) or bool(
            identity_provider_status.get("saml")
        )
        scim_configured = bool(controls.get("scim_configured", False)) or bool(
            identity_provider_status.get("scim")
        )
        return {
            "sso_oidc_required": bool(policy.get("sso_oidc", True)),
            "sso_oidc_configured": oidc_configured,
            "saml_required": bool(policy.get("saml", True)),
            "saml_configured": saml_configured,
            "scim_required": bool(policy.get("scim", True)),
            "scim_configured": scim_configured,
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

    def _get_identity_provider(
        self,
        connection: Connection,
        project_id: str,
        provider_key: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, provider_key, provider_type,
                       display_name, issuer, audience, entity_id, sso_url,
                       jwks_url, certificate_fingerprint, scim_base_url,
                       secret_ref, status, test_status, blocking_gaps,
                       metadata, created_by, created_at, updated_at
                FROM enterprise_identity_providers
                WHERE project_id = :project_id AND provider_key = :provider_key
                """
            ),
            {"project_id": project_id, "provider_key": provider_key},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Identity provider '{provider_key}' was not found.")
        return self._identity_provider_from_row(dict(row))

    def _get_identity_rollout_policy(
        self,
        connection: Connection,
        project_id: str,
        provider_type: str,
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, provider_type, runtime_mode,
                       allowed_domains, rollout_notes, status, created_by,
                       metadata, created_at, updated_at
                FROM enterprise_identity_rollout_policies
                WHERE project_id = :project_id
                  AND provider_type = :provider_type
                  AND status = 'active'
                """
            ),
            {"project_id": project_id, "provider_type": provider_type},
        ).mappings().first()
        return self._identity_rollout_policy_from_row(dict(row)) if row else None

    def _active_identity_provider_status(
        self,
        connection: Connection,
        project_id: str,
    ) -> dict[str, bool]:
        rows = connection.execute(
            text(
                """
                SELECT provider_type
                FROM enterprise_identity_providers
                WHERE project_id = :project_id
                  AND status = 'active'
                  AND jsonb_array_length(blocking_gaps) = 0
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        active_provider_types = {row["provider_type"] for row in rows}
        policies = connection.execute(
            text(
                """
                SELECT provider_type, runtime_mode
                FROM enterprise_identity_rollout_policies
                WHERE project_id = :project_id
                  AND status = 'active'
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        policy_modes = {
            row["provider_type"]: row["runtime_mode"]
            for row in policies
        }
        return {
            "oidc": "oidc" in active_provider_types
            and policy_modes.get("oidc") == "auth_allowed",
            "saml": "saml" in active_provider_types
            and policy_modes.get("saml") == "auth_allowed",
            "scim": "scim" in active_provider_types
            and policy_modes.get("scim") == "provisioning_allowed",
        }

    def _identity_provider_gaps(
        self,
        *,
        provider_type: str,
        issuer: Optional[str],
        audience: Optional[str],
        entity_id: Optional[str],
        sso_url: Optional[str],
        jwks_url: Optional[str],
        certificate_fingerprint: Optional[str],
        scim_base_url: Optional[str],
        secret_ref: Optional[str],
    ) -> list[str]:
        gaps: list[str] = []
        if provider_type == "oidc":
            if not self._optional_text(issuer):
                gaps.append("oidc_issuer_missing")
            if not self._optional_text(audience):
                gaps.append("oidc_audience_missing")
            if not self._optional_text(jwks_url):
                gaps.append("oidc_jwks_url_missing")
        elif provider_type == "saml":
            if not self._optional_text(entity_id):
                gaps.append("saml_entity_id_missing")
            if not self._optional_text(sso_url):
                gaps.append("saml_sso_url_missing")
            if not self._optional_text(certificate_fingerprint):
                gaps.append("saml_certificate_fingerprint_missing")
        elif provider_type == "scim":
            if not self._optional_text(scim_base_url):
                gaps.append("scim_base_url_missing")
            if not self._optional_text(secret_ref):
                gaps.append("scim_secret_ref_missing")
        return gaps

    def _identity_provider_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _identity_rollout_policy_from_row(
        self, row: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            **row,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _optional_text(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def _required_text(self, value: str, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be blank.")
        return cleaned

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
