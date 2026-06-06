from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id


class SecretRefNotFoundError(RuntimeError):
    pass


class SecretRefService:
    ALLOWED_REF_KINDS = {"env", "vault", "local_runner", "platform_secret_store"}
    ALLOWED_STATUSES = {"active", "disabled", "rotation_due", "revoked"}

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
                SELECT id, project_id, ref_kind, ref_key, provider, status, metadata,
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
                SELECT id, project_id, ref_kind, ref_key, provider, status, metadata,
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
    ) -> dict[str, Any]:
        ref_kind = ref_kind.strip()
        ref_key = ref_key.strip()
        if ref_kind not in self.ALLOWED_REF_KINDS:
            raise ValueError(
                f"ref_kind must be one of: {', '.join(sorted(self.ALLOWED_REF_KINDS))}."
            )
        if not ref_key:
            raise ValueError("ref_key must be non-empty.")
        if status not in self.ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(self.ALLOWED_STATUSES))}."
            )
        secret_ref_id = make_id("secret")
        connection.execute(
            text(
                """
                INSERT INTO secret_refs (
                    id, project_id, ref_kind, ref_key, provider, status, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :ref_kind, :ref_key, :provider, :status,
                    CAST(:metadata AS JSONB), :actor
                )
                """
            ),
            {
                "id": secret_ref_id,
                "project_id": project_id,
                "ref_kind": ref_kind,
                "ref_key": ref_key,
                "provider": provider,
                "status": status,
                "metadata": self._json(metadata),
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
        actor: str,
    ) -> dict[str, Any]:
        existing = self.get_secret_ref(connection, project_id, secret_ref_id)
        next_status = status or existing["status"]
        if next_status not in self.ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(self.ALLOWED_STATUSES))}."
            )
        next_provider = provider if provider is not None else existing["provider"]
        next_metadata = metadata if metadata is not None else existing["metadata"]
        connection.execute(
            text(
                """
                UPDATE secret_refs
                SET provider = :provider,
                    status = :status,
                    metadata = CAST(:metadata AS JSONB),
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
                    updated_at = now()
                WHERE project_id = :project_id AND id = :secret_ref_id
                """
            ),
            {"project_id": project_id, "secret_ref_id": secret_ref_id},
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
            "ref_kind": row["ref_kind"],
            "ref_key": row["ref_key"],
            "provider": row["provider"],
            "status": row["status"],
            "metadata": row["metadata"] or {},
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "credential_ref": f"{row['ref_kind']}:{row['ref_key']}",
        }

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload or {})
