from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import (
    create_blocking_decision,
    emit_event,
    make_id,
    normalize_data_boundary,
    now_utc,
)
from .receipts import ReceiptService
from .email_adapter import EmailAdapter
from .slack_adapter import SlackAdapter
from .mattermost_adapter import MattermostAdapter
from .github_adapter import IntegrationConfigurationError


class OutboxMessageNotFoundError(RuntimeError):
    pass


class InvalidCreatedByJobError(RuntimeError):
    pass


class OutboxDryRunNotAllowedError(ValueError):
    pass


class CommsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self.slack = SlackAdapter(settings)
        self.mattermost = MattermostAdapter(settings)
        self.email = EmailAdapter(settings)

    def list_channel_policies(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider, delivery_mode,
                       allowed_data_boundaries, rollout_notes, status,
                       created_by, metadata, created_at, updated_at
                FROM comms_channel_policies
                WHERE project_id = :project_id
                ORDER BY provider ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [dict(row) for row in rows]

    def get_channel_policy(
        self,
        connection: Connection,
        project_id: str,
        provider: str,
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, delivery_mode,
                       allowed_data_boundaries, rollout_notes, status,
                       created_by, metadata, created_at, updated_at
                FROM comms_channel_policies
                WHERE project_id = :project_id
                  AND provider = :provider
                  AND status = 'active'
                """
            ),
            {"project_id": project_id, "provider": provider},
        ).mappings().first()
        return dict(row) if row else None

    def upsert_channel_policy(
        self,
        connection: Connection,
        project_id: str,
        provider: str,
        delivery_mode: str,
        allowed_data_boundaries: list[str],
        actor: str,
        rollout_notes: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        normalized_provider = provider.strip().lower()
        normalized_mode = delivery_mode.strip().lower()
        if normalized_provider not in {"slack", "mattermost", "email"}:
            raise ValueError(f"Unsupported communications provider: {provider}")
        if normalized_mode not in {"disabled", "draft_only", "real_send_allowed"}:
            raise ValueError(f"Unsupported communications delivery mode: {delivery_mode}")
        boundaries = [
            normalize_data_boundary(boundary)
            for boundary in allowed_data_boundaries
            if str(boundary).strip()
        ] or ["public", "internal"]
        policy_id = f"comms_policy_{project_id}_{normalized_provider}"
        connection.execute(
            text(
                """
                INSERT INTO comms_channel_policies (
                    id, project_id, provider, delivery_mode,
                    allowed_data_boundaries, rollout_notes, status,
                    created_by, metadata
                )
                VALUES (
                    :id, :project_id, :provider, :delivery_mode,
                    CAST(:allowed_data_boundaries AS JSONB), :rollout_notes,
                    'active', :created_by, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, provider) DO UPDATE
                SET delivery_mode = EXCLUDED.delivery_mode,
                    allowed_data_boundaries = EXCLUDED.allowed_data_boundaries,
                    rollout_notes = EXCLUDED.rollout_notes,
                    status = 'active',
                    created_by = EXCLUDED.created_by,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": policy_id,
                "project_id": project_id,
                "provider": normalized_provider,
                "delivery_mode": normalized_mode,
                "allowed_data_boundaries": json.dumps(boundaries),
                "rollout_notes": rollout_notes,
                "created_by": actor,
                "metadata": json.dumps(metadata or {}),
            },
        )
        emit_event(
            connection,
            project_id,
            "comms.channel_policy_upserted",
            actor,
            "comms_channel_policy",
            policy_id,
            {
                "provider": normalized_provider,
                "delivery_mode": normalized_mode,
                "allowed_data_boundaries": boundaries,
            },
        )
        return self.get_channel_policy(connection, project_id, normalized_provider)

    def create_outbox_message(
        self,
        connection: Connection,
        project_id: str,
        channel: str,
        subject: Optional[str],
        body: str,
        dedupe_key: str,
        actor: str,
        data_boundary: str = "internal",
    ) -> dict:
        message_id = make_id("out")
        boundary = normalize_data_boundary(data_boundary)
        connection.execute(
            text(
                """
                INSERT INTO outbox_messages (
                    id, project_id, channel, subject, body, status, data_boundary,
                    dedupe_key
                )
                VALUES (
                    :id, :project_id, :channel, :subject, :body, 'draft',
                    :data_boundary, :dedupe_key
                )
                """
            ),
            {
                "id": message_id,
                "project_id": project_id,
                "channel": channel,
                "subject": subject,
                "body": body,
                "data_boundary": boundary,
                "dedupe_key": dedupe_key,
            },
        )
        emit_event(
            connection,
            project_id,
            "outbox.created",
            actor,
            "outbox_message",
            message_id,
            {"channel": channel, "data_boundary": boundary},
        )
        return self.get_outbox_message(connection, message_id)

    def list_outbox_messages(
        self, connection: Connection, project_id: str, limit: int, offset: int = 0
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, channel, subject, body, status, dedupe_key,
                       data_boundary, receipt_id, created_at, updated_at
                FROM outbox_messages
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [dict(row) for row in rows]

    def get_outbox_message(self, connection: Connection, message_id: str) -> dict:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, channel, subject, body, status, dedupe_key,
                       data_boundary, receipt_id, created_at, updated_at
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().first()
        if not row:
            raise OutboxMessageNotFoundError("Outbox message not found.")
        return dict(row)

    def queue_outbox_delivery(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        reservation_id: str,
        dedupe_key: str,
        actor: str,
        priority: int,
        max_attempts: int,
        urgency: str = "normal",
    ) -> dict:
        message = self.get_outbox_message(connection, message_id)
        self._assert_message_project(message, project_id)
        boundary = normalize_data_boundary(message.get("data_boundary"))
        normalized_urgency = self._normalize_urgency(urgency)
        job_id = make_id("job")
        run_after, queue_policy = self._delivery_queue_policy(
            connection,
            project_id,
            channel=message["channel"],
            urgency=normalized_urgency,
        )
        from boss_engine.identity import lineage as _identity_lineage

        # Who this delivery acts for (R8): the same stamping path as enqueue_job.
        job_payload, identity_columns = _identity_lineage.stamp_job_row(
            {
                "project_id": project_id,
                "outbox_message_id": message_id,
                "reservation_id": reservation_id,
                "dedupe_key": dedupe_key,
                "channel": message["channel"],
                "subject": message.get("subject"),
                "body": message["body"],
                "data_boundary": boundary,
                "urgency": normalized_urgency,
                "requires_reservation": True,
            })
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, kind, priority, status, payload,
                    data_boundary, max_attempts, attempt, run_after,
                    on_behalf_of, grant_id, chain, on_behalf_system
                )
                VALUES (
                    :id, :project_id, NULL, 'comms.outbox.deliver', :priority, 'queued',
                    CAST(:payload AS JSONB), :data_boundary, :max_attempts, 0,
                    :run_after, :on_behalf_of, :grant_id, CAST(:chain AS JSONB), :on_behalf_system
                )
                """
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "priority": priority,
                "payload": json.dumps(job_payload),
                "data_boundary": boundary,
                "max_attempts": max_attempts,
                "run_after": run_after,
                **identity_columns,
            },
        )
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            job_id,
            {
                "kind": "comms.outbox.deliver",
                "task_id": None,
                "data_boundary": boundary,
                "urgency": normalized_urgency,
                **queue_policy,
            },
        )
        job = {
            "id": job_id,
            "project_id": project_id,
            "task_id": None,
            "kind": "comms.outbox.deliver",
            "status": "queued",
            "data_boundary": boundary,
            "priority": priority,
            "max_attempts": max_attempts,
            "attempt": 0,
            "run_after": run_after.isoformat(),
        }
        self.budget.attach_job(connection, project_id, reservation_id, job["id"])
        connection.execute(
            text(
                """
                UPDATE outbox_messages
                SET status = 'queued',
                    updated_at = now()
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        )
        event_type = "outbox.deferred" if queue_policy else "outbox.queued"
        event_data = {
            "outbox_message_id": message_id,
            "reservation_id": reservation_id,
            "urgency": normalized_urgency,
        }
        if queue_policy:
            event_data.update(queue_policy)
            event_data["run_after"] = run_after.isoformat()
        emit_event(
            connection,
            project_id,
            event_type,
            actor,
            "job",
            job["id"],
            event_data,
        )
        return job

    def dry_run_deliver_outbox(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        actor: str,
        dedupe_key: Optional[str] = None,
    ) -> dict:
        """Record a dry-run Receipt for a draft outbox message.

        Never calls Slack/email/Mattermost adapters, never requires
        ``real_send_allowed``, never reserves budget, and never enqueues
        ``comms.outbox.deliver``.
        """
        message = self.get_outbox_message(connection, message_id)
        self._assert_message_project(message, project_id)
        status = str(message.get("status") or "")
        if status not in {"draft", "dry_run"}:
            raise OutboxDryRunNotAllowedError(
                "Dry-run requires an existing draft outbox message; "
                f"status is {status}."
            )
        boundary = normalize_data_boundary(message.get("data_boundary"))
        key = (dedupe_key or "").strip() or (
            f"outbox-dry-run:{project_id}:{message_id}:{make_id('dry')}"
        )
        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            key,
            "comms",
            {
                "channel": message["channel"],
                "outbox_message_id": message_id,
                "data_boundary": boundary,
                "delivery_mode": "dry_run",
            },
            {
                "channel": message["channel"],
                "data_boundary": boundary,
                "delivery_mode": "dry_run",
            },
            data_boundary=boundary,
        )
        if created or receipt.get("status") != "succeeded":
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                None,
                {
                    "delivery_mode": "dry_run",
                    "provider_send": False,
                    "message": "dry-run — no provider send",
                    "outbox_message_id": message_id,
                    "channel": message["channel"],
                },
            )
        self._set_outbox_status(connection, message_id, "dry_run", receipt["id"])
        emit_event(
            connection,
            project_id,
            "outbox.dry_run",
            actor,
            "outbox_message",
            message_id,
            {
                "receipt_id": receipt["id"],
                "delivery_mode": "dry_run",
                "channel": message["channel"],
                "data_boundary": boundary,
                "provider_send": False,
                "reused": not created,
            },
        )
        return {
            "status": "dry_run",
            "message_id": message_id,
            "receipt_id": receipt["id"],
            "delivery_mode": "dry_run",
            "provider_send": False,
            "message": "dry-run — no provider send",
        }

    def deliver_outbox_message(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        reservation_id: str,
        dedupe_key: str,
        created_by_job_id: Optional[str] = None,
    ) -> dict:
        message = self.get_outbox_message(connection, message_id)
        self._assert_message_project(message, project_id)
        boundary = normalize_data_boundary(message.get("data_boundary"))
        provider = self._provider_for_channel(message["channel"])
        if created_by_job_id and not self._job_exists(connection, created_by_job_id):
            raise InvalidCreatedByJobError(
                "created_by_job_id does not reference a known job."
            )
        reservation = self.budget.assert_active_reservation(
            connection, project_id, reservation_id
        )
        if not reservation:
            self._set_outbox_status(connection, message_id, "blocked")
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Missing active reservation for outbound communication",
                {"action": "create_budget_reservation"},
                {"outbox_message_id": message_id},
                "missing_reservation",
            )
            emit_event(
                connection,
                project_id,
                "outbox.blocked",
                "comms_worker",
                "outbox_message",
                message_id,
                {"decision_id": decision_id, "reason": "missing_reservation"},
            )
            return {"status": "blocked", "decision_id": decision_id}

        if provider is None:
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because the communication channel is unsupported.",
            )
            self._set_outbox_status(connection, message_id, "blocked")
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Unsupported communication channel",
                {"action": "configure_supported_channel"},
                {"channel": message["channel"], "outbox_message_id": message_id},
                "unsupported_comms_channel",
            )
            emit_event(
                connection,
                project_id,
                "outbox.blocked",
                "comms_worker",
                "outbox_message",
                message_id,
                {
                    "decision_id": decision_id,
                    "reason": "unsupported_channel",
                    "channel": message["channel"],
                },
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "message": f"Unsupported channel: {message['channel']}",
            }

        if boundary in {"restricted", "local-only"}:
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because data-boundary policy blocked the "
                f"{provider['label']} effect.",
            )
            self._set_outbox_status(connection, message_id, "blocked")
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Data boundary blocks {provider['label']} external message",
                {
                    "action": "reclassify_or_configure_approved_boundary_adapter",
                    "channel": message["channel"],
                    "data_boundary": boundary,
                },
                {"outbox_message_id": message_id},
                "data_boundary_blocks_external_effect",
            )
            emit_event(
                connection,
                project_id,
                "policy.denied",
                "comms_worker",
                "outbox_message",
                message_id,
                {
                    "reason": f"data_boundary_not_allowed_for_{provider['key']}",
                    "data_boundary": boundary,
                    "decision_id": decision_id,
                },
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "data_boundary": boundary,
                "message": (
                    f"{boundary} data cannot be sent to {provider['label']} without an "
                    "approved boundary adapter."
                ),
            }

        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            provider["effect_kind"],
            {
                "channel": message["channel"],
                "outbox_message_id": message_id,
                "data_boundary": boundary,
            },
            {
                "channel": message["channel"],
                "subject": message.get("subject"),
                "body": message["body"],
                "data_boundary": boundary,
            },
            created_by_job_id=created_by_job_id,
            data_boundary=boundary,
        )
        if not created and receipt["status"] == "succeeded":
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because the communication effect already exists.",
            )
            self._set_outbox_status(connection, message_id, "acked", receipt["id"])
            emit_event(
                connection,
                project_id,
                "outbox.acknowledged",
                "comms_worker",
                "outbox_message",
                message_id,
                {"receipt_id": receipt["id"], "reused": True},
            )
            return {
                "status": "reused",
                "receipt_id": receipt["id"],
                "provider_ref": receipt.get("provider_ref"),
                "message_id": message_id,
            }

        policy_block = self._delivery_policy_block(
            connection,
            project_id,
            provider,
            boundary,
        )
        if policy_block:
            self.receipts.mark_failed(
                connection,
                receipt["id"],
                {
                    "message": policy_block["message"],
                    "reason": policy_block["reason"],
                },
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because communications delivery policy blocked "
                f"{provider['label']} real send.",
            )
            self._set_outbox_status(connection, message_id, "blocked", receipt["id"])
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"{provider['label']} real delivery is not enabled by project policy",
                {
                    "action": "upsert_comms_channel_policy",
                    "provider": provider["key"],
                    "required_delivery_mode": "real_send_allowed",
                },
                {
                    "outbox_message_id": message_id,
                    "channel": message["channel"],
                    "receipt_id": receipt["id"],
                    "reason": policy_block["reason"],
                },
                f"{provider['key']}_delivery_policy_blocked",
            )
            emit_event(
                connection,
                project_id,
                "outbox.blocked",
                "comms_worker",
                "outbox_message",
                message_id,
                {
                    "decision_id": decision_id,
                    "receipt_id": receipt["id"],
                    "reason": policy_block["reason"],
                },
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "receipt_id": receipt["id"],
                "message": policy_block["message"],
            }

        self._set_outbox_status(connection, message_id, "sending", receipt["id"])
        try:
            provider_ref = self._send_message(
                provider, message["channel"], message.get("subject"), message["body"]
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because "
                f"{provider['label']} delivery is disabled or unconfigured.",
            )
            self._set_outbox_status(connection, message_id, "blocked", receipt["id"])
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"{provider['label']} delivery is blocked by policy or missing configuration",
                {"action": f"configure_or_enable_{provider['key']}_delivery"},
                {"outbox_message_id": message_id, "channel": message["channel"]},
                f"{provider['key']}_delivery_blocked",
            )
            emit_event(
                connection,
                project_id,
                "outbox.blocked",
                "comms_worker",
                "outbox_message",
                message_id,
                {"decision_id": decision_id, "message": str(error)},
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "receipt_id": receipt["id"],
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because "
                f"{provider['label']} delivery failed before acknowledgement.",
            )
            self._set_outbox_status(connection, message_id, "retrying", receipt["id"])
            emit_event(
                connection,
                project_id,
                "outbox.delivery_failed",
                "comms_worker",
                "outbox_message",
                message_id,
                {"receipt_id": receipt["id"], "message": str(error)},
            )
            return {
                "status": "failed",
                "receipt_id": receipt["id"],
                "message": str(error),
            }

        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            provider_ref,
            {"message_id": message_id, "channel": message["channel"]},
        )
        self.budget.settle(
            connection,
            project_id,
            reservation_id,
            receipt["id"],
            "comms",
            provider["skill_id"],
            int(reservation["amount_usd_micros"]),
            None,
        )
        self._set_outbox_status(connection, message_id, "acked", receipt["id"])
        emit_event(
            connection,
            project_id,
            "outbox.acknowledged",
            "comms_worker",
            "outbox_message",
            message_id,
            {"receipt_id": receipt["id"], "provider_ref": provider_ref},
        )
        return {
            "status": "created",
            "receipt_id": receipt["id"],
            "provider_ref": provider_ref,
            "message_id": message_id,
        }

    def _provider_for_channel(self, channel: str) -> Optional[dict[str, str]]:
        if channel.startswith("slack:"):
            return {
                "key": "slack",
                "label": "Slack",
                "effect_kind": "slack.message.post",
                "skill_id": "slack.post_real",
            }
        if channel.startswith("email:"):
            return {
                "key": "email",
                "label": "Email",
                "effect_kind": "email.message.send",
                "skill_id": "email.send_real",
            }
        if channel.startswith("mattermost:"):
            return {
                "key": "mattermost",
                "label": "Mattermost",
                "effect_kind": "mattermost.message.post",
                "skill_id": "mattermost.post_real",
            }
        return None

    def _delivery_policy_block(
        self,
        connection: Connection,
        project_id: str,
        provider: dict[str, str],
        boundary: str,
    ) -> Optional[dict[str, str]]:
        policy = self.get_channel_policy(connection, project_id, provider["key"])
        if not policy:
            return {
                "reason": "comms_policy_missing",
                "message": (
                    f"{provider['label']} real delivery requires an active "
                    "communications channel policy."
                ),
            }
        if policy["delivery_mode"] != "real_send_allowed":
            return {
                "reason": "comms_policy_not_real_send",
                "message": (
                    f"{provider['label']} delivery mode is {policy['delivery_mode']}; "
                    "real_send_allowed is required."
                ),
            }
        allowed = set(policy.get("allowed_data_boundaries") or [])
        if boundary not in allowed:
            return {
                "reason": "comms_policy_data_boundary_blocked",
                "message": (
                    f"{provider['label']} policy does not allow {boundary} data."
                ),
            }
        return None

    def _send_message(
        self, provider: dict[str, str], channel: str, subject: Optional[str], body: str
    ) -> str:
        if provider["key"] == "slack":
            return self.slack.post_message(channel, subject, body)
        if provider["key"] == "email":
            return self.email.send_message(channel, subject, body)
        if provider["key"] == "mattermost":
            return self.mattermost.post_message(channel, subject, body)
        raise RuntimeError(f"Unsupported provider key: {provider['key']}")

    def list_inbox_messages(
        self, connection: Connection, project_id: str, limit: int, offset: int = 0
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, channel, raw_ref, subject, body, status, parsed,
                       data_boundary, created_at, updated_at
                FROM inbox_messages
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [dict(row) for row in rows]

    def ingest_inbox_message(
        self,
        connection: Connection,
        project_id: str,
        channel: str,
        body: str,
        actor: str,
        subject: Optional[str] = None,
        raw_ref: Optional[str] = None,
        data_boundary: str = "internal",
    ) -> dict:
        message_id = make_id("in")
        boundary = normalize_data_boundary(data_boundary)
        parsed = self._parse_inbound_message(channel, subject, body, raw_ref)
        parsed = self.decide_reply(connection, project_id, parsed, subject, body)
        parsed["channel"] = channel
        parsed["ingested_at"] = now_utc().isoformat()
        connection.execute(
            text(
                """
                INSERT INTO inbox_messages (
                    id, project_id, channel, raw_ref, subject, body, status,
                    data_boundary, parsed
                )
                VALUES (
                    :id, :project_id, :channel, :raw_ref, :subject, :body, 'received',
                    :data_boundary, CAST(:parsed AS JSONB)
                )
                """
            ),
            {
                "id": message_id,
                "project_id": project_id,
                "channel": channel,
                "raw_ref": raw_ref,
                "subject": subject,
                "body": body,
                "data_boundary": boundary,
                "parsed": json.dumps(parsed),
            },
        )
        emit_event(
            connection,
            project_id,
            "inbox.received",
            actor,
            "inbox_message",
            message_id,
            {"channel": channel, "raw_ref": raw_ref, "data_boundary": boundary},
        )
        # No-silent-drops ledger: record receipt immediately. Plain informational messages have no
        # command to route, so they're acknowledged (terminal) here; command messages advance later.
        self._record_ack(
            connection, project_id, message_id, channel, "received", terminal=False, actor=actor
        )
        if parsed.get("kind") == "message":
            self._record_ack(
                connection, project_id, message_id, channel, "acknowledged",
                terminal=True, actor=actor, route_kind="informational")
        row = connection.execute(
            text(
                """
                SELECT id, project_id, channel, raw_ref, subject, body, status, parsed,
                       data_boundary, created_at, updated_at
                FROM inbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()
        return dict(row)

    def _record_ack(
        self,
        connection: Connection,
        project_id: Optional[str],
        message_id: str,
        channel: str,
        state: str,
        *,
        terminal: bool,
        actor: str,
        route_kind: Optional[str] = None,
        target_ref: Optional[str] = None,
        reason: Optional[str] = None,
        remediation: Optional[str] = None,
    ) -> None:
        """Upsert the inbound acknowledgment ledger and append the transition to its history. This is
        the durable 'no silent drops' record — every inbound advances here until a terminal state."""
        entry = {"state": state, "at": now_utc().isoformat(), "actor": actor}
        if reason:
            entry["reason"] = reason
        connection.execute(
            text(
                """
                INSERT INTO ack_state (
                    id, project_id, inbox_message_id, channel, state, terminal,
                    route_kind, target_ref, reason, remediation, history
                )
                VALUES (
                    :id, :project_id, :message_id, :channel, :state, :terminal,
                    :route_kind, :target_ref, :reason, :remediation, CAST(:history AS JSONB)
                )
                ON CONFLICT (inbox_message_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    terminal = EXCLUDED.terminal,
                    route_kind = COALESCE(EXCLUDED.route_kind, ack_state.route_kind),
                    target_ref = COALESCE(EXCLUDED.target_ref, ack_state.target_ref),
                    reason = EXCLUDED.reason,
                    remediation = COALESCE(EXCLUDED.remediation, ack_state.remediation),
                    history = ack_state.history || CAST(:entry AS JSONB),
                    updated_at = now()
                """
            ),
            {
                "id": make_id("ack"),
                "project_id": project_id,
                "message_id": message_id,
                "channel": channel,
                "state": state,
                "terminal": terminal,
                "route_kind": route_kind,
                "target_ref": target_ref,
                "reason": reason,
                "remediation": remediation,
                "history": json.dumps([entry]),
                "entry": json.dumps(entry),
            },
        )
        emit_event(
            connection,
            project_id,
            f"inbox.ack.{state}",
            actor,
            "inbox_message",
            message_id,
            {"state": state, "terminal": terminal, "route_kind": route_kind,
             "target_ref": target_ref, "reason": reason},
        )

    def advance_inbox(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        *,
        status: Optional[str],
        ack: str,
        terminal: bool,
        actor: str,
        route_kind: Optional[str] = None,
        target_ref: Optional[str] = None,
        reason: Optional[str] = None,
        remediation: Optional[str] = None,
    ) -> None:
        """Advance an inbox message: optionally update its lifecycle status and always record the ack."""
        if status is not None:
            connection.execute(
                text(
                    "UPDATE inbox_messages SET status = :status, updated_at = now() "
                    "WHERE id = :message_id AND project_id = :project_id"
                ),
                {"status": status, "message_id": message_id, "project_id": project_id},
            )
        channel = self._inbox_channel(connection, message_id)
        self._record_ack(
            connection, project_id, message_id, channel, ack, terminal=terminal, actor=actor,
            route_kind=route_kind, target_ref=target_ref, reason=reason, remediation=remediation)

    def _inbox_channel(self, connection: Connection, message_id: str) -> str:
        row = connection.execute(
            text("SELECT channel FROM inbox_messages WHERE id = :id"),
            {"id": message_id},
        ).mappings().first()
        return row["channel"] if row else "unknown"

    def get_ack_state(
        self, connection: Connection, message_id: str
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, inbox_message_id, channel, state, terminal,
                       route_kind, target_ref, reason, remediation, history,
                       created_at, updated_at
                FROM ack_state
                WHERE inbox_message_id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().first()
        return dict(row) if row else None

    def list_ack_state(
        self, connection: Connection, project_id: str, limit: int = 50, *,
        pending_only: bool = False
    ) -> list[dict]:
        clause = "AND terminal = false" if pending_only else ""
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, inbox_message_id, channel, state, terminal,
                       route_kind, target_ref, reason, remediation, history,
                       created_at, updated_at
                FROM ack_state
                WHERE project_id = :project_id {clause}
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def mark_inbox_actioned(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        *,
        actor: str,
        decision_id: str,
        outcome_status: str,
    ) -> None:
        payload = {
            "action_result": {
                "decision_id": decision_id,
                "status": outcome_status,
                "actor": actor,
                "actioned_at": now_utc().isoformat(),
            }
        }
        connection.execute(
            text(
                """
                UPDATE inbox_messages
                SET status = 'actioned',
                    parsed = COALESCE(parsed, '{}'::jsonb) || CAST(:parsed AS JSONB),
                    updated_at = now()
                WHERE id = :message_id AND project_id = :project_id
                """
            ),
            {
                "message_id": message_id,
                "project_id": project_id,
                "parsed": json.dumps(payload),
            },
        )
        emit_event(
            connection,
            project_id,
            "inbox.actioned",
            actor,
            "inbox_message",
            message_id,
            {"decision_id": decision_id, "status": outcome_status},
        )
        self._record_ack(
            connection, project_id, message_id,
            self._inbox_channel(connection, message_id), "actioned",
            terminal=True, actor=actor, route_kind="decision", target_ref=decision_id)

    def deadletter_inbox_message(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        *,
        actor: str,
        reason: str,
        payload: dict,
        remediation: Optional[str] = None,
    ) -> str:
        deadletter_id = make_id("dlq")
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, 'inbox_message', :message_id, :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": deadletter_id,
                "project_id": project_id,
                "message_id": message_id,
                "reason": reason,
                "payload": json.dumps(payload),
            },
        )
        connection.execute(
            text(
                """
                UPDATE inbox_messages
                SET status = 'deadlettered',
                    parsed = COALESCE(parsed, '{}'::jsonb) || CAST(:parsed AS JSONB),
                    updated_at = now()
                WHERE id = :message_id AND project_id = :project_id
                """
            ),
            {
                "message_id": message_id,
                "project_id": project_id,
                "parsed": json.dumps(
                    {
                        "deadletter": {
                            "id": deadletter_id,
                            "reason": reason,
                            "created_at": now_utc().isoformat(),
                        }
                    }
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "inbox.deadlettered",
            actor,
            "inbox_message",
            message_id,
            {"deadletter_id": deadletter_id, "reason": reason},
        )
        self._record_ack(
            connection, project_id, message_id,
            self._inbox_channel(connection, message_id), "deadlettered",
            terminal=True, actor=actor, reason=reason, remediation=remediation)
        return deadletter_id

    def mark_outbox_retrying(
        self,
        connection: Connection,
        project_id: str,
        message_id: str,
        retry_delay_seconds: int,
    ) -> None:
        self._set_outbox_status(connection, message_id, "retrying")
        emit_event(
            connection,
            project_id,
            "outbox.retry_scheduled",
            "comms_worker",
            "outbox_message",
            message_id,
            {"retry_delay_seconds": retry_delay_seconds},
        )

    def mark_outbox_deadlettered(
        self, connection: Connection, project_id: str, message_id: str, deadletter_id: str
    ) -> None:
        self._set_outbox_status(connection, message_id, "deadlettered")
        emit_event(
            connection,
            project_id,
            "outbox.deadlettered",
            "comms_worker",
            "outbox_message",
            message_id,
            {"deadletter_id": deadletter_id},
        )

    def _scope(self, connection: Connection, project_id: str) -> dict:
        row = connection.execute(
            text(
                """
                SELECT o.id AS org_id, w.id AS workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        return dict(row)

    def _assert_message_project(self, message: dict, project_id: str) -> None:
        if message["project_id"] != project_id:
            raise OutboxMessageNotFoundError("Outbox message not found for project.")

    def _job_exists(self, connection: Connection, job_id: str) -> bool:
        return (
            connection.execute(
                text("SELECT 1 FROM jobs WHERE id = :job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none()
            is not None
        )

    def _set_outbox_status(
        self,
        connection: Connection,
        message_id: str,
        status: str,
        receipt_id: Optional[str] = None,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE outbox_messages
                SET status = :status,
                    receipt_id = COALESCE(:receipt_id, receipt_id),
                    updated_at = now()
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id, "status": status, "receipt_id": receipt_id},
        )

    def _normalize_urgency(self, urgency: str) -> str:
        normalized = urgency.strip().lower() if isinstance(urgency, str) else "normal"
        if normalized not in {"low", "normal", "urgent"}:
            raise ValueError("urgency must be low, normal, or urgent.")
        return normalized

    def _delivery_queue_policy(
        self,
        connection: Connection,
        project_id: str,
        *,
        channel: str,
        urgency: str,
    ) -> tuple[datetime, dict]:
        requested_at = now_utc()
        if urgency == "urgent":
            return requested_at, {}

        quiet_until = self._quiet_hours_until(requested_at)
        if quiet_until:
            return quiet_until, {
                "reason": "quiet_hours",
                "quiet_hours_end_at": quiet_until.isoformat(),
            }

        throttle_limit = self.settings.comms_throttle_max_queued_per_hour
        if throttle_limit > 0:
            recent_count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE project_id = :project_id
                      AND kind = 'comms.outbox.deliver'
                      AND created_at >= :window_start
                      AND payload->>'channel' = :channel
                      AND status IN ('queued', 'leased', 'running', 'succeeded')
                    """
                ),
                {
                    "project_id": project_id,
                    "channel": channel,
                    "window_start": requested_at - timedelta(hours=1),
                },
            ).scalar_one()
            if int(recent_count) >= throttle_limit:
                defer_minutes = max(self.settings.comms_throttle_defer_minutes, 1)
                run_after = requested_at + timedelta(minutes=defer_minutes)
                return run_after, {
                    "reason": "throttled",
                    "throttle_limit_per_hour": throttle_limit,
                    "recent_delivery_jobs": int(recent_count),
                }

        return requested_at, {}

    def _quiet_hours_until(self, value: datetime) -> Optional[datetime]:
        if not self.settings.comms_quiet_hours_enabled:
            return None
        start = self._parse_utc_hhmm(self.settings.comms_quiet_hours_start_utc)
        end = self._parse_utc_hhmm(self.settings.comms_quiet_hours_end_utc)
        current = value.timetz().replace(tzinfo=None)
        current_date = value.date()

        if start < end:
            if start <= current < end:
                return datetime.combine(current_date, end, tzinfo=value.tzinfo)
            return None

        if current >= start:
            return datetime.combine(
                current_date + timedelta(days=1),
                end,
                tzinfo=value.tzinfo,
            )
        if current < end:
            return datetime.combine(current_date, end, tzinfo=value.tzinfo)
        return None

    def _parse_utc_hhmm(self, value: str) -> time:
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value.strip())
        if not match:
            raise ValueError("quiet-hour values must use HH:MM 24-hour UTC format.")
        return time(int(match.group(1)), int(match.group(2)))

    def _parse_inbound_message(
        self,
        channel: str,
        subject: Optional[str],
        body: str,
        raw_ref: Optional[str],
    ) -> dict:
        text_blob = "\n".join(
            item for item in [subject or "", body or "", raw_ref or ""] if item
        )
        is_email = channel.lower().startswith("email")
        context = self._parse_command_tokens(subject, body)
        if is_email and not context.get("thread_id"):
            context["thread_id"] = self._derive_thread_id(subject, raw_ref)
        if not re.search(r"\bdecision\b|\bdecision_id\b|\bdec_", text_blob, re.I):
            result: dict[str, object] = {"kind": "message"}
            if context:
                result["command_context"] = context
            return result

        decision_match = re.search(r"\b(dec_[A-Za-z0-9_]+)\b", text_blob)
        action_match = re.search(
            r"\b(approve|approved|reject|rejected|revise|revision|execute|executed|delegate)\b",
            text_blob,
            re.I,
        )
        parsed: dict[str, object] = {
            "kind": "decision_reply",
            "source_channel": channel,
            "note": body.strip()[:1000],
        }
        if context:
            parsed["command_context"] = context
        if decision_match:
            parsed["decision_id"] = decision_match.group(1)
        if action_match:
            # A HINT, NOT THE ANSWER. The first of these words used to BE the action, so
            # "I don't approve dec_x, reject it" approved it. The reply's meaning is decided by
            # a model in `decide_reply`; this word is evidence it is shown.
            raw_action = action_match.group(1).lower()
            parsed["action_hint"] = {
                "approved": "approve",
                "rejected": "reject",
                "revision": "revise",
                "executed": "execute",
            }.get(raw_action, raw_action)
        delegate_match = re.search(
            r"\bdelegate(?:\s+to|_to:?)\s+([A-Za-z0-9_.-]+)",
            text_blob,
            re.I,
        )
        if delegate_match:
            parsed["delegate_to"] = delegate_match.group(1)
        if "decision_id" not in parsed:
            parsed["parse_error"] = "decision_reply_missing_id_or_action"
            parsed["remediation"] = self._command_remediation(is_email)
        return parsed

    #: What a reply to a decision can do, as the decision model is offered it.
    _REPLY_ACTIONS = {
        "approve": "approve the decision as recommended",
        "reject": "reject the decision",
        "revise": "send it back asking for a revision",
        "execute": "mark an approved decision as executed",
        "delegate": "delegate the decision to someone else (named in the reply)",
        "none": "the reply does not answer the decision (a question, a comment, an "
                "out-of-office, or unclear)",
    }

    def decide_reply(self, connection: Connection, project_id: str, parsed: dict,
                     subject: Optional[str], body: str, *, adapter: Any = None) -> dict:
        """What a person's emailed or chatted reply to a decision MEANS, decided by a model.

        The reply, the decision it answers (its question and options) and the keyword hint go
        to `agent_decision.decide`. Below the confidence bar, or with no model, NOTHING is
        transitioned: the reply is parked with the question to answer instead."""
        from . import agent_decision

        if parsed.get("kind") != "decision_reply" or parsed.get("parse_error") \
                or not parsed.get("decision_id"):
            return parsed
        row = None
        try:
            with connection.begin_nested():
                row = connection.execute(text(
                    "SELECT id, question, options, status, recommendation FROM decisions "
                    "WHERE id = :d AND project_id = :p"),
                    {"d": parsed["decision_id"], "p": project_id}).mappings().first()
        except Exception:  # noqa: BLE001 — the decision is context; the API checks it again
            row = None
        context: dict[str, Any] = {}
        if row is not None:
            context["the decision"] = {
                "id": row["id"], "question": row["question"], "status": row["status"],
                "options": row["options"]}
        hints = ([f"the first decision word in the reply is '{parsed['action_hint']}'"]
                 if parsed.get("action_hint") else [])
        verdict = agent_decision.decide(
            connection, self.settings, project_id,
            situation=("A person replied by " + str(parsed.get("source_channel") or "message")
                       + " to a decision BOSS asked them. Decide what the reply does to it."),
            message="\n".join(x for x in (subject or "", body or "") if x),
            context=context, hints=hints, agent="BOSS inbox",
            ref=f"inbox:{parsed['decision_id']}", adapter=adapter,
            candidates=[agent_decision.Candidate(k, v) for k, v in self._REPLY_ACTIONS.items()])
        parsed["decided_by"] = verdict.to_dict()
        if verdict.decided and verdict.action != "none":
            parsed["action"] = verdict.action
        elif verdict.status == agent_decision.NO_MODEL:
            parsed["parse_error"] = "decision_reply_needs_a_model"
            parsed["remediation"] = agent_decision.NO_MODEL_REPLY
        else:
            parsed["parse_error"] = "decision_reply_unclear"
            parsed["remediation"] = (verdict.question or
                                     "Reply with what you want done to the decision.")
        return parsed

    _COMMAND_TOKEN_RE = re.compile(
        r"\[(org|ws|workspace|proj|project|thread):([^\]]+)\]", re.I
    )

    def _parse_command_tokens(self, subject: Optional[str], body: Optional[str]) -> dict:
        """Extract structured email command tokens — [org:..][ws:..][proj:..][thread:..] — that scope
        an inbound command to an org/workspace/project and thread (spec §D email command contracts)."""
        blob = " ".join(item for item in [subject or "", body or ""] if item)
        context: dict[str, str] = {}
        for key, value in self._COMMAND_TOKEN_RE.findall(blob):
            key = key.lower()
            value = value.strip()
            if not value:
                continue
            if key == "org":
                context["org"] = value
            elif key in ("ws", "workspace"):
                context["workspace"] = value
            elif key in ("proj", "project"):
                context["project"] = value
            elif key == "thread":
                context["thread_id"] = value
        return context

    def _derive_thread_id(self, subject: Optional[str], raw_ref: Optional[str]) -> str:
        """Deterministic thread id for an email: stable across Re:/Fwd: so a conversation coheres."""
        if raw_ref:
            base = raw_ref.strip().lower()
        else:
            base = re.sub(r"\b(re|fwd|fw):\s*", "", (subject or "").lower()).strip()
        return "thread_" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

    def _command_remediation(self, is_email: bool) -> str:
        """Deterministic remediation for a malformed command — tells the sender how to fix it."""
        lead = (
            "Email commands must name a decision and an action. "
            "Example subject: '[proj:boss] approve dec_ab12'. "
            if is_email
            else "Replies must name a decision and an action. "
        )
        return lead + "Valid actions: approve, reject, revise, execute, delegate."
