from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import create_blocking_decision, emit_event, make_id, now_utc
from .receipts import ReceiptService
from .slack_adapter import SlackAdapter
from .github_adapter import IntegrationConfigurationError


class OutboxMessageNotFoundError(RuntimeError):
    pass


class InvalidCreatedByJobError(RuntimeError):
    pass


class CommsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self.slack = SlackAdapter(settings)

    def create_outbox_message(
        self,
        connection: Connection,
        project_id: str,
        channel: str,
        subject: Optional[str],
        body: str,
        dedupe_key: str,
        actor: str,
    ) -> dict:
        message_id = make_id("out")
        connection.execute(
            text(
                """
                INSERT INTO outbox_messages (
                    id, project_id, channel, subject, body, status, dedupe_key
                )
                VALUES (
                    :id, :project_id, :channel, :subject, :body, 'draft', :dedupe_key
                )
                """
            ),
            {
                "id": message_id,
                "project_id": project_id,
                "channel": channel,
                "subject": subject,
                "body": body,
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
            {"channel": channel},
        )
        return self.get_outbox_message(connection, message_id)

    def list_outbox_messages(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, channel, subject, body, status, dedupe_key,
                       receipt_id, created_at, updated_at
                FROM outbox_messages
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def get_outbox_message(self, connection: Connection, message_id: str) -> dict:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, channel, subject, body, status, dedupe_key,
                       receipt_id, created_at, updated_at
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
    ) -> dict:
        message = self.get_outbox_message(connection, message_id)
        job_id = make_id("job")
        run_after = now_utc()
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, project_id, task_id, kind, priority, status, payload,
                    max_attempts, attempt, run_after
                )
                VALUES (
                    :id, :project_id, NULL, 'comms.outbox.deliver', :priority, 'queued',
                    CAST(:payload AS JSONB), :max_attempts, 0, :run_after
                )
                """
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "priority": priority,
                "payload": json.dumps(
                    {
                        "project_id": project_id,
                        "outbox_message_id": message_id,
                        "reservation_id": reservation_id,
                        "dedupe_key": dedupe_key,
                        "channel": message["channel"],
                        "subject": message.get("subject"),
                        "body": message["body"],
                        "requires_reservation": True,
                    }
                ),
                "max_attempts": max_attempts,
                "run_after": run_after,
            },
        )
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            job_id,
            {"kind": "comms.outbox.deliver", "task_id": None},
        )
        job = {
            "id": job_id,
            "project_id": project_id,
            "task_id": None,
            "kind": "comms.outbox.deliver",
            "status": "queued",
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
        emit_event(
            connection,
            project_id,
            "outbox.queued",
            actor,
            "job",
            job["id"],
            {"outbox_message_id": message_id, "reservation_id": reservation_id},
        )
        return job

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
            return {"status": "blocked", "decision_id": decision_id}

        if not message["channel"].startswith("slack:"):
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
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "message": f"Unsupported channel: {message['channel']}",
            }

        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "slack.message.post",
            {"channel": message["channel"], "outbox_message_id": message_id},
            {
                "channel": message["channel"],
                "subject": message.get("subject"),
                "body": message["body"],
            },
            created_by_job_id=created_by_job_id,
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

        self._set_outbox_status(connection, message_id, "sending", receipt["id"])
        try:
            provider_ref = self.slack.post_message(
                message["channel"],
                message.get("subject"),
                message["body"],
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(connection, receipt["id"], {"message": str(error)})
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because Slack delivery is disabled or unconfigured.",
            )
            self._set_outbox_status(connection, message_id, "blocked", receipt["id"])
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Slack delivery is blocked by policy or missing configuration",
                {"action": "configure_or_enable_slack_delivery"},
                {"outbox_message_id": message_id, "channel": message["channel"]},
                "slack_delivery_blocked",
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
                "Released reservation because Slack delivery failed before acknowledgement.",
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
            "slack.post_real",
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

    def list_inbox_messages(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, channel, raw_ref, subject, body, status, parsed,
                       created_at, updated_at
                FROM inbox_messages
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
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
    ) -> dict:
        message_id = make_id("in")
        parsed = {"channel": channel, "ingested_at": now_utc().isoformat()}
        connection.execute(
            text(
                """
                INSERT INTO inbox_messages (
                    id, project_id, channel, raw_ref, subject, body, status, parsed
                )
                VALUES (
                    :id, :project_id, :channel, :raw_ref, :subject, :body, 'received',
                    CAST(:parsed AS JSONB)
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
            {"channel": channel, "raw_ref": raw_ref},
        )
        row = connection.execute(
            text(
                """
                SELECT id, project_id, channel, raw_ref, subject, body, status, parsed,
                       created_at, updated_at
                FROM inbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()
        return dict(row)

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
