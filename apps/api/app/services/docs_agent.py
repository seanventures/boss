from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import PurePosixPath
import re
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .code_agent import CodeAgent, make_code_agent
from .core import create_blocking_decision, emit_event
from .feature_catalog import FeatureCatalog
from .github_adapter import GitHubAdapter, IntegrationConfigurationError
from .jobs import enqueue_job
from .receipts import ReceiptService
from .redaction import redact


MANAGED_START = "<!-- BOSS:DOCS:START -->"
MANAGED_END = "<!-- BOSS:DOCS:END -->"
REQUIRED_SECTIONS = (
    "## What it does",
    "## How it works",
    "## API / interface",
    "## Configuration",
    "## Status & limitations",
)
STATUS_ORDER = {"Planned": 0, "Blocked": 0, "Partial": 1, "Implemented": 2}


def docs_trigger_events(settings: Settings) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in settings.docs_trigger_events.split(",")
        if item.strip()
    )


def docs_update_dedupe_key(
    settings: Settings,
    project_id: str,
    entity_type: str,
    entity_id: str,
    *,
    now: Optional[float] = None,
) -> str:
    debounce_seconds = max(int(settings.docs_debounce_seconds), 1)
    bucket = int((time.time() if now is None else now) // debounce_seconds)
    return f"docs.update:{project_id}:{entity_type}:{entity_id}:{bucket}"


def enqueue_docs_update_job(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    event_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    task_id: Optional[str] = None,
    data_boundary: str = "internal",
    event_actor: str = "manual",
    subscription_id: Optional[str] = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Enqueue or coalesce the newest event into one entity/window job."""
    if event_type not in docs_trigger_events(settings):
        raise ValueError("event_type is not enabled for documentation updates.")
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "event_actor": event_actor,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "data_boundary": data_boundary,
        **({"subscription_id": subscription_id} if subscription_id else {}),
    }
    job = enqueue_job(
        connection,
        project_id,
        "docs.update",
        payload,
        task_id=task_id,
        priority=priority,
        max_attempts=max_attempts,
        data_boundary=data_boundary,
        dedupe_key=docs_update_dedupe_key(
            settings, project_id, entity_type, entity_id
        ),
    )
    if not job["created"]:
        # If the worker has not leased the debounce bucket yet, retain the latest
        # completion evidence (e.g. merged supersedes pr_opened) without a second job.
        connection.execute(
            text(
                """
                UPDATE jobs
                SET payload = CAST(:payload AS JSONB),
                    task_id = COALESCE(:task_id, task_id),
                    data_boundary = :data_boundary,
                    updated_at = now()
                WHERE id = :job_id AND status IN ('queued', 'failed_retryable')
                """
            ),
            {
                "job_id": job["id"],
                "payload": json.dumps(payload),
                "task_id": task_id,
                "data_boundary": data_boundary,
            },
        )
    return job


def validate_markdown_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.suffix.lower() != ".md"
    ):
        raise ValueError("Documentation output paths must be relative .md files.")
    return candidate.as_posix()


def _strip_markdown_fence(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", value, re.DOTALL)
    return match.group(1).strip() if match else value


def merge_managed_markdown(prior: str, generated: str) -> str:
    """Replace only the BOSS-managed block and preserve every human-authored byte."""
    generated = _strip_markdown_fence(generated)
    missing = [heading for heading in REQUIRED_SECTIONS if heading not in generated]
    if missing:
        raise ValueError(
            "Docs writer omitted required sections: " + ", ".join(missing)
        )
    managed = f"{MANAGED_START}\n{generated.rstrip()}\n{MANAGED_END}"
    start = prior.find(MANAGED_START)
    end = prior.find(MANAGED_END)
    if start >= 0 and end >= start:
        end += len(MANAGED_END)
        return f"{prior[:start]}{managed}{prior[end:]}"
    if prior.strip():
        separator = (
            ""
            if prior.endswith("\n\n")
            else ("\n" if prior.endswith("\n") else "\n\n")
        )
        return f"{prior}{separator}{managed}\n"
    return f"{managed}\n"


def target_status(current_status: str, event_type: str) -> str:
    if current_status == "Blocked":
        return current_status
    proposed = (
        "Implemented"
        if event_type == "software_factory.pull_request_merged"
        else "Partial"
    )
    if STATUS_ORDER.get(current_status, 0) > STATUS_ORDER[proposed]:
        return current_status
    return proposed


def reconcile_traceability(
    markdown: str,
    label: str,
    status: str,
    *,
    today: Optional[date] = None,
) -> str:
    """Update one requirement status and the ledger freshness line."""
    if status not in STATUS_ORDER:
        raise ValueError("Unsupported traceability status.")
    updated_on = today or date.today()
    fresh = re.sub(
        r"^Last updated: .+$",
        f"Last updated: {updated_on.strftime('%B')} {updated_on.day}, {updated_on.year}",
        markdown,
        count=1,
        flags=re.MULTILINE,
    )
    lines = fresh.splitlines()
    found = False
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or not cells[0].startswith(f"{label} "):
            continue
        cells[-1] = status
        lines[index] = "| " + " | ".join(cells) + " |"
        found = True
        break
    if not found:
        raise ValueError(f"Requirement {label} was not found in TRACEABILITY.md.")
    return "\n".join(lines) + ("\n" if fresh.endswith("\n") else "")


@dataclass
class DocsDraft:
    markdown: str
    usage: dict[str, Any]
    cost_usd_micros: int
    model: Optional[str]


class DocsAgent:
    def __init__(
        self,
        settings: Settings,
        code_agent: Optional[CodeAgent] = None,
    ):
        self.settings = settings
        self.code_agent = code_agent or make_code_agent(settings)

    def draft(
        self,
        feature: dict[str, Any],
        context: dict[str, Any],
        prior_markdown: str,
    ) -> DocsDraft:
        prompt = (
            "You are the BOSS docs_writer. Draft only Markdown for the managed portion "
            "of one feature page. Do not use a code fence. Do not invent behavior; call "
            "unknowns and unshipped behavior out under Status & limitations. Use exactly "
            "these H2 sections: What it does, How it works, API / interface, "
            "Configuration, Status & limitations. Begin with an H1 title and a one-line "
            "status/source blockquote. Prefer concise user-facing prose and cite file paths, "
            "events, job kinds, and endpoints from the supplied evidence.\n\n"
            f"FEATURE CATALOG:\n{json.dumps(feature, indent=2, default=str)}\n\n"
            f"TRIGGER CONTEXT:\n{json.dumps(context, indent=2, default=str)[:30000]}\n\n"
            f"PRIOR DOCUMENT (human prose outside managed markers is preserved by BOSS):\n"
            f"{prior_markdown[:12000]}"
        )
        redacted_prompt, _ = redact(prompt)
        result = self.code_agent.run_role(
            redacted_prompt,
            self.settings.repo_root,
            read_only=True,
        )
        markdown = merge_managed_markdown(
            prior_markdown,
            str(result.get("summary") or ""),
        )
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return DocsDraft(
            markdown=markdown,
            usage=usage,
            cost_usd_micros=int(result.get("cost_usd_micros") or 0),
            model=result.get("model") or usage.get("model"),
        )


class DocsUpdateService:
    def __init__(
        self,
        settings: Settings,
        *,
        agent: Optional[DocsAgent] = None,
        github: Optional[GitHubAdapter] = None,
    ):
        self.settings = settings
        self.catalog = FeatureCatalog(settings)
        self.agent = agent
        self.github = github or GitHubAdapter(settings)
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()

    def enqueue(
        self,
        connection: Connection,
        *,
        project_id: str,
        event_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        task_id: Optional[str] = None,
        data_boundary: str = "internal",
        actor: str = "manual",
    ) -> dict[str, Any]:
        return enqueue_docs_update_job(
            connection,
            self.settings,
            project_id=project_id,
            event_id=event_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            task_id=task_id,
            event_actor=actor,
            data_boundary=data_boundary,
            max_attempts=3,
        )

    def run_update(
        self,
        connection: Connection,
        *,
        project_id: str,
        event_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        created_by_job_id: Optional[str],
        task_id: Optional[str] = None,
        data_boundary: str = "internal",
    ) -> dict[str, Any]:
        if event_type not in docs_trigger_events(self.settings):
            return {
                "status": "failed",
                "retryable": False,
                "message": "event_type is not enabled for documentation updates.",
            }
        if data_boundary not in {"public", "internal"}:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "Data boundary blocks documentation GitHub effect",
                {"action": "reclassify_or_configure_approved_boundary_adapter"},
                {},
                "data_boundary_blocks_external_effect",
            )
            emit_event(
                connection,
                project_id,
                "policy.denied",
                "docs-agent",
                "docs_update",
                entity_id,
                {
                    "data_boundary": data_boundary,
                    "decision_id": decision_id,
                    "reason": "documentation_external_effect_boundary_blocked",
                },
            )
            return {
                "status": "blocked",
                "retryable": False,
                "decision_id": decision_id,
                "message": f"{data_boundary} data cannot be sent to GitHub.",
            }

        feature = self.catalog.resolve(
            connection,
            project_id,
            entity_type,
            entity_id,
            event_id=event_id,
            actor="docs-agent",
        )
        if feature["idempotent"]:
            return {
                "status": "reused",
                "feature_id": feature["id"],
                "message": "The triggering event was already documented.",
            }

        reservation = self.budget.reserve(
            connection,
            project_id,
            category="docs_update",
            amount_usd_micros=int(self.settings.docs_update_budget_usd_micros),
            reason=f"documentation update for {feature['slug']}",
            expires_in_minutes=20,
            job_id=created_by_job_id,
        )
        if reservation.get("status") != "reserved":
            return {
                "status": "blocked",
                "retryable": False,
                "decision_id": reservation.get("decision_id"),
                "message": reservation.get("message") or "Docs budget denied.",
            }
        reservation_id = str(reservation["reservation_id"])
        scope = self._scope(connection, project_id)
        branch_name = f"boss/docs/{feature['slug']}"
        doc_path = validate_markdown_path(feature["doc_path"])
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            f"docs.update:{event_id}",
            "github.docs_pull_request.create",
            {
                "repository": self.github.repository_full_name,
                "branch_name": branch_name,
            },
            {
                "event_id": event_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "feature_id": feature["id"],
                "doc_path": doc_path,
            },
            created_by_job_id=created_by_job_id,
            data_boundary=data_boundary,
        )
        if not created and receipt["status"] == "succeeded":
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Documentation PR receipt already succeeded.",
            )
            result = receipt.get("result") or {}
            self.catalog.mark_documented(
                connection,
                project_id,
                feature["id"],
                event_id,
                status=result.get("feature_status"),
                actor="docs-agent",
            )
            return {"status": "reused", "receipt_id": receipt["id"], **result}

        try:
            context = self._assemble_context(
                connection, project_id, entity_type, entity_id, event_id, event_type
            )
            prior_markdown = self._read_local_markdown(doc_path)
            remote_prior = self.github.read_text_file(branch_name, doc_path)
            if remote_prior is not None:
                prior_markdown = remote_prior
            agent = self.agent or DocsAgent(self.settings)
            draft = agent.draft(feature, context, prior_markdown)
            files = [(doc_path, draft.markdown)]
            feature_status = target_status(feature["status"], event_type)
            if feature.get("label"):
                traceability_path = "docs/product/TRACEABILITY.md"
                traceability = self._read_local_markdown(traceability_path)
                files.append(
                    (
                        traceability_path,
                        reconcile_traceability(
                            traceability,
                            feature["label"],
                            feature_status,
                        ),
                    )
                )
            self.github.ensure_branch(branch_name)
            commit_shas = [
                self.github.upsert_file(
                    branch_name,
                    path,
                    content,
                    f"docs: update {feature['slug']} ({event_id})",
                )
                for path, content in files
            ]
            pull_request = self.github.create_pull_request(
                branch_name,
                f"docs: update {feature['title']}",
                self._pull_request_body(feature, context, event_id, event_type),
            )
        except IntegrationConfigurationError as error:
            return self._fail(
                connection,
                project_id,
                reservation_id,
                receipt["id"],
                error,
                retryable=False,
            )
        except Exception as error:  # noqa: BLE001 - durable worker retry owns recovery
            return self._fail(
                connection,
                project_id,
                reservation_id,
                receipt["id"],
                error,
                retryable=True,
            )

        documented = self.catalog.mark_documented(
            connection,
            project_id,
            feature["id"],
            event_id,
            status=feature_status,
            actor="docs-agent",
        )
        result = {
            "feature_id": feature["id"],
            "feature_status": feature_status,
            "doc_path": doc_path,
            "branch_name": branch_name,
            "pull_request_url": pull_request.url,
            "pull_request_number": pull_request.number,
            "commit_shas": commit_shas,
            "last_documented_at": documented["last_documented_at"],
        }
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            pull_request.url,
            result,
        )
        self.budget.settle(
            connection,
            project_id,
            reservation_id,
            receipt["id"],
            "model_and_tool",
            draft.model or "docs_writer",
            int(self.settings.docs_update_budget_usd_micros),
            draft.cost_usd_micros,
        )
        emit_event(
            connection,
            project_id,
            "docs.pr_opened",
            "docs-agent",
            "feature_catalog_entry",
            feature["id"],
            {
                "event_id": event_id,
                "event_type": event_type,
                "source_entity_type": entity_type,
                "source_entity_id": entity_id,
                "receipt_id": receipt["id"],
                **result,
            },
        )
        return {"status": "succeeded", "receipt_id": receipt["id"], **result}

    def _assemble_context(
        self,
        connection: Connection,
        project_id: str,
        entity_type: str,
        entity_id: str,
        event_id: str,
        event_type: str,
    ) -> dict[str, Any]:
        if entity_type == "code_task_run":
            row = connection.execute(
                text(
                    """
                    SELECT id, task_id, title, spec, state, pull_request_url,
                           pull_request_number, decision_id
                    FROM code_task_runs
                    WHERE id = :entity_id AND project_id = :project_id
                    """
                ),
                {"entity_id": entity_id, "project_id": project_id},
            ).mappings().first()
            if row is None:
                raise ValueError("Code task run not found in project.")
            source = dict(row)
            task_id = row["task_id"]
        elif entity_type == "discovery_run":
            row = connection.execute(
                text(
                    """
                    SELECT id, title, seed, context, state, usage
                    FROM discovery_runs
                    WHERE id = :entity_id AND project_id = :project_id
                    """
                ),
                {"entity_id": entity_id, "project_id": project_id},
            ).mappings().first()
            if row is None:
                raise ValueError("Discovery run not found in project.")
            source = dict(row)
            task_id = None
        elif entity_type == "plan":
            row = connection.execute(
                text(
                    """
                    SELECT id, title, objective, phases, success_criteria, risks
                    FROM plans
                    WHERE id = :entity_id AND project_id = :project_id
                    """
                ),
                {"entity_id": entity_id, "project_id": project_id},
            ).mappings().first()
            if row is None:
                raise ValueError("Plan not found in project.")
            source = dict(row)
            task_id = None
        elif entity_type == "artifact":
            row = connection.execute(
                text(
                    """
                    SELECT id, task_id, artifact_type, title, uri, content,
                           data_boundary, created_by_job_id
                    FROM artifacts
                    WHERE id = :entity_id AND project_id = :project_id
                    """
                ),
                {"entity_id": entity_id, "project_id": project_id},
            ).mappings().first()
            if row is None:
                raise ValueError("Artifact not found in project.")
            source = dict(row)
            task_id = row["task_id"]
        else:
            raise ValueError(
                "entity_type must be one of: code_task_run, discovery_run, plan, artifact."
            )

        artifacts: list[dict[str, Any]] = []
        if task_id:
            artifacts = [
                dict(item)
                for item in connection.execute(
                    text(
                        """
                        SELECT id, artifact_type, title, uri, content, data_boundary
                        FROM artifacts
                        WHERE project_id = :project_id AND task_id = :task_id
                        ORDER BY created_at DESC
                        LIMIT 20
                        """
                    ),
                    {"project_id": project_id, "task_id": task_id},
                ).mappings().all()
            ]
        return {
            "event": {"id": event_id, "type": event_type},
            "source_entity": source,
            "spec_artifacts": artifacts,
        }

    def _scope(self, connection: Connection, project_id: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Unknown project_id: {project_id}")
        return dict(row)

    def _read_local_markdown(self, path: str) -> str:
        safe_path = validate_markdown_path(path)
        target = (self.settings.repo_root / safe_path).resolve()
        root = self.settings.repo_root.resolve()
        if root not in target.parents:
            raise ValueError("Documentation path escaped the repository root.")
        return target.read_text(encoding="utf-8") if target.exists() else ""

    def _pull_request_body(
        self,
        feature: dict[str, Any],
        context: dict[str, Any],
        event_id: str,
        event_type: str,
    ) -> str:
        source = context["source_entity"]
        artifacts = context.get("spec_artifacts") or []
        source_pr = source.get("pull_request_url")
        lines = [
            "Automated draft documentation update. Human review and merge are required.",
            "",
            "## Evidence",
            f"- Feature: `{feature.get('label') or feature['id']}` — {feature['title']}",
            f"- Trigger: `{event_type}` / `{event_id}`",
            f"- Source entity: `{source.get('id')}`",
        ]
        if source_pr:
            lines.append(f"- Source pull request: {source_pr}")
        if artifacts:
            lines.append(
                "- Spec artifacts: "
                + ", ".join(f"`{artifact['id']}`" for artifact in artifacts)
            )
        lines.extend(
            [
                "",
                "## Guardrails",
                "- Draft PR only; BOSS does not merge documentation automatically.",
                "- Human-authored prose outside BOSS managed markers is preserved.",
                "- TRACEABILITY reconciliation is included for labelled features.",
            ]
        )
        return "\n".join(lines)

    def _fail(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        receipt_id: str,
        error: Exception,
        *,
        retryable: bool,
    ) -> dict[str, Any]:
        message = str(error)[:2000]
        self.receipts.mark_failed(connection, receipt_id, {"message": message})
        self.budget.release(
            connection,
            project_id,
            reservation_id,
            f"Documentation update failed: {message[:300]}",
        )
        emit_event(
            connection,
            project_id,
            "docs.update_failed",
            "docs-agent",
            "receipt",
            receipt_id,
            {"message": message, "retryable": retryable},
        )
        return {
            "status": "failed",
            "retryable": retryable,
            "receipt_id": receipt_id,
            "message": message,
        }
