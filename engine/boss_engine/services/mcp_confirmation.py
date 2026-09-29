"""Durable, single-use human confirmation for selected MCP API actions.

The request body is deliberately not persisted: it may contain credentials or
private data. The approval binds a digest of the exact HTTP method, path,
query, JSON body, API operation alias, and project. A separate human principal
must review the redacted preview and type the digest before MCP can execute it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from ..ports import sql
from .core import emit_event, make_id


CONFIRMATION_TTL_SECONDS = 300
MAX_REQUEST_BYTES = 16_384
_REVIEW_FIELD_NAMES = frozenset({
    "actor", "project_id", "decision_id", "changeset_id", "preview_id",
    "member_id", "target_member_id", "grant_id", "role_keys", "permissions",
    "resource_id", "run_id", "definition_id", "target_id", "status",
})


class ConfirmationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def request_digest(
    *, project_id: str, operation: str, method: str, path: str,
    query: dict[str, Any], body: dict[str, Any] | None,
) -> str:
    document = {
        "project_id": project_id, "operation": operation, "method": method,
        "path": path, "query": query, "body": body,
    }
    try:
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfirmationError("invalid_request_json") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ConfirmationError("request_too_large")
    return hashlib.sha256(encoded).hexdigest()


def create_request(
    connection: Any, *, project_id: str, requested_by: str,
    operation: str, method: str, path: str, query: dict[str, Any],
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    digest = request_digest(
        project_id=project_id, operation=operation, method=method,
        path=path, query=query, body=body,
    )
    confirmation_id = make_id("mcpconf")
    # Free-form values can contain a credential even in a field called "note".
    # Store only a small, fixed field-name summary; the digest still covers the
    # exact unredacted request. Reviewers must receive request details through
    # their trusted operator workflow before approving the digest.
    preview = {
        "method": method, "path": path,
        "query_parameter_count": len(query),
        "body_field_count": len(body or {}),
        "body_fields": sorted(_REVIEW_FIELD_NAMES.intersection(body or {})),
    }
    connection.execute(sql("""
        INSERT INTO mcp_confirmations (
            id, project_id, requested_by, operation_alias, method, request_path,
            request_digest, preview, status, expires_at
        ) VALUES (
            :id, :project_id, :requested_by, :operation, :method, :path,
            :digest, CAST(:preview AS JSONB), 'pending',
            clock_timestamp() + INTERVAL '5 minutes'
        )
    """), {
        "id": confirmation_id, "project_id": project_id,
        "requested_by": requested_by, "operation": operation,
        "method": method, "path": path, "digest": digest,
        "preview": json.dumps(preview, sort_keys=True),
    })
    emit_event(connection, project_id, "mcp.confirmation_requested", requested_by,
               "mcp_confirmation", confirmation_id,
               {"operation": operation, "request_digest": digest})
    return get_request(connection, project_id=project_id, confirmation_id=confirmation_id)


def get_request(connection: Any, *, project_id: str, confirmation_id: str) -> dict[str, Any]:
    row = connection.execute(sql("""
        SELECT id, project_id, requested_by, operation_alias, method,
               request_path, request_digest, preview, status, approved_by,
               created_at, expires_at, approved_at, consumed_at
        FROM mcp_confirmations
        WHERE project_id = :project_id AND id = :id
    """), {"project_id": project_id, "id": confirmation_id}).mappings().first()
    if not row:
        raise ConfirmationError("confirmation_not_found")
    result = dict(row)
    for field in ("created_at", "expires_at", "approved_at", "consumed_at"):
        if isinstance(result[field], datetime):
            result[field] = result[field].isoformat()
    return result


def list_requests(connection: Any, *, project_id: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = connection.execute(sql("""
        SELECT id FROM mcp_confirmations
        WHERE project_id = :project_id
          AND status IN ('pending', 'approved')
          AND expires_at > clock_timestamp()
        ORDER BY created_at DESC LIMIT :limit
    """), {"project_id": project_id, "limit": max(1, min(limit, 100))}).scalars().all()
    return [get_request(connection, project_id=project_id, confirmation_id=row) for row in rows]


def approve_request(
    connection: Any, *, project_id: str, confirmation_id: str,
    approver: str, reviewed_digest: str,
) -> dict[str, Any]:
    row = connection.execute(sql("""
        SELECT requested_by, request_digest, status, expires_at
        FROM mcp_confirmations
        WHERE project_id = :project_id AND id = :id FOR UPDATE
    """), {"project_id": project_id, "id": confirmation_id}).mappings().first()
    if not row:
        raise ConfirmationError("confirmation_not_found")
    if row["requested_by"] == approver:
        raise ConfirmationError("separate_human_approver_required")
    if row["status"] != "pending":
        raise ConfirmationError("confirmation_not_pending")
    if row["expires_at"] <= datetime.now(timezone.utc):
        connection.execute(sql("""
            UPDATE mcp_confirmations SET status = 'expired'
            WHERE project_id = :project_id AND id = :id
        """), {"project_id": project_id, "id": confirmation_id})
        raise ConfirmationError("confirmation_expired")
    if not isinstance(reviewed_digest, str) or reviewed_digest != row["request_digest"]:
        raise ConfirmationError("reviewed_digest_mismatch")
    connection.execute(sql("""
        UPDATE mcp_confirmations
        SET status = 'approved', approved_by = :approver,
            approved_at = clock_timestamp()
        WHERE project_id = :project_id AND id = :id
    """), {"project_id": project_id, "id": confirmation_id, "approver": approver})
    emit_event(connection, project_id, "mcp.confirmation_approved", approver,
               "mcp_confirmation", confirmation_id,
               {"request_digest": reviewed_digest, "requested_by": row["requested_by"]})
    return get_request(connection, project_id=project_id, confirmation_id=confirmation_id)


def consume_request(
    connection: Any, *, project_id: str, confirmation_id: str,
    requester: str, operation: str, method: str, path: str,
    query: dict[str, Any], body: dict[str, Any] | None,
) -> None:
    digest = request_digest(
        project_id=project_id, operation=operation, method=method,
        path=path, query=query, body=body,
    )
    row = connection.execute(sql("""
        UPDATE mcp_confirmations
        SET status = 'consumed', consumed_at = clock_timestamp()
        WHERE id = :id AND project_id = :project_id
          AND requested_by = :requester AND operation_alias = :operation
          AND method = :method AND request_path = :path
          AND request_digest = :digest AND status = 'approved'
          AND expires_at > clock_timestamp()
        RETURNING id, approved_by
    """), {
        "id": confirmation_id, "project_id": project_id,
        "requester": requester, "operation": operation,
        "method": method, "path": path, "digest": digest,
    }).mappings().first()
    if not row:
        raise ConfirmationError("confirmation_missing_expired_or_mismatched")
    emit_event(connection, project_id, "mcp.confirmation_consumed", requester,
               "mcp_confirmation", confirmation_id,
               {"operation": operation, "request_digest": digest,
                "approved_by": row["approved_by"]})


def record_outcome(
    connection: Any, *, project_id: str, confirmation_id: str,
    requester: str, status_code: int,
) -> None:
    emit_event(connection, project_id, "mcp.confirmation_api_result", requester,
               "mcp_confirmation", confirmation_id,
               {"status_code": status_code})
