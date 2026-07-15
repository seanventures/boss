from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .github_adapter import GitHubAdapter
from .receipts import ReceiptService


class GitHubCommunityAgentBase:
    """Shared repository scoping and receipt boundary for community agents."""

    def __init__(
        self,
        settings: Settings,
        adapter_factory: Optional[Callable[[Settings], GitHubAdapter]] = None,
    ) -> None:
        self.settings = settings
        self.receipts = ReceiptService()
        self.adapter_factory = adapter_factory or GitHubAdapter

    @staticmethod
    def _scope(connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT w.id AS workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Unknown project_id: {project_id}")
        return dict(row)

    def _adapter(self, repository: str) -> GitHubAdapter:
        parts = repository.split("/", 1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ValueError("GitHub repository must use owner/name format.")
        scoped = self.settings.model_copy(
            update={
                "target_github_owner": parts[0].strip(),
                "target_github_repo": parts[1].strip(),
            }
        )
        return self.adapter_factory(scoped)

    def _effect(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: Optional[str],
        dedupe_key: str,
        effect_kind: str,
        effect_scope: dict[str, Any],
        payload: dict[str, Any],
        invoke: Callable[[], Any],
        serialize: Optional[Callable[[Any], dict[str, Any]]] = None,
    ) -> tuple[dict[str, Any], bool]:
        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            effect_kind,
            effect_scope,
            payload,
            created_by_job_id=job_id,
            data_boundary="internal",
        )
        if not created and receipt["status"] == "succeeded":
            return dict(receipt.get("result") or {}), False
        try:
            value = invoke()
            if serialize is not None:
                result = serialize(value)
            elif is_dataclass(value):
                result = asdict(value)
            elif isinstance(value, dict):
                result = value
            else:
                result = {"value": value}
            provider_ref = str(
                result.get("url") or result.get("id") or effect_scope.get("repository")
            )
            self.receipts.mark_succeeded(
                connection, receipt["id"], provider_ref, result
            )
            return result, True
        except Exception as error:
            self.receipts.mark_failed(
                connection,
                receipt["id"],
                {"message": str(error), "type": error.__class__.__name__},
            )
            raise
