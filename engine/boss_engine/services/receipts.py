from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, json_hash, make_id, normalize_data_boundary


class ReceiptService:
    def get_by_dedupe_key(
        self, connection: Connection, org_id: str, dedupe_key: str
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, data_boundary, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE org_id = :org_id AND dedupe_key = :dedupe_key
                """
            ),
            {"org_id": org_id, "dedupe_key": dedupe_key},
        ).mappings().first()
        return dict(row) if row else None

    def list_by_dedupe_prefix(
        self, connection: Connection, org_id: str, prefix: str
    ) -> list[dict]:
        """Receipts whose dedupe key starts with prefix, oldest first.

        Used to replay the original typed-decision receipt. The prefix is matched
        literally: LIKE's wildcards in it are escaped, not refused. Refusing them refused
        every real prefix — each starts "model-gateway:<project>:typed_decision:", and `_`
        is a LIKE wildcard — so every governed decision failed here on Postgres while the
        unit doubles, which never ran this query, passed.
        """
        literal = (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, data_boundary, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE org_id = :org_id AND dedupe_key LIKE :prefix ESCAPE '\\'
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"org_id": org_id, "prefix": literal + "%"},
        ).mappings().all()
        return [dict(row) for row in rows]

    def merge_result(self, connection: Connection, receipt_id: str, result: dict) -> None:
        """Add fields to a receipt result without dropping what settlement wrote."""
        connection.execute(
            text(
                """
                UPDATE receipts
                SET result = COALESCE(result, '{}'::jsonb) || CAST(:result AS JSONB),
                    updated_at = now()
                WHERE id = :receipt_id
                """
            ),
            {"receipt_id": receipt_id, "result": json.dumps(result)},
        )

    def get_by_id(
        self, connection: Connection, org_id: str, receipt_id: str
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, data_boundary, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE org_id = :org_id AND id = :receipt_id
                """
            ),
            {"org_id": org_id, "receipt_id": receipt_id},
        ).mappings().first()
        return dict(row) if row else None

    def get_by_id_or_dedupe_key(
        self, connection: Connection, org_id: str, key: str
    ) -> Optional[dict]:
        """Look up a receipt by id (rcpt_…) first, then by dedupe_key."""
        token = str(key or "").strip()
        if not token:
            return None
        if token.startswith("rcpt_"):
            found = self.get_by_id(connection, org_id, token)
            if found:
                return found
        found = self.get_by_dedupe_key(connection, org_id, token)
        if found:
            return found
        return self.get_by_id(connection, org_id, token)

    def list_by_project(
        self, connection: Connection, project_id: str, limit: int, offset: int = 0
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, dedupe_key, effect_kind, effect_scope, payload_hash,
                       status, provider_ref, created_by_job_id, data_boundary, result, error,
                       created_at, updated_at
                FROM receipts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
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
        data_boundary: Optional[str] = None,
    ) -> tuple[dict, bool]:
        existing = self.get_by_dedupe_key(connection, org_id, dedupe_key)
        if existing:
            return existing, False

        receipt_id = make_id("rcpt")
        boundary = normalize_data_boundary(
            data_boundary
            or effect_scope.get("data_boundary")
            or payload.get("data_boundary")
        )
        connection.execute(
            text(
                """
                INSERT INTO receipts (
                    id, org_id, workspace_id, project_id, dedupe_key, effect_kind,
                    effect_scope, payload_hash, status, created_by_job_id,
                    data_boundary
                )
                VALUES (
                    :id, :org_id, :workspace_id, :project_id, :dedupe_key, :effect_kind,
                    CAST(:effect_scope AS JSONB), :payload_hash, 'in_progress',
                    :created_by_job_id, :data_boundary
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
                "data_boundary": boundary,
            },
        )
        emit_event(
            connection,
            project_id,
            "receipt.created",
            "system",
            "receipt",
            receipt_id,
            {
                "effect_kind": effect_kind,
                "dedupe_key": dedupe_key,
                "data_boundary": boundary,
            },
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
                "data_boundary": boundary,
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
        # A QUESTION THIS EFFECT'S FAILURE RAISED IS MOOT NOW (cleared_causes): "GitHub
        # integration is not configured" stayed in Needs you after the merge it blocked landed.
        from .cleared_causes import on_effect_succeeded

        on_effect_succeeded(connection, receipt_id)

    def record_completed(
        self,
        connection: Connection,
        org_id: str,
        workspace_id: str,
        project_id: str,
        dedupe_key: str,
        effect_kind: str,
        effect_scope: dict,
        payload: dict,
        *,
        provider_ref: Optional[str] = None,
        created_by_job_id: Optional[str] = None,
        data_boundary: str = "internal",
    ) -> tuple[dict, bool]:
        """Record an effect that has ALREADY happened, closed in one step.

        Most callers here are not starting something and waiting on it -- they have already
        done the work and are writing it down, with the outcome in the payload. Those were
        using create_or_reuse and never closing the receipt, so a completed effect was recorded
        as permanently in progress.

        Measured on the live console when the Receipts screen first ran: 1,428 of 1,606
        receipts in 24 hours sat at in_progress, and exactly one effect kind of seven ever
        called mark_succeeded. At 88% noise a receipt can no longer answer the only question it
        exists for -- did this effect actually happen? -- which is precisely what the "the API
        restarted while this deployment was running" investigation needed it for.

        Use create_or_reuse + mark_succeeded/mark_failed when the effect genuinely resolves
        later. Use this when it is already done.
        """
        receipt, created = self.create_or_reuse(
            connection,
            org_id,
            workspace_id,
            project_id,
            dedupe_key,
            effect_kind,
            effect_scope,
            payload,
            created_by_job_id=created_by_job_id,
            data_boundary=data_boundary,
        )
        # Only on first write. Re-marking a reused receipt would overwrite the result of the
        # effect that actually ran with the payload of the one that was deduped away.
        if created:
            self.mark_succeeded(connection, receipt["id"], provider_ref, payload)
            receipt = {**receipt, "status": "succeeded", "provider_ref": provider_ref}
        return receipt, created

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
