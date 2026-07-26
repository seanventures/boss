from __future__ import annotations

import hashlib
import hmac
import json
import os
from secrets import compare_digest
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id, normalize_data_boundary
from .jobs import enqueue_job
from .receipts import ReceiptService


_BOUNDARY_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
    "local-only": 4,
}
_SUPPORTED_GITHUB_EVENTS = {"pull_request", "check_suite", "issue_comment"}
_GITHUB_RECONCILIATION_EVENTS = {
    "push",
    "create",
    "delete",
    "pull_request",
    "pull_request_review",
    "check_run",
    "check_suite",
    "status",
    "branch_protection_rule",
    "repository",
}
_GITHUB_COMMUNITY_ACTIONS = {
    "issues": {"opened", "edited", "reopened"},
    "issue_comment": {"created"},
    "pull_request": {"opened", "synchronize", "reopened"},
    "pull_request_review": {"submitted"},
}
_GITHUB_COMMUNITY_EVENT_TYPES = {
    "issues": "github.issue_opened",
    "issue_comment": "github.issue_comment",
    "pull_request": "github.pr_opened",
    "pull_request_review": "github.pr_review",
}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _endpoint(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "url": row["url"],
        "secret_ref": row["secret_ref"],
        "description": row["description"],
        "data_boundary": row["data_boundary"],
        "enabled": bool(row["enabled"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _delivery(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "endpoint_id": row["endpoint_id"],
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "status": row["status"],
        "dedupe_key": row["dedupe_key"],
        "request_body": row["request_body"] or {},
        "response": row["response"],
        "last_error": row["last_error"],
        "receipt_id": row["receipt_id"],
        "attempt_count": int(row["attempt_count"]),
        "data_boundary": row["data_boundary"],
        "next_attempt_at": _iso(row["next_attempt_at"]),
        "delivered_at": _iso(row["delivered_at"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


class WebhookEndpointNotFoundError(ValueError):
    pass


class WebhookDeliveryNotFoundError(ValueError):
    pass


class WebhookService:
    """Durable outbound webhooks; secret values never enter job payloads."""

    def __init__(
        self,
        settings: Settings,
        post: Optional[Callable[..., httpx.Response]] = None,
    ) -> None:
        self.settings = settings
        self.receipts = ReceiptService()
        self._post = post or httpx.post

    @staticmethod
    def _validate_url(url: str) -> str:
        normalized = url.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Webhook URL must be an absolute http(s) URL.")
        return normalized

    @staticmethod
    def _validate_secret_ref(secret_ref: str) -> str:
        normalized = secret_ref.strip()
        if not normalized.startswith("env:") or not normalized[4:].strip():
            raise ValueError(
                "secret_ref must be an env:NAME reference, never a raw secret."
            )
        return normalized

    @staticmethod
    def _get_endpoint(
        connection: Connection,
        project_id: str,
        endpoint_id: str,
        *,
        enabled_only: bool = False,
    ) -> Optional[Any]:
        query = """
            SELECT id, project_id, url, secret_ref, description, data_boundary,
                   enabled, created_at, updated_at
            FROM webhook_endpoints
            WHERE id = :endpoint_id AND project_id = :project_id
              AND deleted_at IS NULL
        """
        if enabled_only:
            query += " AND enabled = true"
        return (
            connection.execute(
                text(query),
                {"endpoint_id": endpoint_id, "project_id": project_id},
            )
            .mappings()
            .first()
        )

    def list_endpoints(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT id, project_id, url, secret_ref, description, data_boundary,
                       enabled, created_at, updated_at
                FROM webhook_endpoints
                WHERE project_id = :project_id AND deleted_at IS NULL
                ORDER BY created_at DESC
                """
                ),
                {"project_id": project_id},
            )
            .mappings()
            .all()
        )
        return [_endpoint(row) for row in rows]

    def create_endpoint(
        self,
        connection: Connection,
        project_id: str,
        url: str,
        secret_ref: str,
        *,
        description: Optional[str] = None,
        data_boundary: str = "internal",
        enabled: bool = True,
        actor: str = "system",
    ) -> dict[str, Any]:
        endpoint_id = make_id("whend")
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO webhook_endpoints (
                    id, project_id, url, secret_ref, description,
                    data_boundary, enabled
                )
                VALUES (
                    :id, :project_id, :url, :secret_ref, :description,
                    :data_boundary, :enabled
                )
                RETURNING id, project_id, url, secret_ref, description,
                          data_boundary, enabled, created_at, updated_at
                """
                ),
                {
                    "id": endpoint_id,
                    "project_id": project_id,
                    "url": self._validate_url(url),
                    "secret_ref": self._validate_secret_ref(secret_ref),
                    "description": description,
                    "data_boundary": normalize_data_boundary(data_boundary),
                    "enabled": bool(enabled),
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            project_id,
            "webhook.endpoint.created",
            actor,
            "webhook_endpoint",
            endpoint_id,
            {"url": row["url"], "data_boundary": row["data_boundary"]},
        )
        return _endpoint(row)

    def update_endpoint(
        self,
        connection: Connection,
        project_id: str,
        endpoint_id: str,
        *,
        url: Optional[str] = None,
        secret_ref: Optional[str] = None,
        description: Optional[str] = None,
        data_boundary: Optional[str] = None,
        enabled: Optional[bool] = None,
        actor: str = "system",
    ) -> Optional[dict[str, Any]]:
        assignments: list[str] = []
        params: dict[str, Any] = {
            "project_id": project_id,
            "endpoint_id": endpoint_id,
        }
        if url is not None:
            assignments.append("url = :url")
            params["url"] = self._validate_url(url)
        if secret_ref is not None:
            assignments.append("secret_ref = :secret_ref")
            params["secret_ref"] = self._validate_secret_ref(secret_ref)
        if description is not None:
            assignments.append("description = :description")
            params["description"] = description
        if data_boundary is not None:
            assignments.append("data_boundary = :data_boundary")
            params["data_boundary"] = normalize_data_boundary(data_boundary)
        if enabled is not None:
            assignments.append("enabled = :enabled")
            params["enabled"] = bool(enabled)
        if not assignments:
            row = self._get_endpoint(connection, project_id, endpoint_id)
            return _endpoint(row) if row else None
        assignments.append("updated_at = now()")
        row = (
            connection.execute(
                text(
                    f"""
                UPDATE webhook_endpoints
                SET {", ".join(assignments)}
                WHERE id = :endpoint_id AND project_id = :project_id
                  AND deleted_at IS NULL
                RETURNING id, project_id, url, secret_ref, description,
                          data_boundary, enabled, created_at, updated_at
                """
                ),
                params,
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        emit_event(
            connection,
            project_id,
            "webhook.endpoint.updated",
            actor,
            "webhook_endpoint",
            endpoint_id,
            {"enabled": bool(row["enabled"]), "data_boundary": row["data_boundary"]},
        )
        return _endpoint(row)

    def delete_endpoint(
        self,
        connection: Connection,
        project_id: str,
        endpoint_id: str,
        *,
        actor: str = "system",
    ) -> bool:
        deleted = connection.execute(
            text(
                """
                UPDATE webhook_endpoints
                SET enabled = false, deleted_at = now(), updated_at = now()
                WHERE id = :endpoint_id AND project_id = :project_id
                  AND deleted_at IS NULL
                """
            ),
            {"endpoint_id": endpoint_id, "project_id": project_id},
        ).rowcount
        if deleted:
            emit_event(
                connection,
                project_id,
                "webhook.endpoint.deleted",
                actor,
                "webhook_endpoint",
                endpoint_id,
                {},
            )
        return bool(deleted)

    @staticmethod
    def _get_delivery(
        connection: Connection, project_id: str, delivery_id: str, *, lock: bool = False
    ) -> Optional[Any]:
        suffix = " FOR UPDATE" if lock else ""
        return (
            connection.execute(
                text(
                    """
                SELECT id, project_id, endpoint_id, event_id, event_type, status,
                       dedupe_key, request_body, response, last_error, receipt_id,
                       attempt_count, data_boundary, next_attempt_at, delivered_at,
                       created_at, updated_at
                FROM webhook_deliveries
                WHERE id = :delivery_id AND project_id = :project_id
                """
                    + suffix
                ),
                {"delivery_id": delivery_id, "project_id": project_id},
            )
            .mappings()
            .first()
        )

    def list_deliveries(
        self, connection: Connection, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = (
            connection.execute(
                text(
                    """
                SELECT id, project_id, endpoint_id, event_id, event_type, status,
                       dedupe_key, request_body, response, last_error, receipt_id,
                       attempt_count, data_boundary, next_attempt_at, delivered_at,
                       created_at, updated_at
                FROM webhook_deliveries
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
                ),
                {"project_id": project_id, "limit": limit},
            )
            .mappings()
            .all()
        )
        return [_delivery(row) for row in rows]

    def enqueue_delivery(
        self,
        connection: Connection,
        project_id: str,
        endpoint_id: str,
        event_type: str,
        payload: dict[str, Any],
        dedupe_key: str,
        *,
        event_id: Optional[str] = None,
        data_boundary: str = "internal",
        priority: int = 100,
        max_attempts: int = 3,
        actor: str = "system",
    ) -> dict[str, Any]:
        if not event_type.strip():
            raise ValueError("event_type must not be empty.")
        if not dedupe_key.strip():
            raise ValueError("dedupe_key must not be empty.")
        if max_attempts < 1 or max_attempts > 20:
            raise ValueError("max_attempts must be between 1 and 20.")
        existing = connection.execute(
            text(
                """
                SELECT id FROM webhook_deliveries
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).scalar_one_or_none()
        if existing:
            row = self._get_delivery(connection, project_id, existing)
            result = _delivery(row)
            result.update(
                {"created": False, "job_id": self._job_id(connection, existing)}
            )
            return result

        endpoint = self._get_endpoint(
            connection, project_id, endpoint_id, enabled_only=True
        )
        if not endpoint:
            raise WebhookEndpointNotFoundError("Enabled webhook endpoint not found.")
        boundary = normalize_data_boundary(data_boundary)
        endpoint_boundary = normalize_data_boundary(endpoint["data_boundary"])
        if _BOUNDARY_RANK[endpoint_boundary] < _BOUNDARY_RANK[boundary]:
            raise ValueError(
                "Webhook endpoint data boundary cannot receive this event boundary."
            )
        delivery_id = make_id("whdel")
        request_body = {
            "delivery_id": delivery_id,
            "event_id": event_id,
            "event_type": event_type,
            "data": payload,
        }
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO webhook_deliveries (
                    id, project_id, endpoint_id, event_id, event_type,
                    dedupe_key, request_body, data_boundary
                )
                VALUES (
                    :id, :project_id, :endpoint_id, :event_id, :event_type,
                    :dedupe_key, CAST(:request_body AS JSONB), :data_boundary
                )
                ON CONFLICT (project_id, dedupe_key) DO NOTHING
                RETURNING id, project_id, endpoint_id, event_id, event_type, status,
                          dedupe_key, request_body, response, last_error, receipt_id,
                          attempt_count, data_boundary, next_attempt_at, delivered_at,
                          created_at, updated_at
                """
                ),
                {
                    "id": delivery_id,
                    "project_id": project_id,
                    "endpoint_id": endpoint_id,
                    "event_id": event_id,
                    "event_type": event_type.strip(),
                    "dedupe_key": dedupe_key.strip(),
                    "request_body": json.dumps(request_body),
                    "data_boundary": boundary,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            existing = connection.execute(
                text(
                    """
                    SELECT id FROM webhook_deliveries
                    WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                    """
                ),
                {"project_id": project_id, "dedupe_key": dedupe_key},
            ).scalar_one()
            existing_row = self._get_delivery(connection, project_id, existing)
            result = _delivery(existing_row)
            result.update(
                {"created": False, "job_id": self._job_id(connection, existing)}
            )
            return result
        job = enqueue_job(
            connection,
            project_id,
            "webhook.outbound.deliver",
            {"delivery_id": delivery_id, "data_boundary": boundary},
            priority=priority,
            max_attempts=max_attempts,
            data_boundary=boundary,
            dedupe_key=f"webhook-delivery-job:{delivery_id}",
        )
        emit_event(
            connection,
            project_id,
            "webhook.delivery.queued",
            actor,
            "webhook_delivery",
            delivery_id,
            {"event_type": event_type, "endpoint_id": endpoint_id, "job_id": job["id"]},
        )
        result = _delivery(row)
        result.update({"created": True, "job_id": job["id"]})
        return result

    @staticmethod
    def _job_id(connection: Connection, delivery_id: str) -> Optional[str]:
        return connection.execute(
            text(
                """
                SELECT id FROM jobs
                WHERE kind = 'webhook.outbound.deliver'
                  AND payload->>'delivery_id' = :delivery_id
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"delivery_id": delivery_id},
        ).scalar_one_or_none()

    @staticmethod
    def _resolve_secret(secret_ref: str) -> str:
        name = secret_ref[4:].strip() if secret_ref.startswith("env:") else ""
        value = os.environ.get(name) if name else None
        if not value:
            raise ValueError(f"Webhook secret reference {secret_ref!r} is unavailable.")
        return value

    def deliver(
        self,
        connection: Connection,
        project_id: str,
        delivery_id: str,
        created_by_job_id: str,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        row = self._get_delivery(connection, project_id, delivery_id, lock=True)
        if not row:
            raise WebhookDeliveryNotFoundError("Webhook delivery not found.")
        job = (
            connection.execute(
                text(
                    """
                SELECT id, attempt FROM jobs
                WHERE id = :job_id AND project_id = :project_id
                  AND kind = 'webhook.outbound.deliver'
                  AND payload->>'delivery_id' = :delivery_id
                  AND lease_owner = :worker_id
                  AND lease_token_hash = :lease_token_hash
                  AND status IN ('leased', 'running')
                  AND lease_expires_at > now()
                """
                ),
                {
                    "job_id": created_by_job_id,
                    "project_id": project_id,
                    "delivery_id": delivery_id,
                    "worker_id": worker_id,
                    "lease_token_hash": hashlib.sha256(
                        lease_token.encode("utf-8")
                    ).hexdigest(),
                },
            )
            .mappings()
            .first()
        )
        if not job:
            raise ValueError(
                "An active worker lease does not own this webhook delivery."
            )
        if row["status"] == "delivered":
            result = _delivery(row)
            result["created"] = False
            return result
        attempt = max(int(job["attempt"]), 1)
        endpoint = self._get_endpoint(connection, project_id, row["endpoint_id"])
        if not endpoint or not endpoint["enabled"]:
            return self._mark_delivery_failed(
                connection,
                row,
                attempt,
                None,
                "Webhook endpoint is disabled or deleted.",
            )

        project_scope = (
            connection.execute(
                text(
                    """
                SELECT p.id AS project_id, p.workspace_id, w.org_id
                FROM projects p JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
                ),
                {"project_id": project_id},
            )
            .mappings()
            .one()
        )
        receipt, created = self.receipts.create_or_reuse(
            connection,
            project_scope["org_id"],
            project_scope["workspace_id"],
            project_id,
            f"webhook-delivery:{delivery_id}:attempt:{attempt}",
            "webhook.outbound.deliver",
            {
                "endpoint_id": endpoint["id"],
                "endpoint_host": urlparse(endpoint["url"]).netloc,
                "data_boundary": row["data_boundary"],
            },
            row["request_body"] or {},
            created_by_job_id=created_by_job_id,
            data_boundary=row["data_boundary"],
        )
        if not created:
            if receipt["status"] == "succeeded":
                refreshed = (
                    connection.execute(
                        text(
                            """
                        UPDATE webhook_deliveries
                        SET status = 'delivered', receipt_id = :receipt_id,
                            attempt_count = GREATEST(attempt_count, :attempt),
                            delivered_at = COALESCE(delivered_at, now()), updated_at = now()
                        WHERE id = :delivery_id
                        RETURNING id, project_id, endpoint_id, event_id, event_type, status,
                                  dedupe_key, request_body, response, last_error, receipt_id,
                                  attempt_count, data_boundary, next_attempt_at, delivered_at,
                                  created_at, updated_at
                        """
                        ),
                        {
                            "delivery_id": delivery_id,
                            "receipt_id": receipt["id"],
                            "attempt": attempt,
                        },
                    )
                    .mappings()
                    .one()
                )
                result = _delivery(refreshed)
                result["created"] = False
                return result
            result = _delivery(row)
            result.update(
                {
                    "status": "failed",
                    "receipt_id": receipt["id"],
                    "created": False,
                    "message": "Webhook attempt already has a non-success receipt; no duplicate effect was made.",
                }
            )
            return result

        try:
            body = json.dumps(
                row["request_body"] or {},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            secret = self._resolve_secret(endpoint["secret_ref"])
            signature = hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            response = self._post(
                endpoint["url"],
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Boss-Event": row["event_type"],
                    "X-Boss-Delivery": delivery_id,
                    "X-Hub-Signature-256": f"sha256={signature}",
                },
                timeout=self.settings.webhook_timeout_seconds,
            )
            response_data = {
                "status_code": response.status_code,
                "body": response.text[:2000],
            }
            if 200 <= response.status_code < 300:
                self.receipts.mark_succeeded(
                    connection,
                    receipt["id"],
                    f"http:{response.status_code}",
                    response_data,
                )
                delivered = (
                    connection.execute(
                        text(
                            """
                        UPDATE webhook_deliveries
                        SET status = 'delivered', response = CAST(:response AS JSONB),
                            last_error = NULL, receipt_id = :receipt_id,
                            attempt_count = GREATEST(attempt_count, :attempt),
                            next_attempt_at = NULL, delivered_at = now(), updated_at = now()
                        WHERE id = :delivery_id
                        RETURNING id, project_id, endpoint_id, event_id, event_type, status,
                                  dedupe_key, request_body, response, last_error, receipt_id,
                                  attempt_count, data_boundary, next_attempt_at, delivered_at,
                                  created_at, updated_at
                        """
                        ),
                        {
                            "delivery_id": delivery_id,
                            "response": json.dumps(response_data),
                            "receipt_id": receipt["id"],
                            "attempt": attempt,
                        },
                    )
                    .mappings()
                    .one()
                )
                emit_event(
                    connection,
                    project_id,
                    "webhook.delivery.delivered",
                    "worker",
                    "webhook_delivery",
                    delivery_id,
                    {"receipt_id": receipt["id"], "attempt": attempt},
                )
                return _delivery(delivered)
            message = f"Webhook endpoint returned HTTP {response.status_code}."
            self.receipts.mark_failed(
                connection, receipt["id"], {"message": message, **response_data}
            )
            return self._mark_delivery_failed(
                connection, row, attempt, receipt["id"], message, response_data
            )
        except (httpx.HTTPError, ValueError) as error:
            self.receipts.mark_failed(
                connection,
                receipt["id"],
                {"message": str(error), "type": error.__class__.__name__},
            )
            return self._mark_delivery_failed(
                connection, row, attempt, receipt["id"], str(error)
            )

    @staticmethod
    def _mark_delivery_failed(
        connection: Connection,
        row: Any,
        attempt: int,
        receipt_id: Optional[str],
        message: str,
        response: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        failed = (
            connection.execute(
                text(
                    """
                UPDATE webhook_deliveries
                SET status = 'failed', response = CAST(:response AS JSONB),
                    last_error = CAST(:error AS JSONB), receipt_id = :receipt_id,
                    attempt_count = GREATEST(attempt_count, :attempt), updated_at = now()
                WHERE id = :delivery_id
                RETURNING id, project_id, endpoint_id, event_id, event_type, status,
                          dedupe_key, request_body, response, last_error, receipt_id,
                          attempt_count, data_boundary, next_attempt_at, delivered_at,
                          created_at, updated_at
                """
                ),
                {
                    "delivery_id": row["id"],
                    "response": json.dumps(response) if response is not None else None,
                    "error": json.dumps({"message": message}),
                    "receipt_id": receipt_id,
                    "attempt": attempt,
                },
            )
            .mappings()
            .one()
        )
        emit_event(
            connection,
            row["project_id"],
            "webhook.delivery.failed",
            "worker",
            "webhook_delivery",
            row["id"],
            {"receipt_id": receipt_id, "attempt": attempt, "message": message},
        )
        result = _delivery(failed)
        result["message"] = message
        return result


class InboundWebhookService:
    """Authenticates raw provider bytes before durable normalization."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _mapping(raw: str) -> dict[str, str]:
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): str(value) for key, value in parsed.items()}

    @staticmethod
    def _resolve_config_secret(value: str) -> str:
        if value.startswith("env:"):
            return os.environ.get(value[4:].strip(), "")
        return value

    def _secret(self, source: str) -> str:
        configured = self._mapping(self.settings.webhook_inbound_secrets_json)
        fallback = self.settings.webhook_inbound_secret
        if source == "github" and self.settings.github_webhook_secret:
            fallback = self.settings.github_webhook_secret
        return self._resolve_config_secret(
            configured.get(source, fallback)
        )

    def _project_for_repo(self, connection: Connection, repo: str) -> Optional[str]:
        explicit_project_id = self._mapping(
            self.settings.webhook_project_map_json
        ).get(repo)
        project_id = explicit_project_id
        if not project_id and repo:
            bound_projects = connection.execute(
                text(
                    """
                    SELECT DISTINCT project_id
                    FROM github_repository_bindings
                    WHERE status = 'active' AND lower(full_name) = lower(:repo)
                    ORDER BY project_id
                    LIMIT 2
                    """
                ),
                {"repo": repo},
            ).scalars().all()
            if len(bound_projects) == 1:
                project_id = str(bound_projects[0])
        known_repos = {
            f"{self.settings.github_owner}/{self.settings.github_repo}",
            f"{self.settings.target_github_owner}/{self.settings.target_github_repo}",
        }
        if not project_id and repo in known_repos:
            project_id = self.settings.default_project_id
        if not project_id:
            return None
        return connection.execute(
            text("SELECT id FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        ).scalar_one_or_none()

    def ingest(
        self,
        connection: Connection,
        source: str,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        github_community_only: bool = False,
    ) -> dict[str, Any]:
        normalized_source = source.strip().lower()
        signature = headers.get("x-hub-signature-256", "")
        secret = self._secret(normalized_source)
        if not secret:
            return {
                "status": "not_configured",
                "source": normalized_source,
                "delivery_id": headers.get("x-github-delivery", "").strip() or None,
                "duplicate": False,
            }
        expected = (
            "sha256="
            + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        )
        audit_project_id = self.settings.default_project_id
        delivery_key = headers.get("x-github-delivery", "").strip()
        if not signature or not compare_digest(signature, expected):
            emit_event(
                connection,
                audit_project_id,
                "webhook.inbound.rejected",
                normalized_source or "unknown",
                "webhook_delivery",
                delivery_key or "missing",
                {
                    "source": normalized_source,
                    "delivery_id": delivery_key or None,
                    "reason": "invalid_signature",
                },
            )
            return {
                "status": "rejected",
                "source": normalized_source,
                "delivery_id": delivery_key or None,
                "duplicate": False,
            }
        if not delivery_key:
            emit_event(
                connection,
                audit_project_id,
                "webhook.inbound.rejected",
                normalized_source,
                "webhook_delivery",
                "missing",
                {"source": normalized_source, "reason": "missing_delivery_id"},
            )
            return {
                "status": "invalid",
                "source": normalized_source,
                "duplicate": False,
            }
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            emit_event(
                connection,
                audit_project_id,
                "webhook.inbound.rejected",
                normalized_source,
                "webhook_delivery",
                delivery_key,
                {
                    "source": normalized_source,
                    "delivery_id": delivery_key,
                    "reason": "invalid_json",
                },
            )
            return {
                "status": "invalid",
                "source": normalized_source,
                "delivery_id": delivery_key,
                "duplicate": False,
            }
        if not isinstance(payload, dict):
            emit_event(
                connection,
                audit_project_id,
                "webhook.inbound.rejected",
                normalized_source,
                "webhook_delivery",
                delivery_key,
                {
                    "source": normalized_source,
                    "delivery_id": delivery_key,
                    "reason": "invalid_payload_type",
                },
            )
            return {
                "status": "invalid",
                "source": normalized_source,
                "delivery_id": delivery_key,
                "duplicate": False,
            }

        existing = (
            connection.execute(
                text(
                    """
                SELECT id, project_id, event_id, status FROM inbound_webhooks
                WHERE source = :source AND delivery_key = :delivery_key
                """
                ),
                {"source": normalized_source, "delivery_key": delivery_key},
            )
            .mappings()
            .first()
        )
        if existing:
            return {
                "status": existing["status"],
                "source": normalized_source,
                "delivery_id": delivery_key,
                "inbound_webhook_id": existing["id"],
                "event_id": existing["event_id"],
                "project_id": existing["project_id"],
                "duplicate": True,
            }

        provider_event = headers.get("x-github-event", "").strip().lower()
        action = str(payload.get("action") or "").strip().lower()
        repo = str((payload.get("repository") or {}).get("full_name") or "")
        project_id = self._project_for_repo(connection, repo)
        community_event_type = None
        if action in _GITHUB_COMMUNITY_ACTIONS.get(provider_event, set()):
            community_event_type = _GITHUB_COMMUNITY_EVENT_TYPES[provider_event]
        legacy_routed = (
            not github_community_only and provider_event in _SUPPORTED_GITHUB_EVENTS
        )
        reconciliation_routed = bool(
            normalized_source == "github"
            and project_id
            and provider_event in _GITHUB_RECONCILIATION_EVENTS
        )
        sender_login = str((payload.get("sender") or {}).get("login") or "")
        own_sender = bool(
            normalized_source == "github"
            and sender_login
            and sender_login.casefold()
            == self.settings.github_app_bot_login.strip().casefold()
        )
        community_routed = bool(
            project_id
            and (community_event_type or legacy_routed)
            and not own_sender
        )
        routed = bool(community_routed or reconciliation_routed)
        target_project_id = project_id or audit_project_id
        status = (
            "accepted"
            if routed
            else ("ignored" if own_sender else "unrouted")
        )
        inbound_id = make_id("whin")
        inserted = connection.execute(
            text(
                """
                INSERT INTO inbound_webhooks (
                    id, project_id, source, delivery_key, repo_full_name,
                    provider_event, action, status, signature_status,
                    payload_hash, payload
                )
                VALUES (
                    :id, :project_id, :source, :delivery_key, :repo_full_name,
                    :provider_event, :action, :status, 'valid',
                    :payload_hash, CAST(:payload AS JSONB)
                )
                ON CONFLICT (source, delivery_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": inbound_id,
                "project_id": target_project_id,
                "source": normalized_source,
                "delivery_key": delivery_key,
                "repo_full_name": repo or None,
                "provider_event": provider_event or "unknown",
                "action": action or None,
                "status": status,
                "payload_hash": hashlib.sha256(raw_body).hexdigest(),
                "payload": json.dumps(payload),
            },
        ).scalar_one_or_none()
        if not inserted:
            existing = (
                connection.execute(
                    text(
                        """
                    SELECT id, project_id, event_id, status FROM inbound_webhooks
                    WHERE source = :source AND delivery_key = :delivery_key
                    """
                    ),
                    {"source": normalized_source, "delivery_key": delivery_key},
                )
                .mappings()
                .one()
            )
            return {
                "status": existing["status"],
                "source": normalized_source,
                "delivery_id": delivery_key,
                "inbound_webhook_id": existing["id"],
                "event_id": existing["event_id"],
                "project_id": existing["project_id"],
                "duplicate": True,
            }

        event_type = (
            community_event_type
            if community_routed and community_event_type
            else (
                "webhook.inbound.received"
                if community_routed
                else (
                    "github.repository_signal"
                    if reconciliation_routed
                    else (
                        "webhook.inbound.ignored"
                        if own_sender
                        else "webhook.inbound.unrouted"
                    )
                )
            )
        )
        normalized = self._normalize_github_payload(provider_event, payload)
        event_id = emit_event(
            connection,
            target_project_id,
            event_type,
            normalized_source,
            "inbound_webhook",
            inbound_id,
            {
                "source": normalized_source,
                "delivery_id": delivery_key,
                "inbound_webhook_id": inbound_id,
                "github_event": provider_event,
                "action": action or None,
                "repository": repo,
                "data_boundary": "internal",
                "normalized": normalized,
                "reason": (
                    None
                    if routed
                    else (
                        "self_sender"
                        if own_sender
                        else (
                            "unmapped_repository"
                            if not project_id
                            else "unsupported_event_or_action"
                        )
                    )
                ),
            },
        )
        connection.execute(
            text("UPDATE inbound_webhooks SET event_id = :event_id WHERE id = :id"),
            {"event_id": event_id, "id": inbound_id},
        )
        return {
            "status": status,
            "source": normalized_source,
            "delivery_id": delivery_key,
            "inbound_webhook_id": inbound_id,
            "event_id": event_id,
            "project_id": target_project_id,
            "duplicate": False,
        }

    @staticmethod
    def _normalize_github_payload(
        provider_event: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        repository = payload.get("repository") or {}
        sender = payload.get("sender") or {}
        common = {
            "action": payload.get("action"),
            "repository": repository.get("full_name"),
            "sender": {"login": sender.get("login"), "type": sender.get("type")},
            "number": payload.get("number"),
        }
        if provider_event == "issues":
            item = payload.get("issue") or {}
            author = item.get("user") or {}
            return {
                **common,
                "entity_type": "issue",
                "entity_id": item.get("id"),
                "issue": {
                    "id": item.get("id"),
                    "number": item.get("number") or payload.get("number"),
                    "title": item.get("title"),
                    "body": item.get("body"),
                    "html_url": item.get("html_url"),
                    "author": author.get("login"),
                    "labels": [
                        label.get("name") if isinstance(label, dict) else str(label)
                        for label in (item.get("labels") or [])
                    ],
                },
            }
        if provider_event == "pull_request":
            item = payload.get("pull_request") or {}
            author = item.get("user") or {}
            return {
                **common,
                "entity_type": "pull_request",
                "entity_id": item.get("id"),
                "pull_request": {
                    "id": item.get("id"),
                    "number": item.get("number") or payload.get("number"),
                    "title": item.get("title"),
                    "body": item.get("body"),
                    "html_url": item.get("html_url"),
                    "author": author.get("login"),
                    "head_sha": (item.get("head") or {}).get("sha"),
                    "head_ref": (item.get("head") or {}).get("ref"),
                    "base_ref": (item.get("base") or {}).get("ref"),
                    "draft": bool(item.get("draft")),
                },
            }
        if provider_event == "pull_request_review":
            item = payload.get("review") or {}
            author = item.get("user") or {}
            pr = payload.get("pull_request") or {}
            return {
                **common,
                "entity_type": "pull_request_review",
                "entity_id": item.get("id"),
                "pull_request": {
                    "id": pr.get("id"),
                    "number": pr.get("number") or payload.get("number"),
                    "title": pr.get("title"),
                    "html_url": pr.get("html_url"),
                    "head_sha": (pr.get("head") or {}).get("sha"),
                },
                "review": {
                    "id": item.get("id"),
                    "body": item.get("body"),
                    "state": item.get("state"),
                    "author": author.get("login"),
                    "commit_id": item.get("commit_id"),
                },
            }
        if provider_event == "check_suite":
            item = payload.get("check_suite") or {}
            return {**common, "entity_type": "check_suite", "entity_id": item.get("id")}
        item = payload.get("comment") or {}
        author = item.get("user") or {}
        issue = payload.get("issue") or {}
        issue_author = issue.get("user") or {}
        return {
            **common,
            "entity_type": "issue_comment",
            "entity_id": item.get("id"),
            "issue": {
                "id": issue.get("id"),
                "number": issue.get("number") or payload.get("number"),
                "title": issue.get("title"),
                "body": issue.get("body"),
                "html_url": issue.get("html_url"),
                "author": issue_author.get("login"),
                "labels": [
                    label.get("name") if isinstance(label, dict) else str(label)
                    for label in (issue.get("labels") or [])
                ],
                "is_pull_request": bool(issue.get("pull_request")),
            },
            "comment": {
                "id": item.get("id"),
                "body": item.get("body"),
                "html_url": item.get("html_url"),
                "author": author.get("login"),
            },
        }
