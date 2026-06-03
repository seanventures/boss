from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, json_hash, make_id


class ReceiptService:
    def get_by_dedupe_key(
        self, connection: Connection, org_id: str, dedupe_key: str
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE org_id = :org_id AND dedupe_key = :dedupe_key
                """
            ),
            {"org_id": org_id, "dedupe_key": dedupe_key},
        ).mappings().first()
        return dict(row) if row else None

    def list_by_project(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def create_or_reuse(
        self,
        connection: Connection,
        org_id: str,
        workspace_id: str,
        project_id: str,
        dedupe_key: str,
        effect_kind: str,
        effect_scope: dict,
        payload: dict,
        created_by_job_id: Optional[str] = None,
    ) -> tuple[dict, bool]:
        existing = self.get_by_dedupe_key(connection, org_id, dedupe_key)
        if existing:
            return existing, False

        receipt_id = make_id("rcpt")
        connection.execute(
            text(
                """
                INSERT INTO receipts (
                    id, org_id, workspace_id, project_id, dedupe_key, effect_kind,
                    effect_scope, payload_hash, status, created_by_job_id
                )
                VALUES (
                    :id, :org_id, :workspace_id, :project_id, :dedupe_key, :effect_kind,
                    CAST(:effect_scope AS JSONB), :payload_hash, 'in_progress', :created_by_job_id
                )
                """
            ),
            {
                "id": receipt_id,
                "org_id": org_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "dedupe_key": dedupe_key,
                "effect_kind": effect_kind,
                "effect_scope": json.dumps(effect_scope),
                "payload_hash": json_hash(payload),
                "created_by_job_id": created_by_job_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "receipt.created",
            "system",
            "receipt",
            receipt_id,
            {"effect_kind": effect_kind, "dedupe_key": dedupe_key},
        )
        return (
            {
                "id": receipt_id,
                "project_id": project_id,
                "dedupe_key": dedupe_key,
                "effect_kind": effect_kind,
                "status": "in_progress",
                "effect_scope": effect_scope,
                "payload_hash": json_hash(payload),
                "provider_ref": None,
                "created_by_job_id": created_by_job_id,
                "result": None,
                "error": None,
                "created_at": None,
                "updated_at": None,
            },
            True,
        )

    def mark_succeeded(
        self,
        connection: Connection,
        receipt_id: str,
        provider_ref: Optional[str],
        result: dict,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE receipts
                SET status = 'succeeded',
                    provider_ref = :provider_ref,
                    result = CAST(:result AS JSONB),
                    updated_at = now()
                WHERE id = :receipt_id
                """
            ),
            {
                "receipt_id": receipt_id,
                "provider_ref": provider_ref,
                "result": json.dumps(result),
            },
        )

    def mark_failed(self, connection: Connection, receipt_id: str, error: dict) -> None:
        connection.execute(
            text(
                """
                UPDATE receipts
                SET status = 'failed',
                    error = CAST(:error AS JSONB),
                    updated_at = now()
                WHERE id = :receipt_id
                """
            ),
            {"receipt_id": receipt_id, "error": json.dumps(error)},
        )
