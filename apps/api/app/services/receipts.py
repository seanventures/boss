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
                SELECT id, dedupe_key, effect_kind, status, provider_ref, result, error
                FROM receipts
                WHERE org_id = :org_id AND dedupe_key = :dedupe_key
                """
            ),
            {"org_id": org_id, "dedupe_key": dedupe_key},
        ).mappings().first()
        return dict(row) if row else None

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
                    effect_scope, payload_hash, status
                )
                VALUES (
                    :id, :org_id, :workspace_id, :project_id, :dedupe_key, :effect_kind,
                    CAST(:effect_scope AS JSONB), :payload_hash, 'in_progress'
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
                "dedupe_key": dedupe_key,
                "effect_kind": effect_kind,
                "status": "in_progress",
                "provider_ref": None,
                "result": None,
                "error": None,
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
