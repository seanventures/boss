from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id


class SecretRefNotFoundError(RuntimeError):
    pass


class SecretRefService:
    # EVERY STORE BOSS CAN ACTUALLY WRITE TO, not a subset of them.
    #
    # `infisical` and `database` were missing, and both are what `default_secret_backend`
    # returns on a normal install: the installer provisions Infisical, and an install with no
    # store at all falls back to Postgres. So pasting a GitHub token into the wizard — which
    # writes through the chosen backend and then records a `<kind>:<key>` reference — failed
    # with "credential_ref kind must be one of: env, local_runner, local_vault,
    # platform_secret_store, vault" on the two configurations customers will have. The write
    # succeeded and the reference was refused, so Step one could not complete.
    #
    # Reading never had this problem: `resolve_secret_ref` asks `backend_for`, which knows all
    # of these. It was only the validation that had its own private list.
    ALLOWED_REF_KINDS = {
        "env",
        "vault",
        "local_vault",
        "local_runner",
        "platform_secret_store",
        "infisical",
        "database",
        # The GitHub App connected in Setup › GitHub access: resolved at use time to a fresh
        # 1-hour installation token by identity/github_setup.py. Nothing is stored for it.
        "identity_github",
    }
    ALLOWED_SCOPE_TYPES = {"org", "workspace", "project"}
    ALLOWED_SECRET_KINDS = {
        "api_token",
        "access_key",
        "client_secret",
        "consumer_key",
        "private_key",
        "service_account",
        "session",
    }
    ALLOWED_STORAGE_BACKENDS = {
        "environment",
        "vault",
        "local_vault",
        "local_runner",
        "platform_secret_store",
        "infisical",
        "database",
        "identity_github",
    }
    ALLOWED_STATUSES = {
        "active",
        "disabled",
        "rotating",
        "rotation_due",
        "revoked",
    }
    USABLE_STATUSES = {"active", "rotation_due", "rotating"}
    _SENSITIVE_METADATA_KEYS = {
        "api_key",
        "access_key",
        "client_secret",
        "consumer_key",
        "password",
        "private_key",
        "secret",
        "token",
    }

    def list_secret_refs(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        if offset < 0:
            raise ValueError("offset must be >= 0.")
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": limit,
            "offset": offset,
        }
        if provider:
            filters.append("provider = :provider")
            params["provider"] = provider
        if status:
            filters.append("status = :status")
            params["status"] = status
        where_clause = " AND ".join(filters)
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, scope_type, scope_id, ref_kind, ref_key,
                       provider, secret_kind, storage_backend, status, metadata,
                       rotation_due_at, last_verified_at, supersedes_secret_ref_id,
                       overlap_expires_at, revoked_at, revoked_by,
                       created_by, created_at, updated_at
                FROM secret_refs
                WHERE {where_clause}
                ORDER BY provider NULLS LAST, ref_kind, ref_key
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_row(row) for row in rows]

    def get_secret_ref(
        self, connection: Connection, project_id: str, secret_ref_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, scope_type, scope_id, ref_kind, ref_key,
                       provider, secret_kind, storage_backend, status, metadata,
                       rotation_due_at, last_verified_at, supersedes_secret_ref_id,
                       overlap_expires_at, revoked_at, revoked_by,
                       created_by, created_at, updated_at
                FROM secret_refs
                WHERE project_id = :project_id AND id = :secret_ref_id
                """
            ),
            {"project_id": project_id, "secret_ref_id": secret_ref_id},
        ).mappings().first()
        if not row:
            raise SecretRefNotFoundError("SecretRef not found.")
        return self._serialize_row(row)

    def create_secret_ref(
        self,
        connection: Connection,
        *,
        project_id: str,
        ref_kind: str,
        ref_key: str,
        provider: Optional[str],
        status: str,
        metadata: dict[str, Any],
        actor: str,
        scope_type: str = "project",
        scope_id: Optional[str] = None,
        secret_kind: str = "api_token",
        storage_backend: Optional[str] = None,
        rotation_due_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        ref_kind = ref_kind.strip().lower()
        ref_key = ref_key.strip()
        if ref_kind not in self.ALLOWED_REF_KINDS:
            raise ValueError(
                f"ref_kind must be one of: {', '.join(sorted(self.ALLOWED_REF_KINDS))}."
            )
        if not ref_key:
            raise ValueError("ref_key must be non-empty.")
        self.parse_credential_ref(f"{ref_kind}:{ref_key}")
        if status not in self.ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(self.ALLOWED_STATUSES))}."
            )
        normalized_scope_type = scope_type.strip().lower()
        if normalized_scope_type not in self.ALLOWED_SCOPE_TYPES:
            raise ValueError(
                "scope_type must be one of: "
                + ", ".join(sorted(self.ALLOWED_SCOPE_TYPES))
                + "."
            )
        normalized_scope_id = (scope_id or project_id).strip()
        if not normalized_scope_id:
            raise ValueError("scope_id must be non-empty.")
        self._validate_scope(
            connection,
            project_id=project_id,
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
        )
        normalized_secret_kind = secret_kind.strip().lower()
        if normalized_secret_kind not in self.ALLOWED_SECRET_KINDS:
            raise ValueError(
                "secret_kind must be one of: "
                + ", ".join(sorted(self.ALLOWED_SECRET_KINDS))
                + "."
            )
        normalized_storage_backend = (
            storage_backend or self._storage_backend_for_ref_kind(ref_kind)
        ).strip().lower()
        if normalized_storage_backend not in self.ALLOWED_STORAGE_BACKENDS:
            raise ValueError(
                "storage_backend must be one of: "
                + ", ".join(sorted(self.ALLOWED_STORAGE_BACKENDS))
                + "."
            )
        expected_storage_backend = self._storage_backend_for_ref_kind(ref_kind)
        if normalized_storage_backend != expected_storage_backend:
            raise ValueError(
                f"storage_backend for {ref_kind!r} must be "
                f"{expected_storage_backend!r}."
            )
        safe_metadata = self._validate_metadata(metadata)
        secret_ref_id = make_id("secret")
        connection.execute(
            text(
                """
                INSERT INTO secret_refs (
                    id, project_id, scope_type, scope_id, ref_kind, ref_key,
                    provider, secret_kind, storage_backend, status, metadata,
                    rotation_due_at, created_by
                )
                VALUES (
                    :id, :project_id, :scope_type, :scope_id, :ref_kind, :ref_key,
                    :provider, :secret_kind, :storage_backend, :status,
                    CAST(:metadata AS JSONB), :rotation_due_at, :actor
                )
                """
            ),
            {
                "id": secret_ref_id,
                "project_id": project_id,
                "scope_type": normalized_scope_type,
                "scope_id": normalized_scope_id,
                "ref_kind": ref_kind,
                "ref_key": ref_key,
                "provider": provider,
                "secret_kind": normalized_secret_kind,
                "storage_backend": normalized_storage_backend,
                "status": status,
                "metadata": self._json(safe_metadata),
                "rotation_due_at": rotation_due_at,
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "secret_ref.created",
            actor,
            "secret_ref",
            secret_ref_id,
            {
                "ref_kind": ref_kind,
                "ref_key": ref_key,
                "provider": provider,
                "status": status,
                "scope_type": normalized_scope_type,
                "scope_id": normalized_scope_id,
                "secret_kind": normalized_secret_kind,
                "storage_backend": normalized_storage_backend,
            },
        )
        return self.get_secret_ref(connection, project_id, secret_ref_id)

    def update_secret_ref(
        self,
        connection: Connection,
        *,
        project_id: str,
        secret_ref_id: str,
        provider: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        rotation_due_at: Optional[datetime] = None,
        actor: str,
    ) -> dict[str, Any]:
        existing = self.get_secret_ref(connection, project_id, secret_ref_id)
        next_status = status or existing["status"]
        if existing["status"] == "revoked" and next_status != "revoked":
            raise ValueError("Revoked SecretRefs cannot be reactivated.")
        if next_status not in self.ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(self.ALLOWED_STATUSES))}."
            )
        next_provider = provider if provider is not None else existing["provider"]
        next_metadata = (
            self._validate_metadata(metadata)
            if metadata is not None
            else existing["metadata"]
        )
        next_rotation_due_at = (
            rotation_due_at
            if rotation_due_at is not None
            else existing["rotation_due_at"]
        )
        connection.execute(
            text(
                """
                UPDATE secret_refs
                SET provider = :provider,
                    status = :status,
                    metadata = CAST(:metadata AS JSONB),
                    rotation_due_at = :rotation_due_at,
                    revoked_at = CASE
                        WHEN :status = 'revoked' THEN COALESCE(revoked_at, now())
                        ELSE revoked_at
                    END,
                    revoked_by = CASE
                        WHEN :status = 'revoked' THEN COALESCE(revoked_by, :actor)
                        ELSE revoked_by
                    END,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :secret_ref_id
                """
            ),
            {
                "project_id": project_id,
                "secret_ref_id": secret_ref_id,
                "provider": next_provider,
                "status": next_status,
                "metadata": self._json(next_metadata),
                "rotation_due_at": next_rotation_due_at,
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "secret_ref.updated",
            actor,
            "secret_ref",
            secret_ref_id,
            {"status": next_status, "provider": next_provider},
        )
        return self.get_secret_ref(connection, project_id, secret_ref_id)

    def delete_secret_ref(
        self,
        connection: Connection,
        *,
        project_id: str,
        secret_ref_id: str,
        actor: str,
    ) -> None:
        existing = self.get_secret_ref(connection, project_id, secret_ref_id)
        connection.execute(
            text(
                """
                UPDATE secret_refs
                SET status = 'revoked',
                    revoked_at = now(),
                    revoked_by = :actor,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :secret_ref_id
                """
            ),
            {
                "project_id": project_id,
                "secret_ref_id": secret_ref_id,
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "secret_ref.revoked",
            actor,
            "secret_ref",
            secret_ref_id,
            {"ref_kind": existing["ref_kind"], "ref_key": existing["ref_key"]},
        )

    def _serialize_row(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "scope_type": row["scope_type"],
            "scope_id": row["scope_id"],
            "ref_kind": row["ref_kind"],
            "ref_key": row["ref_key"],
            "provider": row["provider"],
            "secret_kind": row["secret_kind"],
            "storage_backend": row["storage_backend"],
            "status": row["status"],
            "metadata": row["metadata"] or {},
            "rotation_due_at": self._iso(row["rotation_due_at"]),
            "last_verified_at": self._iso(row["last_verified_at"]),
            "supersedes_secret_ref_id": row["supersedes_secret_ref_id"],
            "overlap_expires_at": self._iso(row["overlap_expires_at"]),
            "revoked_at": self._iso(row["revoked_at"]),
            "revoked_by": row["revoked_by"],
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "credential_ref": f"{row['ref_kind']}:{row['ref_key']}",
        }

    def get_by_credential_ref(
        self,
        connection: Connection,
        *,
        project_id: str,
        credential_ref: str,
    ) -> dict[str, Any]:
        ref_kind, ref_key = self.parse_credential_ref(credential_ref)
        row = connection.execute(
            text(
                """
                SELECT id, project_id, scope_type, scope_id, ref_kind, ref_key,
                       provider, secret_kind, storage_backend, status, metadata,
                       rotation_due_at, last_verified_at, supersedes_secret_ref_id,
                       overlap_expires_at, revoked_at, revoked_by,
                       created_by, created_at, updated_at
                FROM secret_refs
                WHERE project_id = :project_id
                  AND ref_kind = :ref_kind
                  AND ref_key = :ref_key
                """
            ),
            {
                "project_id": project_id,
                "ref_kind": ref_kind,
                "ref_key": ref_key,
            },
        ).mappings().first()
        if not row:
            raise SecretRefNotFoundError("SecretRef not found.")
        return self._serialize_row(row)

    def require_usable(
        self,
        connection: Connection,
        *,
        project_id: str,
        credential_ref: str,
    ) -> dict[str, Any]:
        item = self.get_by_credential_ref(
            connection,
            project_id=project_id,
            credential_ref=credential_ref,
        )
        if item["status"] not in self.USABLE_STATUSES:
            raise ValueError(
                f"SecretRef {credential_ref!r} is {item['status']} and cannot be used."
            )
        if (
            item["status"] == "rotating"
            and item["overlap_expires_at"]
            and datetime.fromisoformat(item["overlap_expires_at"])
            <= datetime.now(timezone.utc)
        ):
            raise ValueError(
                f"SecretRef {credential_ref!r} rotation overlap has expired."
            )
        return item

    @classmethod
    def parse_credential_ref(cls, credential_ref: str) -> tuple[str, str]:
        ref_kind, separator, ref_key = credential_ref.strip().partition(":")
        if not separator or not ref_key:
            raise ValueError("credential_ref must use '<kind>:<key>' format.")
        if ref_kind not in cls.ALLOWED_REF_KINDS:
            raise ValueError(
                f"credential_ref kind must be one of: {', '.join(sorted(cls.ALLOWED_REF_KINDS))}."
            )
        if ref_kind == "env" and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", ref_key):
            raise ValueError(
                "env SecretRef keys must be environment variable names, never raw values."
            )
        from .env_refs import env_name_allowed, file_path_allowed, refusal

        if ref_kind == "env" and not env_name_allowed(ref_key):
            raise ValueError(refusal(ref_key))
        if ref_kind == "local_runner" and os.path.isabs(os.path.expanduser(ref_key)) \
                and not file_path_allowed(ref_key):
            raise ValueError("A secret reference cannot read that file: it is one of BOSS's own "
                             "or the system's.")
        return ref_kind, ref_key

    @classmethod
    def _validate_metadata(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object.")

        def visit(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for raw_key, child in value.items():
                    key = str(raw_key).strip().lower()
                    child_path = f"{path}.{raw_key}" if path else str(raw_key)
                    if key == "raw_secret_stored":
                        if child is not False:
                            raise ValueError("metadata.raw_secret_stored must be false.")
                    elif key.endswith("_ref"):
                        if not isinstance(child, str):
                            raise ValueError(f"{child_path} must be a SecretRef string.")
                        cls.parse_credential_ref(child)
                    elif key in cls._SENSITIVE_METADATA_KEYS or any(
                        key.endswith(f"_{token}")
                        for token in cls._SENSITIVE_METADATA_KEYS
                    ):
                        raise ValueError(
                            f"{child_path} looks like raw secret material; store a SecretRef instead."
                        )
                    visit(child, child_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")

        visit(metadata, "")
        return metadata

    @staticmethod
    def _storage_backend_for_ref_kind(ref_kind: str) -> str:
        # A missing entry here is a KeyError rather than a message, so every kind the
        # allow-list accepts has to appear. `env` is the only one whose stored backend name
        # differs from its ref kind.
        return {
            "env": "environment",
            "vault": "vault",
            "local_vault": "local_vault",
            "local_runner": "local_runner",
            "platform_secret_store": "platform_secret_store",
            "infisical": "infisical",
            "database": "database",
            "identity_github": "identity_github",
        }[ref_kind]

    @staticmethod
    def _validate_scope(
        connection: Connection,
        *,
        project_id: str,
        scope_type: str,
        scope_id: str,
    ) -> None:
        scope = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not scope:
            raise ValueError("Project not found.")
        expected_scope_id = scope[f"{scope_type}_id"]
        if scope_id != expected_scope_id:
            raise ValueError(
                f"scope_id does not match the project's {scope_type} scope."
            )

    @staticmethod
    def _iso(value: Any) -> Optional[str]:
        return value.isoformat() if value else None

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload or {})
