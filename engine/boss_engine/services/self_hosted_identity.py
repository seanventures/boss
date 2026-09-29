"""Self-hosted identity, SSO, and SCIM service shared by all editions.

The paid cluster and hosted support implementation extends this service, but
Community startup only imports this module.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event
from .members import MemberDirectoryService


class SelfHostedIdentityService:
    IDENTITY_PROVIDER_TYPES = {"oidc", "saml", "scim"}
    IDENTITY_PROVIDER_STATUSES = {"planned", "active", "blocked", "disabled"}
    IDENTITY_ROLLOUT_MODES = {
        "disabled",
        "metadata_only",
        "auth_allowed",
        "provisioning_allowed",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.members = MemberDirectoryService(settings)

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
            # org_members. An inactive project row now overrides that fallback
            # for this project, and the org mirror must also be deactivated to
            # remove access to sibling projects. Retain both rows for audit.
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
