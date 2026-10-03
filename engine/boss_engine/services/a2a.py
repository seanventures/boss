from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from typing import Any, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, json_hash, make_id, normalize_data_boundary, now_utc
from .receipts import ReceiptService


class A2ANotFoundError(ValueError):
    pass


class A2AStateError(ValueError):
    pass


class A2ATransportUnavailable(RuntimeError):
    pass


class A2ATransport(Protocol):
    def health(self) -> dict[str, Any]: ...

    def ensure_group(self, stream: str, group: str) -> None: ...

    def publish(
        self, stream: str, fields: dict[str, str], maxlen: int
    ) -> str: ...

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int,
        block_ms: int,
    ) -> list[tuple[str, dict[str, str]]]: ...

    def reclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        count: int,
    ) -> list[tuple[str, dict[str, str]]]: ...

    def acknowledge(self, stream: str, group: str, stream_id: str) -> int: ...


class RedisStreamsTransport:
    """Small Redis Streams adapter. Redis is never the canonical ledger."""

    def __init__(self, redis_url: Optional[str]):
        self.redis_url = redis_url
        self._client: Any = None

    def _redis(self):
        if not self.redis_url:
            raise A2ATransportUnavailable("REDIS_URL is not configured.")
        if self._client is None:
            try:
                import redis
            except ImportError as exc:
                raise A2ATransportUnavailable(
                    "The redis package is not installed."
                ) from exc
            self._client = redis.Redis.from_url(
                self.redis_url, decode_responses=True
            )
        return self._client

    def health(self) -> dict[str, Any]:
        started = time.monotonic()
        if not self.redis_url:
            return {
                "status": "not_configured",
                "configured": False,
                "latency_ms": 0,
            }
        try:
            client = self._redis()
            client.ping()
            return {
                "status": "healthy",
                "configured": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - health must report, never mask
            return {
                "status": "degraded",
                "configured": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": {"type": exc.__class__.__name__, "message": str(exc)},
            }

    def ensure_group(self, stream: str, group: str) -> None:
        client = self._redis()
        try:
            client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP means the durable group already exists
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(
        self, stream: str, fields: dict[str, str], maxlen: int
    ) -> str:
        return str(
            self._redis().xadd(
                stream,
                fields,
                maxlen=max(1000, maxlen),
                approximate=True,
            )
        )

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int,
        block_ms: int,
    ) -> list[tuple[str, dict[str, str]]]:
        response = self._redis().xreadgroup(
            group,
            consumer,
            {stream: ">"},
            count=count,
            block=block_ms if block_ms > 0 else None,
        )
        return [
            (str(stream_id), dict(fields))
            for _, messages in response
            for stream_id, fields in messages
        ]

    def reclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        count: int,
    ) -> list[tuple[str, dict[str, str]]]:
        response = self._redis().xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=max(1, min_idle_ms),
            start_id="0-0",
            count=count,
        )
        messages = response[1] if len(response) > 1 else []
        return [
            (str(stream_id), dict(fields))
            for stream_id, fields in messages
        ]

    def acknowledge(self, stream: str, group: str, stream_id: str) -> int:
        return int(self._redis().xack(stream, group, stream_id))


class A2AService:
    MESSAGE_KINDS = {
        "instruction",
        "status",
        "question",
        "answer",
        "task_offer",
        "task_accept",
        "resource_request",
        "resource_grant",
        "artifact",
        "evidence",
        "decision_request",
        "decision_result",
        "metric",
        "heartbeat",
        "error",
        "cancel",
        "task.coordinate",
        "job.request",
        "job.status",
        "resource.request",
        "resource.response",
        "handoff",
        "barrier.arrive",
        "barrier.release",
        "telemetry",
        "finding",
        "dissent",
        "reply",
    }
    SENDER_KINDS = {"agent", "panel", "workflow", "worker", "system", "operator"}
    ACK_MODES = {"none", "accepted", "processed"}

    def __init__(
        self, settings: Settings, transport: Optional[A2ATransport] = None
    ):
        self.settings = settings
        self.transport = transport or RedisStreamsTransport(settings.redis_url)
        self.receipts = ReceiptService()

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    def create_channel(
        self,
        connection: Connection,
        *,
        project_id: str,
        channel_key: str,
        name: str,
        description: str,
        allowed_message_kinds: Optional[list[str]],
        allowed_schema_versions: Optional[dict[str, Any]],
        retention_seconds: int,
        max_payload_bytes: int,
        actor: str,
    ) -> dict[str, Any]:
        key = self._key(channel_key, "channel_key")
        kinds = sorted(set(allowed_message_kinds or self.MESSAGE_KINDS))
        unknown = sorted(set(kinds) - self.MESSAGE_KINDS)
        if unknown:
            raise ValueError("Unknown A2A message kinds: " + ", ".join(unknown))
        existing = connection.execute(
            text(
                "SELECT id FROM a2a_channels "
                "WHERE project_id=:project_id AND channel_key=:channel_key"
            ),
            {"project_id": project_id, "channel_key": key},
        ).scalar_one_or_none()
        if existing:
            return self.get_channel(connection, project_id, existing)
        channel_id = make_id("a2ach")
        connection.execute(
            text(
                """
                INSERT INTO a2a_channels (
                    id, project_id, channel_key, name, description,
                    allowed_message_kinds, allowed_schema_versions,
                    retention_seconds, max_payload_bytes, created_by
                )
                VALUES (
                    :id, :project_id, :channel_key, :name, :description,
                    CAST(:kinds AS JSONB), CAST(:versions AS JSONB),
                    :retention, :max_payload, :created_by
                )
                """
            ),
            {
                "id": channel_id,
                "project_id": project_id,
                "channel_key": key,
                "name": self._required(name, "name"),
                "description": str(description or "").strip(),
                "kinds": json.dumps(kinds),
                "versions": json.dumps(allowed_schema_versions or {}),
                "retention": max(1, int(retention_seconds)),
                "max_payload": max(1024, int(max_payload_bytes)),
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "a2a.channel_created",
            actor,
            "a2a_channel",
            channel_id,
            {"channel_key": key},
        )
        return self.get_channel(connection, project_id, channel_id)

    def list_channels(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT c.*,
                       (SELECT count(*) FROM a2a_subscriptions s
                        WHERE s.channel_id=c.id AND s.status='active') AS subscriber_count,
                       (SELECT count(*) FROM a2a_envelopes e
                        WHERE e.channel_id=c.id) AS message_count
                FROM a2a_channels c
                WHERE c.project_id=:project_id
                ORDER BY c.channel_key
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize(dict(row)) for row in rows]

    def get_channel(
        self, connection: Connection, project_id: str, channel_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                "SELECT * FROM a2a_channels "
                "WHERE id=:id AND project_id=:project_id"
            ),
            {"id": channel_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise A2ANotFoundError("A2A channel not found.")
        return self._serialize(dict(row))

    def create_subscription(
        self,
        connection: Connection,
        *,
        project_id: str,
        channel_id: str,
        subscriber_kind: str,
        subscriber_id: str,
        consumer_group: Optional[str],
        recipient_filter: Optional[dict[str, Any]],
        ack_mode: str,
        visibility_timeout_seconds: int,
        max_attempts: int,
        data_boundaries: Optional[list[str]],
        actor: str,
    ) -> dict[str, Any]:
        channel = self.get_channel(connection, project_id, channel_id)
        if channel["status"] != "active":
            raise A2AStateError("Subscriptions require an active channel.")
        if subscriber_kind not in self.SENDER_KINDS:
            raise ValueError("Invalid subscriber_kind.")
        if ack_mode not in self.ACK_MODES:
            raise ValueError("Invalid ack_mode.")
        boundaries = [
            normalize_data_boundary(value)
            for value in (data_boundaries or ["public", "internal"])
        ]
        group = self._key(
            consumer_group or f"{subscriber_kind}_{subscriber_id}", "consumer_group"
        )
        existing = connection.execute(
            text(
                """
                SELECT id FROM a2a_subscriptions
                WHERE channel_id=:channel_id AND consumer_group=:consumer_group
                  AND subscriber_kind=:subscriber_kind
                  AND subscriber_id=:subscriber_id
                """
            ),
            {
                "channel_id": channel_id,
                "consumer_group": group,
                "subscriber_kind": subscriber_kind,
                "subscriber_id": subscriber_id,
            },
        ).scalar_one_or_none()
        if existing:
            return self.get_subscription(connection, project_id, existing)
        subscription_id = make_id("a2asub")
        connection.execute(
            text(
                """
                INSERT INTO a2a_subscriptions (
                    id, project_id, channel_id, subscriber_kind, subscriber_id,
                    consumer_group, recipient_filter, ack_mode,
                    visibility_timeout_seconds, max_attempts, data_boundaries,
                    created_by
                )
                VALUES (
                    :id, :project_id, :channel_id, :subscriber_kind, :subscriber_id,
                    :consumer_group, CAST(:recipient_filter AS JSONB), :ack_mode,
                    :visibility_timeout, :max_attempts,
                    CAST(:data_boundaries AS JSONB), :created_by
                )
                """
            ),
            {
                "id": subscription_id,
                "project_id": project_id,
                "channel_id": channel_id,
                "subscriber_kind": subscriber_kind,
                "subscriber_id": self._required(subscriber_id, "subscriber_id"),
                "consumer_group": group,
                "recipient_filter": json.dumps(recipient_filter or {}),
                "ack_mode": ack_mode,
                "visibility_timeout": max(5, min(3600, visibility_timeout_seconds)),
                "max_attempts": max(1, min(20, max_attempts)),
                "data_boundaries": json.dumps(boundaries),
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "a2a.subscription_created",
            actor,
            "a2a_subscription",
            subscription_id,
            {
                "channel_id": channel_id,
                "subscriber_kind": subscriber_kind,
                "subscriber_id": subscriber_id,
            },
        )
        return self.get_subscription(connection, project_id, subscription_id)

    def get_subscription(
        self, connection: Connection, project_id: str, subscription_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT s.*, c.channel_key
                FROM a2a_subscriptions s
                JOIN a2a_channels c ON c.id=s.channel_id
                WHERE s.id=:id AND s.project_id=:project_id
                """
            ),
            {"id": subscription_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise A2ANotFoundError("A2A subscription not found.")
        return self._serialize(dict(row))

    def list_subscriptions(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        ids = connection.execute(
            text(
                "SELECT id FROM a2a_subscriptions WHERE project_id=:project_id "
                "ORDER BY created_at"
            ),
            {"project_id": project_id},
        ).scalars().all()
        return [self.get_subscription(connection, project_id, item) for item in ids]

    # ------------------------------------------------------------------
    # Durable publish / dispatch
    # ------------------------------------------------------------------
    def publish(
        self,
        connection: Connection,
        *,
        project_id: str,
        channel_id: str,
        message_kind: str,
        schema_name: str,
        schema_version: int,
        sender_kind: str,
        sender_id: str,
        recipient: dict[str, Any],
        payload: dict[str, Any],
        dedupe_key: str,
        actor: str,
        data_boundary: str = "internal",
        priority: int = 100,
        ttl_seconds: Optional[int] = None,
        ack_mode: str = "processed",
        conversation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        reply_to_id: Optional[str] = None,
        lineage: Optional[dict[str, Any]] = None,
        sender_version_id: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = connection.execute(
            text(
                "SELECT id FROM a2a_envelopes "
                "WHERE project_id=:project_id AND dedupe_key=:dedupe_key"
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).scalar_one_or_none()
        if existing:
            result = self.get_message(connection, project_id, existing)
            result["duplicate"] = True
            return result
        channel = self.get_channel(connection, project_id, channel_id)
        if channel["status"] != "active":
            raise A2AStateError("Messages require an active channel.")
        if message_kind not in self.MESSAGE_KINDS:
            raise ValueError("Invalid message_kind.")
        if channel["allowed_message_kinds"] and message_kind not in channel[
            "allowed_message_kinds"
        ]:
            raise A2AStateError("Message kind is not allowed on this channel.")
        schema_versions = channel["allowed_schema_versions"] or {}
        if schema_versions:
            allowed_versions = schema_versions.get(schema_name)
            if allowed_versions is None:
                raise A2AStateError(
                    "Message schema is not allowed on this channel."
                )
            if (
                not isinstance(allowed_versions, list)
                or int(schema_version) not in allowed_versions
            ):
                raise A2AStateError(
                    "Message schema version is not allowed on this channel."
                )
        if sender_kind not in self.SENDER_KINDS:
            raise ValueError("Invalid sender_kind.")
        if ack_mode not in self.ACK_MODES:
            raise ValueError("Invalid ack_mode.")
        boundary = normalize_data_boundary(data_boundary)
        payload_bytes = len(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        size_limit = min(
            int(channel["max_payload_bytes"]), self.settings.a2a_max_payload_bytes
        )
        if payload_bytes > size_limit:
            raise ValueError(
                f"A2A payload is {payload_bytes} bytes; limit is {size_limit}."
            )
        ttl = int(ttl_seconds or self.settings.a2a_default_ttl_seconds)
        ttl = max(1, min(ttl, int(channel["retention_seconds"]), 604800))
        message_id = make_id("a2amsg")
        correlation = correlation_id or message_id
        trace = trace_id or correlation
        expires_at = now_utc() + timedelta(seconds=ttl)
        connection.execute(
            text(
                """
                INSERT INTO a2a_envelopes (
                    id, project_id, channel_id, message_kind, schema_name,
                    schema_version, sender_kind, sender_id, sender_version_id,
                    recipient, data_boundary, priority, ttl_seconds, expires_at,
                    ack_mode, conversation_id, correlation_id, causation_id,
                    trace_id, reply_to_id, lineage, payload, payload_hash,
                    dedupe_key, created_by
                )
                VALUES (
                    :id, :project_id, :channel_id, :message_kind, :schema_name,
                    :schema_version, :sender_kind, :sender_id, :sender_version_id,
                    CAST(:recipient AS JSONB), :data_boundary, :priority,
                    :ttl_seconds, :expires_at, :ack_mode, :conversation_id,
                    :correlation_id, :causation_id, :trace_id, :reply_to_id,
                    CAST(:lineage AS JSONB), CAST(:payload AS JSONB),
                    :payload_hash, :dedupe_key, :created_by
                )
                """
            ),
            {
                "id": message_id,
                "project_id": project_id,
                "channel_id": channel_id,
                "message_kind": message_kind,
                "schema_name": self._required(schema_name, "schema_name"),
                "schema_version": max(1, int(schema_version)),
                "sender_kind": sender_kind,
                "sender_id": self._required(sender_id, "sender_id"),
                "sender_version_id": sender_version_id,
                "recipient": json.dumps(recipient),
                "data_boundary": boundary,
                "priority": int(priority),
                "ttl_seconds": ttl,
                "expires_at": expires_at,
                "ack_mode": ack_mode,
                "conversation_id": conversation_id,
                "correlation_id": correlation,
                "causation_id": causation_id,
                "trace_id": trace,
                "reply_to_id": reply_to_id,
                "lineage": json.dumps(lineage or {}),
                "payload": json.dumps(payload),
                "payload_hash": json_hash(payload),
                "dedupe_key": self._required(dedupe_key, "dedupe_key"),
                "created_by": actor,
            },
        )
        subscriptions = connection.execute(
            text(
                """
                SELECT id, recipient_filter, data_boundaries
                FROM a2a_subscriptions
                WHERE project_id=:project_id AND channel_id=:channel_id
                  AND status='active'
                """
            ),
            {"project_id": project_id, "channel_id": channel_id},
        ).mappings().all()
        matched_subscriptions = [
            item
            for item in subscriptions
            if boundary in (item["data_boundaries"] or [])
            and self._recipient_matches(recipient, item["recipient_filter"] or {})
        ]
        for subscription in matched_subscriptions:
            connection.execute(
                text(
                    """
                    INSERT INTO a2a_deliveries (
                        id, project_id, envelope_id, subscription_id
                    ) VALUES (:id, :project_id, :envelope_id, :subscription_id)
                    """
                ),
                {
                    "id": make_id("a2adel"),
                    "project_id": project_id,
                    "envelope_id": message_id,
                    "subscription_id": subscription["id"],
                },
            )
        outbox_id = make_id("a2aout")
        job_id = make_id("job")
        job_payload = {
            "message_id": message_id,
            "outbox_id": outbox_id,
            "channel_id": channel_id,
            "requires_reservation": False,
        }
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, project_id, kind, priority, status, payload,
                    data_boundary, max_attempts, attempt, dedupe_key, run_after
                )
                VALUES (
                    :id, :project_id, 'a2a.dispatch', :priority, 'queued',
                    CAST(:payload AS JSONB), :data_boundary, :max_attempts, 0,
                    :dedupe_key, :run_after
                )
                """
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "priority": int(priority),
                "payload": json.dumps(job_payload),
                "data_boundary": boundary,
                "max_attempts": self.settings.a2a_dispatch_max_attempts,
                "dedupe_key": f"a2a-dispatch:{dedupe_key}",
                "run_after": now_utc(),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO a2a_outbox (id, project_id, envelope_id, job_id)
                VALUES (:id, :project_id, :envelope_id, :job_id)
                """
            ),
            {
                "id": outbox_id,
                "project_id": project_id,
                "envelope_id": message_id,
                "job_id": job_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "a2a.message_queued",
            actor,
            "a2a_envelope",
            message_id,
            {
                "channel_id": channel_id,
                "message_kind": message_kind,
                "delivery_count": len(matched_subscriptions),
                "job_id": job_id,
                "trace_id": trace,
            },
        )
        if sender_kind == "agent":
            # AN AGENT ASKED AN AGENT, and this envelope is the proof. It is the only place the
            # ledger may write `asked_by_agent` (agent_acts.record_handoff); everything else that
            # moves work between agents is BOSS, a captain or a person, and is labelled so.
            from . import agent_acts

            agent_acts.quietly(
                connection, "sign an agent's handoff", agent_acts.record_handoff,
                project_id=project_id, envelope_id=message_id, sender_id=sender_id,
                recipient=recipient, actor=actor, lineage=lineage, message_kind=message_kind)
        return self.get_message(connection, project_id, message_id)

    def dispatch(
        self,
        connection: Connection,
        *,
        project_id: str,
        message_id: str,
        created_by_job_id: str,
        actor: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT e.*, c.channel_key, c.retention_seconds, o.id AS outbox_id,
                       o.status AS outbox_status, o.attempt AS outbox_attempt
                FROM a2a_envelopes e
                JOIN a2a_channels c ON c.id=e.channel_id
                JOIN a2a_outbox o ON o.envelope_id=e.id
                WHERE e.id=:id AND e.project_id=:project_id
                FOR UPDATE
                """
            ),
            {"id": message_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise A2ANotFoundError("A2A message not found.")
        if row["status"] in {"published", "delivering", "acknowledged"}:
            result = self.get_message(connection, project_id, message_id)
            result["duplicate"] = True
            return result
        if row["status"] in {"cancelled", "expired", "deadlettered"}:
            raise A2AStateError(f"Cannot dispatch a {row['status']} message.")
        if row["expires_at"] <= now_utc():
            connection.execute(
                text(
                    "UPDATE a2a_envelopes SET status='expired', updated_at=now() "
                    "WHERE id=:id"
                ),
                {"id": message_id},
            )
            raise A2AStateError("A2A message expired before dispatch.")
        project = connection.execute(
            text(
                """
                SELECT p.workspace_id, w.org_id
                FROM projects p JOIN workspaces w ON w.id=p.workspace_id
                WHERE p.id=:project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        stream = self._stream(project_id, row["channel_key"])
        transport_payload = self._transport_payload(dict(row))
        receipt, created = self.receipts.create_or_reuse(
            connection,
            org_id=project["org_id"],
            workspace_id=project["workspace_id"],
            project_id=project_id,
            dedupe_key=f"a2a:dispatch:{message_id}",
            effect_kind="a2a.redis.publish",
            effect_scope={
                "message_id": message_id,
                "channel_id": row["channel_id"],
                "stream": stream,
                "data_boundary": row["data_boundary"],
            },
            payload=transport_payload,
            created_by_job_id=created_by_job_id,
            data_boundary=row["data_boundary"],
        )
        if not created and receipt["status"] == "succeeded":
            stream_id = (receipt.get("result") or {}).get("redis_stream_id")
            self._mark_published(
                connection, row, receipt["id"], stream_id, actor
            )
            return self.get_message(connection, project_id, message_id)
        attempt_id = make_id("a2aattempt")
        attempt = int(row["outbox_attempt"]) + 1
        attempt_params = {
            "id": message_id,
            "outbox_id": row["outbox_id"],
            "attempt": attempt,
            "attempt_id": attempt_id,
            "project_id": project_id,
            "message_id": message_id,
        }
        connection.execute(
            text(
                "UPDATE a2a_envelopes SET status='dispatching', updated_at=now() "
                "WHERE id=:id"
            ),
            attempt_params,
        )
        connection.execute(
            text(
                "UPDATE a2a_outbox SET status='dispatching', attempt=:attempt, "
                "updated_at=now() WHERE id=:outbox_id"
            ),
            attempt_params,
        )
        connection.execute(
            text(
                """
                INSERT INTO a2a_transport_attempts (
                    id, project_id, envelope_id, operation, status, attempt
                ) VALUES (
                    :attempt_id, :project_id, :message_id, 'xadd', 'started', :attempt
                )
                """
            ),
            attempt_params,
        )
        started = time.monotonic()
        try:
            stream_id = self.transport.publish(
                stream,
                {
                    "message_id": message_id,
                    "project_id": project_id,
                    "trace_id": row["trace_id"],
                    "envelope": json.dumps(transport_payload, sort_keys=True),
                },
                maxlen=max(1000, int(row["retention_seconds"])),
            )
        except Exception as exc:
            error = {"type": exc.__class__.__name__, "message": str(exc)}
            self.receipts.mark_failed(connection, receipt["id"], error)
            failure_params = {
                "outbox_id": row["outbox_id"],
                "message_id": message_id,
                "attempt_id": attempt_id,
                "latency": int((time.monotonic() - started) * 1000),
                "error": json.dumps(error),
            }
            connection.execute(
                text(
                    "UPDATE a2a_outbox SET status='retry_wait', "
                    "last_error=CAST(:error AS JSONB), updated_at=now() "
                    "WHERE id=:outbox_id"
                ),
                failure_params,
            )
            connection.execute(
                text(
                    "UPDATE a2a_envelopes SET status='queued', updated_at=now() "
                    "WHERE id=:message_id"
                ),
                failure_params,
            )
            connection.execute(
                text(
                    "UPDATE a2a_transport_attempts SET status='failed', "
                    "latency_ms=:latency, error=CAST(:error AS JSONB) "
                    "WHERE id=:attempt_id"
                ),
                failure_params,
            )
            emit_event(
                connection,
                project_id,
                "a2a.dispatch_failed",
                actor,
                "a2a_envelope",
                message_id,
                {"attempt": attempt, "error": error},
            )
            raise A2ATransportUnavailable(str(exc)) from exc
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            stream_id,
            {"redis_stream_id": stream_id, "stream": stream},
        )
        connection.execute(
            text(
                """
                UPDATE a2a_transport_attempts
                SET status='succeeded', redis_stream_id=:stream_id,
                    latency_ms=:latency WHERE id=:attempt_id
                """
            ),
            {
                "stream_id": stream_id,
                "latency": int((time.monotonic() - started) * 1000),
                "attempt_id": attempt_id,
            },
        )
        self._mark_published(connection, row, receipt["id"], stream_id, actor)
        return self.get_message(connection, project_id, message_id)

    def _mark_published(
        self,
        connection: Connection,
        row: Any,
        receipt_id: str,
        stream_id: str,
        actor: str,
    ) -> None:
        status = "delivering" if self._delivery_count(connection, row["id"]) else "published"
        params = {
            "status": status,
            "receipt_id": receipt_id,
            "stream_id": stream_id,
            "message_id": row["id"],
        }
        connection.execute(
            text(
                "UPDATE a2a_envelopes SET status=:status, "
                "receipt_id=:receipt_id, redis_stream_id=:stream_id, "
                "updated_at=now() WHERE id=:message_id"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE a2a_outbox SET status='published', updated_at=now() "
                "WHERE envelope_id=:message_id"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE a2a_deliveries SET status='published', "
                "redis_stream_id=:stream_id, updated_at=now() "
                "WHERE envelope_id=:message_id AND status='pending'"
            ),
            params,
        )
        emit_event(
            connection,
            row["project_id"],
            "a2a.message_published",
            actor,
            "a2a_envelope",
            row["id"],
            {
                "redis_stream_id": stream_id,
                "receipt_id": receipt_id,
                "delivery_count": self._delivery_count(connection, row["id"]),
            },
        )

    # ------------------------------------------------------------------
    # Consume / acknowledge / inspect
    # ------------------------------------------------------------------
    def poll(
        self,
        connection: Connection,
        *,
        project_id: str,
        subscription_id: str,
        consumer_name: str,
        count: int,
        block_ms: int,
        actor: str,
    ) -> list[dict[str, Any]]:
        subscription = self.get_subscription(
            connection, project_id, subscription_id
        )
        if subscription["status"] != "active":
            raise A2AStateError("Subscription is not active.")
        channel = self.get_channel(
            connection, project_id, subscription["channel_id"]
        )
        stream = self._stream(project_id, channel["channel_key"])
        self.transport.ensure_group(stream, subscription["consumer_group"])
        batch_size = max(1, min(100, count))
        reclaimed = self.transport.reclaim(
            stream,
            subscription["consumer_group"],
            self._key(consumer_name, "consumer_name"),
            int(subscription["visibility_timeout_seconds"]) * 1000,
            batch_size,
        )
        fresh = self.transport.consume(
            stream,
            subscription["consumer_group"],
            self._key(consumer_name, "consumer_name"),
            batch_size,
            0 if reclaimed else max(0, min(5000, block_ms)),
        )
        messages: list[tuple[str, dict[str, str]]] = []
        seen_stream_ids: set[str] = set()
        for item in [*reclaimed, *fresh]:
            if item[0] in seen_stream_ids:
                continue
            seen_stream_ids.add(item[0])
            messages.append(item)
            if len(messages) >= batch_size:
                break
        claimed: list[dict[str, Any]] = []
        for stream_id, fields in messages:
            message_id = fields.get("message_id")
            if not message_id:
                continue
            delivery = connection.execute(
                text(
                    """
                    SELECT d.id, d.status, d.attempt, s.max_attempts,
                           s.visibility_timeout_seconds, s.consumer_group,
                           e.data_boundary, c.channel_key
                    FROM a2a_deliveries d
                    JOIN a2a_subscriptions s ON s.id=d.subscription_id
                    JOIN a2a_envelopes e ON e.id=d.envelope_id
                    JOIN a2a_channels c ON c.id=e.channel_id
                    WHERE d.envelope_id=:message_id
                      AND d.subscription_id=:subscription_id
                    FOR UPDATE
                    """
                ),
                {
                    "message_id": message_id,
                    "subscription_id": subscription_id,
                },
            ).mappings().first()
            if delivery is None:
                boundary = connection.execute(
                    text(
                        "SELECT data_boundary FROM a2a_envelopes "
                        "WHERE id=:id AND project_id=:project_id"
                    ),
                    {"id": message_id, "project_id": project_id},
                ).scalar_one_or_none()
                transport_ack = self._ack_transport_cursor(
                    connection,
                    project_id=project_id,
                    message_id=message_id,
                    delivery_id=None,
                    channel_key=channel["channel_key"],
                    consumer_group=subscription["consumer_group"],
                    stream_id=stream_id,
                    data_boundary=boundary or "internal",
                )
                emit_event(
                    connection,
                    project_id,
                    "a2a.message_filtered",
                    actor,
                    "a2a_subscription",
                    subscription_id,
                    {
                        "message_id": message_id,
                        "reason": "recipient_filter_not_matched",
                        "transport_ack": transport_ack,
                    },
                )
                continue
            if delivery["status"] in {
                "acknowledged",
                "cancelled",
                "expired",
                "deadlettered",
            }:
                self._ack_transport_cursor(
                    connection,
                    project_id=project_id,
                    message_id=message_id,
                    delivery_id=delivery["id"],
                    channel_key=delivery["channel_key"],
                    consumer_group=delivery["consumer_group"],
                    stream_id=stream_id,
                    data_boundary=delivery["data_boundary"],
                )
                continue
            attempt = int(delivery["attempt"]) + 1
            if attempt > int(delivery["max_attempts"]):
                transport_ack = self._ack_transport_cursor(
                    connection,
                    project_id=project_id,
                    message_id=message_id,
                    delivery_id=delivery["id"],
                    channel_key=delivery["channel_key"],
                    consumer_group=delivery["consumer_group"],
                    stream_id=stream_id,
                    data_boundary=delivery["data_boundary"],
                )
                connection.execute(
                    text(
                        "UPDATE a2a_deliveries SET status='deadlettered', "
                        "last_error=CAST(:error AS JSONB), "
                        "visibility_expires_at=NULL, lease_owner=NULL, "
                        "updated_at=now() WHERE id=:id"
                    ),
                    {
                        "id": delivery["id"],
                        "error": json.dumps(
                            {
                                "type": "A2ADeliveryAttemptsExhausted",
                                "attempt": attempt,
                                "max_attempts": int(delivery["max_attempts"]),
                            }
                        ),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE a2a_envelopes SET status='deadlettered', "
                        "updated_at=now() WHERE id=:id"
                    ),
                    {"id": message_id},
                )
                emit_event(
                    connection,
                    project_id,
                    "a2a.delivery_deadlettered",
                    actor,
                    "a2a_delivery",
                    delivery["id"],
                    {
                        "message_id": message_id,
                        "attempt": attempt,
                        "max_attempts": int(delivery["max_attempts"]),
                        "transport_ack": transport_ack,
                    },
                )
                continue
            visibility = now_utc() + timedelta(
                seconds=int(delivery["visibility_timeout_seconds"])
            )
            connection.execute(
                text(
                    """
                    UPDATE a2a_deliveries
                    SET status='claimed', attempt=:attempt,
                        redis_stream_id=:stream_id, consumer_name=:consumer,
                        lease_owner=:consumer, visibility_expires_at=:visibility,
                        claimed_at=now(), updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": delivery["id"],
                    "attempt": attempt,
                    "stream_id": stream_id,
                    "consumer": consumer_name,
                    "visibility": visibility,
                },
            )
            item = self.get_message(connection, project_id, message_id)
            item["delivery_id"] = delivery["id"]
            item["consumer_group"] = subscription["consumer_group"]
            claimed.append(item)
            emit_event(
                connection,
                project_id,
                "a2a.delivery_claimed",
                actor,
                "a2a_delivery",
                delivery["id"],
                {
                    "message_id": message_id,
                    "consumer_name": consumer_name,
                    "attempt": attempt,
                },
            )
            if subscription["ack_mode"] == "none":
                acknowledgement = self.acknowledge(
                    connection,
                    project_id=project_id,
                    delivery_id=delivery["id"],
                    acknowledgement="processed",
                    actor_kind=subscription["subscriber_kind"],
                    actor_id=subscription["subscriber_id"],
                    result={"auto_acknowledged": True},
                    evidence_refs=[],
                    reason="Subscription ack_mode is none.",
                )
                item.update(
                    self.get_message(connection, project_id, message_id)
                )
                item["delivery_id"] = delivery["id"]
                item["consumer_group"] = subscription["consumer_group"]
                item["auto_acknowledgement"] = acknowledgement
        return claimed

    def acknowledge(
        self,
        connection: Connection,
        *,
        project_id: str,
        delivery_id: str,
        acknowledgement: str,
        actor_kind: str,
        actor_id: str,
        result: Optional[dict[str, Any]],
        evidence_refs: Optional[list[str]],
        reason: Optional[str],
    ) -> dict[str, Any]:
        if acknowledgement not in {"accepted", "processed", "rejected", "deferred"}:
            raise ValueError("Invalid acknowledgement.")
        delivery = connection.execute(
            text(
                """
                SELECT d.*, s.consumer_group, s.ack_mode,
                       s.subscriber_kind, s.subscriber_id, c.channel_key,
                       e.status AS envelope_status, e.data_boundary
                FROM a2a_deliveries d
                JOIN a2a_subscriptions s ON s.id=d.subscription_id
                JOIN a2a_channels c ON c.id=s.channel_id
                JOIN a2a_envelopes e ON e.id=d.envelope_id
                WHERE d.id=:id AND d.project_id=:project_id
                FOR UPDATE
                """
            ),
            {"id": delivery_id, "project_id": project_id},
        ).mappings().first()
        if delivery is None:
            raise A2ANotFoundError("A2A delivery not found.")
        if (
            delivery["subscriber_kind"] != actor_kind
            or delivery["subscriber_id"] != actor_id
        ):
            raise A2AStateError(
                "Acknowledgement actor does not own this subscription delivery."
            )
        if delivery["status"] == "acknowledged":
            return self.get_delivery(connection, project_id, delivery_id)
        final = acknowledgement in {"processed", "rejected"} or (
            acknowledgement == "accepted" and delivery["ack_mode"] == "accepted"
        )
        transport_ack = None
        receipt_id = None
        ack_id = make_id("a2aack")
        connection.execute(
            text(
                """
                INSERT INTO a2a_acknowledgements (
                    id, project_id, delivery_id, acknowledgement, actor_kind,
                    actor_id, result, evidence_refs, reason, receipt_id
                ) VALUES (
                    :id, :project_id, :delivery_id, :acknowledgement, :actor_kind,
                    :actor_id, CAST(:result AS JSONB),
                    CAST(:evidence_refs AS JSONB), :reason, :receipt_id
                )
                """
            ),
            {
                "id": ack_id,
                "project_id": project_id,
                "delivery_id": delivery_id,
                "acknowledgement": acknowledgement,
                "actor_kind": actor_kind,
                "actor_id": actor_id,
                "result": json.dumps(result or {}),
                "evidence_refs": json.dumps(evidence_refs or []),
                "reason": reason,
                "receipt_id": receipt_id,
            },
        )
        if final and delivery["redis_stream_id"]:
            transport_ack = self._ack_transport_cursor(
                connection,
                project_id=project_id,
                message_id=delivery["envelope_id"],
                delivery_id=delivery_id,
                channel_key=delivery["channel_key"],
                consumer_group=delivery["consumer_group"],
                stream_id=delivery["redis_stream_id"],
                data_boundary=delivery["data_boundary"],
            )
            receipt_id = transport_ack["receipt_id"]
            connection.execute(
                text(
                    "UPDATE a2a_acknowledgements SET receipt_id=:receipt_id "
                    "WHERE id=:id"
                ),
                {"receipt_id": receipt_id, "id": ack_id},
            )
        if final:
            connection.execute(
                text(
                    """
                    UPDATE a2a_deliveries
                    SET status='acknowledged', acknowledged_at=now(),
                        visibility_expires_at=NULL, lease_owner=NULL,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {"id": delivery_id},
            )
        else:
            next_status = "processing" if acknowledgement == "accepted" else "retry_wait"
            connection.execute(
                text(
                    "UPDATE a2a_deliveries SET status=:status, updated_at=now() "
                    "WHERE id=:id"
                ),
                {"status": next_status, "id": delivery_id},
            )
        self._refresh_envelope_status(connection, delivery["envelope_id"])
        emit_event(
            connection,
            project_id,
            "a2a.delivery_acknowledged",
            actor_id,
            "a2a_delivery",
            delivery_id,
            {
                "acknowledgement": acknowledgement,
                "final": final,
                "evidence_refs": evidence_refs or [],
                "transport_ack": transport_ack,
            },
        )
        return self.get_delivery(connection, project_id, delivery_id)

    def cancel(
        self,
        connection: Connection,
        *,
        project_id: str,
        message_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        message = connection.execute(
            text(
                """
                SELECT e.id, e.status, e.data_boundary, c.channel_key, o.job_id
                FROM a2a_envelopes e
                JOIN a2a_channels c ON c.id=e.channel_id
                LEFT JOIN a2a_outbox o ON o.envelope_id=e.id
                WHERE e.id=:id AND e.project_id=:project_id
                FOR UPDATE OF e
                """
            ),
            {"id": message_id, "project_id": project_id},
        ).mappings().first()
        if message is None:
            raise A2ANotFoundError("A2A message not found.")
        if message["status"] == "cancelled":
            result = self.get_message(connection, project_id, message_id)
            result["duplicate"] = True
            return result
        if message["status"] in {"acknowledged", "expired", "deadlettered"}:
            raise A2AStateError(
                f"Cannot cancel a terminal {message['status']} message; "
                "replay it instead."
            )
        deliveries = connection.execute(
            text(
                """
                SELECT d.id, d.redis_stream_id, s.consumer_group
                FROM a2a_deliveries d
                JOIN a2a_subscriptions s ON s.id=d.subscription_id
                WHERE d.envelope_id=:message_id
                  AND d.status NOT IN ('acknowledged','cancelled')
                FOR UPDATE OF d
                """
            ),
            {"message_id": message_id},
        ).mappings().all()
        transport_acks = []
        for delivery in deliveries:
            if delivery["redis_stream_id"]:
                transport_acks.append(
                    self._ack_transport_cursor(
                        connection,
                        project_id=project_id,
                        message_id=message_id,
                        delivery_id=delivery["id"],
                        channel_key=message["channel_key"],
                        consumer_group=delivery["consumer_group"],
                        stream_id=delivery["redis_stream_id"],
                        data_boundary=message["data_boundary"],
                    )
                )
        reason_payload = json.dumps(
            {"reason": self._required(reason, "reason")}
        )
        params = {"message_id": message_id, "reason": reason_payload}
        connection.execute(
            text(
                "UPDATE a2a_envelopes SET status='cancelled', updated_at=now() "
                "WHERE id=:message_id"
            ),
            params,
        )
        connection.execute(
            text(
                """
                UPDATE a2a_deliveries
                SET status='cancelled', lease_owner=NULL,
                    visibility_expires_at=NULL, updated_at=now()
                WHERE envelope_id=:message_id
                  AND status NOT IN ('acknowledged','cancelled')
                """
            ),
            params,
        )
        connection.execute(
            text(
                """
                UPDATE a2a_outbox
                SET status='cancelled', last_error=CAST(:reason AS JSONB),
                    updated_at=now()
                WHERE envelope_id=:message_id
                  AND status IN ('pending','dispatching','retry_wait')
                """
            ),
            params,
        )
        if message["job_id"]:
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status='cancelled', last_error=CAST(:reason AS JSONB),
                        lease_owner=NULL, lease_token_hash=NULL,
                        lease_expires_at=NULL, updated_at=now()
                    WHERE id=:job_id
                      AND status IN ('queued','leased','retry_wait')
                    """
                ),
                {**params, "job_id": message["job_id"]},
            )
        emit_event(
            connection,
            project_id,
            "a2a.message_cancelled",
            actor,
            "a2a_envelope",
            message_id,
            {"reason": reason, "transport_acks": transport_acks},
        )
        return self.get_message(connection, project_id, message_id)

    def replay(
        self,
        connection: Connection,
        *,
        project_id: str,
        message_id: str,
        dedupe_key: str,
        reason: str,
        actor: str,
        ttl_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        original = self.get_message(connection, project_id, message_id)
        if original["status"] not in {
            "acknowledged",
            "cancelled",
            "expired",
            "deadlettered",
        }:
            raise A2AStateError(
                "Replay requires an acknowledged, cancelled, expired, or "
                "deadlettered source message."
            )
        lineage = dict(original["lineage"] or {})
        lineage.update(
            {
                "replayed_from": message_id,
                "replay_reason": self._required(reason, "reason"),
            }
        )
        replayed = self.publish(
            connection,
            project_id=project_id,
            channel_id=original["channel_id"],
            message_kind=original["message_kind"],
            schema_name=original["schema_name"],
            schema_version=original["schema_version"],
            sender_kind=original["sender_kind"],
            sender_id=original["sender_id"],
            sender_version_id=original["sender_version_id"],
            recipient=original["recipient"],
            payload=original["payload"],
            dedupe_key=dedupe_key,
            data_boundary=original["data_boundary"],
            priority=original["priority"],
            ttl_seconds=ttl_seconds or original["ttl_seconds"],
            ack_mode=original["ack_mode"],
            conversation_id=original["conversation_id"],
            correlation_id=original["correlation_id"],
            causation_id=message_id,
            trace_id=original["trace_id"],
            reply_to_id=original["reply_to_id"],
            lineage=lineage,
            actor=actor,
        )
        emit_event(
            connection,
            project_id,
            "a2a.message_replayed",
            actor,
            "a2a_envelope",
            replayed["id"],
            {
                "source_message_id": message_id,
                "reason": reason,
                "trace_id": original["trace_id"],
            },
        )
        return replayed

    def get_message(
        self, connection: Connection, project_id: str, message_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT e.*, c.channel_key, c.name AS channel_name,
                       o.id AS outbox_id, o.job_id, o.status AS outbox_status,
                       o.attempt AS dispatch_attempt, o.last_error AS dispatch_error,
                       (SELECT count(*) FROM a2a_deliveries d
                        WHERE d.envelope_id=e.id) AS delivery_count,
                       (SELECT count(*) FROM a2a_deliveries d
                        WHERE d.envelope_id=e.id AND d.status='acknowledged')
                         AS acknowledged_count
                FROM a2a_envelopes e
                JOIN a2a_channels c ON c.id=e.channel_id
                LEFT JOIN a2a_outbox o ON o.envelope_id=e.id
                WHERE e.id=:id AND e.project_id=:project_id
                """
            ),
            {"id": message_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise A2ANotFoundError("A2A message not found.")
        return self._serialize(dict(row))

    def list_messages(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 100,
        status: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id=:project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": max(1, min(500, limit)),
        }
        if status is not None:
            clauses.append("status=:status")
            params["status"] = status
        if trace_id is not None:
            clauses.append("trace_id=:trace_id")
            params["trace_id"] = trace_id
        ids = connection.execute(
            text(
                "SELECT id FROM a2a_envelopes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT :limit"
            ),
            params,
        ).scalars().all()
        return [self.get_message(connection, project_id, item) for item in ids]

    def get_delivery(
        self, connection: Connection, project_id: str, delivery_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT d.*, s.subscriber_kind, s.subscriber_id, s.consumer_group,
                       e.message_kind, e.trace_id, e.correlation_id,
                       c.channel_key
                FROM a2a_deliveries d
                JOIN a2a_subscriptions s ON s.id=d.subscription_id
                JOIN a2a_envelopes e ON e.id=d.envelope_id
                JOIN a2a_channels c ON c.id=e.channel_id
                WHERE d.id=:id AND d.project_id=:project_id
                """
            ),
            {"id": delivery_id, "project_id": project_id},
        ).mappings().first()
        if row is None:
            raise A2ANotFoundError("A2A delivery not found.")
        result = self._serialize(dict(row))
        acknowledgements = connection.execute(
            text(
                "SELECT * FROM a2a_acknowledgements "
                "WHERE delivery_id=:id ORDER BY created_at"
            ),
            {"id": delivery_id},
        ).mappings().all()
        result["acknowledgements"] = [
            self._serialize(dict(item)) for item in acknowledgements
        ]
        return result

    def audit_feed(
        self, connection: Connection, project_id: str, limit: int = 100
    ) -> dict[str, Any]:
        messages = self.list_messages(connection, project_id, limit=limit)
        attempts = connection.execute(
            text(
                """
                SELECT * FROM a2a_transport_attempts
                WHERE project_id=:project_id ORDER BY created_at DESC LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": max(1, min(500, limit))},
        ).mappings().all()
        deliveries = connection.execute(
            text(
                """
                SELECT d.id, d.envelope_id, d.subscription_id, d.status,
                       d.attempt, d.consumer_name, d.visibility_expires_at,
                       d.last_error, d.updated_at
                FROM a2a_deliveries d
                WHERE d.project_id=:project_id
                ORDER BY d.updated_at DESC LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": max(1, min(500, limit))},
        ).mappings().all()
        return {
            "transport": self.transport.health(),
            "messages": messages,
            "deliveries": [self._serialize(dict(item)) for item in deliveries],
            "transport_attempts": [
                self._serialize(dict(item)) for item in attempts
            ],
        }

    def health(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        counts = connection.execute(
            text(
                """
                SELECT
                  count(*) FILTER (WHERE status IN ('queued','dispatching')) AS queued,
                  count(*) FILTER (WHERE status='deadlettered') AS deadlettered,
                  count(*) FILTER (WHERE status='expired') AS expired,
                  count(*) FILTER (WHERE status IN (
                    'published','delivering','acknowledged','partially_acknowledged'
                  )) AS delivered
                FROM a2a_envelopes WHERE project_id=:project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        delivery_counts = connection.execute(
            text(
                """
                SELECT
                  count(*) FILTER (WHERE status IN ('pending','published')) AS pending,
                  count(*) FILTER (WHERE status IN (
                    'claimed','processing','retry_wait'
                  )) AS in_flight,
                  count(*) FILTER (WHERE status='acknowledged') AS acknowledged,
                  count(*) FILTER (WHERE status='deadlettered') AS deadlettered
                FROM a2a_deliveries WHERE project_id=:project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        transport = self.transport.health()
        if counts["deadlettered"]:
            overall = "degraded"
        elif transport.get("status") == "healthy":
            overall = "healthy"
        elif transport.get("status") == "not_configured" or not transport.get("configured"):
            overall = "not_configured"
        else:
            overall = "degraded"
        return {
            "status": overall,
            "transport": transport,
            "ledger": {key: int(value or 0) for key, value in counts.items()},
            "deliveries": {
                key: int(value or 0) for key, value in delivery_counts.items()
            },
        }

    def mark_dispatch_failure(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: str,
        error: dict[str, Any],
        terminal: bool,
    ) -> None:
        row = connection.execute(
            text(
                """
                SELECT o.id, o.envelope_id
                FROM a2a_outbox o
                WHERE o.project_id=:project_id AND o.job_id=:job_id
                """
            ),
            {"project_id": project_id, "job_id": job_id},
        ).mappings().first()
        if row is None:
            return
        status = "deadlettered" if terminal else "retry_wait"
        envelope_status = "deadlettered" if terminal else "queued"
        params = {
            "status": status,
            "error": json.dumps(error),
            "id": row["id"],
            "envelope_status": envelope_status,
            "envelope_id": row["envelope_id"],
        }
        connection.execute(
            text(
                "UPDATE a2a_outbox SET status=:status, "
                "last_error=CAST(:error AS JSONB), updated_at=now() WHERE id=:id"
            ),
            params,
        )
        connection.execute(
            text(
                "UPDATE a2a_envelopes SET status=:envelope_status, "
                "updated_at=now() WHERE id=:envelope_id"
            ),
            params,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _ack_transport_cursor(
        self,
        connection: Connection,
        *,
        project_id: str,
        message_id: str,
        delivery_id: Optional[str],
        channel_key: str,
        consumer_group: str,
        stream_id: str,
        data_boundary: str,
    ) -> dict[str, Any]:
        project = connection.execute(
            text(
                """
                SELECT p.workspace_id, w.org_id
                FROM projects p JOIN workspaces w ON w.id=p.workspace_id
                WHERE p.id=:project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        stream = self._stream(project_id, channel_key)
        effect_payload = {
            "message_id": message_id,
            "delivery_id": delivery_id,
            "stream": stream,
            "consumer_group": consumer_group,
            "redis_stream_id": stream_id,
        }
        cursor_key = delivery_id or f"{consumer_group}:{message_id}"
        receipt, created = self.receipts.create_or_reuse(
            connection,
            org_id=project["org_id"],
            workspace_id=project["workspace_id"],
            project_id=project_id,
            dedupe_key=f"a2a:xack:{cursor_key}",
            effect_kind="a2a.redis.acknowledge",
            effect_scope={
                "message_id": message_id,
                "delivery_id": delivery_id,
                "stream": stream,
                "data_boundary": data_boundary,
            },
            payload=effect_payload,
            data_boundary=data_boundary,
        )
        if not created and receipt["status"] == "succeeded":
            return {
                "status": "succeeded",
                "receipt_id": receipt["id"],
                "duplicate": True,
                **(receipt.get("result") or {}),
            }
        attempt_id = make_id("a2aattempt")
        attempt = connection.execute(
            text(
                """
                SELECT count(*) + 1 FROM a2a_transport_attempts
                WHERE delivery_id=:delivery_id AND operation='xack'
                """
            ),
            {"delivery_id": delivery_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO a2a_transport_attempts (
                    id, project_id, envelope_id, delivery_id, operation,
                    status, attempt, redis_stream_id, consumer_group
                ) VALUES (
                    :id, :project_id, :message_id, :delivery_id, 'xack',
                    'started', :attempt, :stream_id, :consumer_group
                )
                """
            ),
            {
                "id": attempt_id,
                "project_id": project_id,
                "message_id": message_id,
                "delivery_id": delivery_id,
                "attempt": int(attempt),
                "stream_id": stream_id,
                "consumer_group": consumer_group,
            },
        )
        started = time.monotonic()
        try:
            acknowledged = self.transport.acknowledge(
                stream, consumer_group, stream_id
            )
        except Exception as exc:  # noqa: BLE001 - preserve canonical ACK evidence
            error = {"type": exc.__class__.__name__, "message": str(exc)}
            self.receipts.mark_failed(connection, receipt["id"], error)
            connection.execute(
                text(
                    """
                    UPDATE a2a_transport_attempts
                    SET status='failed', latency_ms=:latency,
                        error=CAST(:error AS JSONB)
                    WHERE id=:id
                    """
                ),
                {
                    "id": attempt_id,
                    "latency": int((time.monotonic() - started) * 1000),
                    "error": json.dumps(error),
                },
            )
            return {
                "status": "failed",
                "receipt_id": receipt["id"],
                "error": error,
            }
        result = {
            "acknowledged": int(acknowledged),
            "stream": stream,
            "consumer_group": consumer_group,
            "redis_stream_id": stream_id,
        }
        self.receipts.mark_succeeded(
            connection, receipt["id"], stream_id, result
        )
        connection.execute(
            text(
                """
                UPDATE a2a_transport_attempts
                SET status='succeeded', latency_ms=:latency
                WHERE id=:id
                """
            ),
            {
                "id": attempt_id,
                "latency": int((time.monotonic() - started) * 1000),
            },
        )
        return {"status": "succeeded", "receipt_id": receipt["id"], **result}

    def _transport_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: self._serialize(row.get(key))
            for key in (
                "id",
                "project_id",
                "channel_id",
                "message_kind",
                "schema_name",
                "schema_version",
                "sender_kind",
                "sender_id",
                "sender_version_id",
                "recipient",
                "data_boundary",
                "priority",
                "ttl_seconds",
                "expires_at",
                "ack_mode",
                "conversation_id",
                "correlation_id",
                "causation_id",
                "trace_id",
                "reply_to_id",
                "lineage",
                "payload",
                "payload_hash",
                "dedupe_key",
                "created_at",
            )
        }

    def _stream(self, project_id: str, channel_key: str) -> str:
        return (
            f"{self.settings.a2a_redis_prefix}:"
            f"{self._key(project_id, 'project_id')}:"
            f"{self._key(channel_key, 'channel_key')}"
        )

    def _recipient_matches(
        self, recipient: dict[str, Any], recipient_filter: dict[str, Any]
    ) -> bool:
        if not recipient_filter:
            return True
        return all(recipient.get(key) == value for key, value in recipient_filter.items())

    def _delivery_count(self, connection: Connection, message_id: str) -> int:
        return int(
            connection.execute(
                text("SELECT count(*) FROM a2a_deliveries WHERE envelope_id=:id"),
                {"id": message_id},
            ).scalar_one()
        )

    def _refresh_envelope_status(
        self, connection: Connection, message_id: str
    ) -> None:
        counts = connection.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status='acknowledged') AS acknowledged
                FROM a2a_deliveries WHERE envelope_id=:id
                """
            ),
            {"id": message_id},
        ).mappings().one()
        total = int(counts["total"])
        acknowledged = int(counts["acknowledged"])
        status = (
            "acknowledged"
            if total and acknowledged == total
            else "partially_acknowledged"
            if acknowledged
            else "delivering"
        )
        connection.execute(
            text(
                "UPDATE a2a_envelopes SET status=:status, updated_at=now() "
                "WHERE id=:id"
            ),
            {"status": status, "id": message_id},
        )

    def _required(self, value: Any, field: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError(f"{field} must not be blank.")
        return cleaned

    def _key(self, value: Any, field: str) -> str:
        cleaned = self._required(value, field).lower().replace("-", "_")
        cleaned = re.sub(r"[^a-z0-9_.:]+", "_", cleaned).strip("_")
        if not cleaned:
            raise ValueError(f"{field} must contain a usable identifier.")
        return cleaned

    def _serialize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._serialize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._serialize(item) for item in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value
