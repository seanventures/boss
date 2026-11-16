"""File a browser or phone transcript the person already has onto a task.

The words are stored as submitted. Nothing here opens a phone line or calls a
speech vendor. The transcript id is the receipt's dedupe key, so a second
submit of that id returns the first filing.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id
from .receipts import ReceiptService

EFFECT_KIND = "task.voice_transcript"
SOURCES = frozenset({"browser", "phone"})

_COLUMNS = (
    "id, org_id, workspace_id, project_id, task_id, transcript_id, "
    "source, body, submitted_by, receipt_id, created_at"
)


class TaskVoiceError(Exception):
    """A filing that did not happen. `code` is stable; the message is for a person."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TaskVoiceService:
    def attach(
        self,
        connection: Connection,
        *,
        org_id: str,
        project_id: str,
        task_id: str,
        transcript_id: str,
        source: str,
        body: str,
        submitted_by: str,
    ) -> dict:
        """Store one transcript on the task and write one succeeded receipt.

        A later call with the same transcript id returns the stored row and
        does not write another receipt, even if the words differ.
        """
        transcript_id = _required(transcript_id, "transcript_id_required", "A transcript id is required.")
        submitted_by = _required(submitted_by, "submitted_by_required", "Say who filed these words.")
        source_name = str(source or "").strip().lower()
        if source_name not in SOURCES:
            raise TaskVoiceError(
                "source_not_accepted",
                "File a browser or phone transcript. BOSS does not open a phone line.",
            )
        words = str(body or "").strip()
        if not words:
            raise TaskVoiceError(
                "transcript_empty",
                "Paste the transcript. BOSS does not call a speech vendor.",
            )

        existing = self._by_transcript_id(connection, org_id, transcript_id)
        if existing:
            return _public(existing, created=False)

        workspace_id = self._workspace_id(connection, org_id, project_id)
        boundary = self._task_boundary(connection, project_id, task_id)
        dedupe_key = f"task.voice:{org_id}:{transcript_id}"
        payload = {
            "task_id": task_id,
            "transcript_id": transcript_id,
            "source": source_name,
            "submitted_by": submitted_by,
            "body": words,
        }
        receipt, _created = ReceiptService().record_completed(
            connection,
            org_id,
            workspace_id,
            project_id,
            dedupe_key,
            EFFECT_KIND,
            {"task_id": task_id, "transcript_id": transcript_id, "source": source_name},
            payload,
            provider_ref=None,
            data_boundary=boundary,
        )
        row = connection.execute(
            text(
                f"""
                INSERT INTO task_voice_transcripts (
                    id, org_id, workspace_id, project_id, task_id, transcript_id,
                    source, body, submitted_by, receipt_id, created_at
                ) VALUES (
                    :id, :org_id, :workspace_id, :project_id, :task_id, :transcript_id,
                    :source, :body, :submitted_by, :receipt_id, clock_timestamp()
                )
                RETURNING {_COLUMNS}
                """
            ),
            {
                "id": make_id("tvt"),
                "org_id": org_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "task_id": task_id,
                "transcript_id": transcript_id,
                "source": source_name,
                "body": words,
                "submitted_by": submitted_by,
                "receipt_id": receipt["id"],
            },
        ).mappings().one()
        return _public(dict(row), created=True)

    def list_for_task(
        self,
        connection: Connection,
        *,
        org_id: str,
        project_id: str,
        task_id: str,
    ) -> list[dict]:
        """Transcripts filed on this task, oldest first."""
        self._workspace_id(connection, org_id, project_id)
        self._task_boundary(connection, project_id, task_id)
        rows = connection.execute(
            text(
                f"""
                SELECT {_COLUMNS}
                FROM task_voice_transcripts
                WHERE org_id = :org_id AND project_id = :project_id AND task_id = :task_id
                ORDER BY filed_order ASC
                """
            ),
            {"org_id": org_id, "project_id": project_id, "task_id": task_id},
        ).mappings().all()
        return [_public(dict(row)) for row in rows]

    def _by_transcript_id(
        self, connection: Connection, org_id: str, transcript_id: str
    ) -> Optional[dict]:
        row = connection.execute(
            text(
                f"""
                SELECT {_COLUMNS}
                FROM task_voice_transcripts
                WHERE org_id = :org_id AND transcript_id = :transcript_id
                """
            ),
            {"org_id": org_id, "transcript_id": transcript_id},
        ).mappings().first()
        return dict(row) if row else None

    def _workspace_id(self, connection: Connection, org_id: str, project_id: str) -> str:
        row = connection.execute(
            text(
                """
                SELECT p.workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id AND w.org_id = :org_id
                """
            ),
            {"project_id": project_id, "org_id": org_id},
        ).first()
        if row is None:
            raise TaskVoiceError(
                "project_not_found",
                "That project is not in this organization.",
            )
        return str(row[0])

    def _task_boundary(self, connection: Connection, project_id: str, task_id: str) -> str:
        row = connection.execute(
            text(
                """
                SELECT data_boundary
                FROM tasks
                WHERE id = :task_id AND project_id = :project_id
                """
            ),
            {"task_id": task_id, "project_id": project_id},
        ).first()
        if row is None:
            raise TaskVoiceError("task_not_found", "That task is not in this project.")
        boundary = str(row[0] or "internal")
        return boundary


def _required(value: str, code: str, message: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise TaskVoiceError(code, message)
    return text_value


def _public(row: dict, *, created: Optional[bool] = None) -> dict:
    created_at = row.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    shown = {
        "id": row["id"],
        "transcript_id": row["transcript_id"],
        "task_id": row["task_id"],
        "project_id": row["project_id"],
        "source": row["source"],
        "body": row["body"],
        "submitted_by": row["submitted_by"],
        "receipt_id": row["receipt_id"],
        "created_at": created_at,
    }
    if created is not None:
        shown["created"] = created
    return shown
