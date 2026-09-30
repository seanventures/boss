from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id, now_utc
from .github_branches import GitHubBranchService
from .github_connections import GitHubConnectionService
from .jobs import JobLeaseService, enqueue_job
from .receipts import ReceiptService


class GitHubReconciliationNotFoundError(RuntimeError):
    pass


class GitHubReconciliationStateError(RuntimeError):
    pass


class GitHubReconciliationProviderError(RuntimeError):
    pass


class GitHubReconciliationService:
    """Converge webhook and poll signals onto durable project repository truth."""

    MAX_REFS = 100
    MAX_ATTEMPTS = 3
    _SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
    _ZERO_SHA = re.compile(r"^0{40,64}$")
    RECONCILIATION_EVENTS = {
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

    def __init__(
        self,
        settings: Settings,
        *,
        github_connections: GitHubConnectionService,
    ) -> None:
        self.settings = settings
        self.github_connections = github_connections
        self.receipts = ReceiptService()
        self.jobs = JobLeaseService(settings)

    def ingest_inbound_webhook(
        self,
        connection: Connection,
        *,
        inbound_webhook_id: str,
        actor: str = "github",
    ) -> list[dict[str, Any]]:
        inbound = connection.execute(
            text(
                """
                SELECT id, project_id, source, delivery_key, repo_full_name,
                       provider_event, action, status, payload_hash, payload,
                       created_at
                FROM inbound_webhooks
                WHERE id = :inbound_webhook_id
                """
            ),
            {"inbound_webhook_id": inbound_webhook_id},
        ).mappings().first()
        if inbound is None:
            raise GitHubReconciliationNotFoundError(
                "Inbound GitHub webhook was not found."
            )
        if (
            inbound["source"] != "github"
            or inbound["status"] != "accepted"
            or inbound["provider_event"] not in self.RECONCILIATION_EVENTS
        ):
            return []
        binding = connection.execute(
            text(
                """
                SELECT id, project_id, full_name, default_branch
                FROM github_repository_bindings
                WHERE project_id = :project_id
                  AND status = 'active'
                  AND lower(full_name) = lower(:repository)
                ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """
            ),
            {
                "project_id": inbound["project_id"],
                "repository": inbound["repo_full_name"],
            },
        ).mappings().first()
        if binding is None:
            emit_event(
                connection,
                inbound["project_id"],
                "github.reconciliation_unbound",
                actor,
                "inbound_webhook",
                inbound_webhook_id,
                {
                    "repository": inbound["repo_full_name"],
                    "delivery_id": inbound["delivery_key"],
                },
            )
            return []
        refs = self._refs_from_payload(
            inbound["provider_event"],
            dict(inbound["payload"] or {}),
            default_branch=str(binding["default_branch"] or "main"),
            repository_full_name=str(binding["full_name"]),
        )
        scheduled = []
        for ref in refs:
            signal = self._create_or_get_signal(
                connection,
                project_id=inbound["project_id"],
                repository_binding_id=binding["id"],
                inbound_webhook_id=inbound["id"],
                delivery_id=inbound["delivery_key"],
                provider_event=inbound["provider_event"],
                action=inbound["action"],
                ref_name=ref["ref_name"],
                expected_commit_sha=ref.get("expected_commit_sha"),
                expected_deleted=bool(ref.get("expected_deleted", False)),
                payload_hash=inbound["payload_hash"],
                provider_occurred_at=ref.get("provider_occurred_at"),
                actor=actor,
            )
            run = self._schedule_run(
                connection,
                project_id=inbound["project_id"],
                repository_binding_id=binding["id"],
                source="webhook",
                requested_refs=[ref["ref_name"]],
                dedupe_key=f"github-reconcile:signal:{signal['id']}",
                trigger_signal_id=signal["id"],
                repair_of_run_id=None,
                actor=actor,
            )
            connection.execute(
                text(
                    """
                    UPDATE github_reconciliation_signals
                    SET reconciliation_run_id = :run_id, updated_at = now()
                    WHERE id = :signal_id
                    """
                ),
                {"signal_id": signal["id"], "run_id": run["id"]},
            )
            scheduled.append(
                {
                    "signal": self.get_signal(
                        connection, inbound["project_id"], signal["id"]
                    ),
                    "run": run,
                }
            )
        return scheduled

    def schedule_poll(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: Optional[str],
        refs: Optional[list[str]],
        dedupe_key: str,
        actor: str,
    ) -> list[dict[str, Any]]:
        safe_dedupe = self._required_text(dedupe_key, "dedupe_key")
        filters = ["project_id = :project_id", "status = 'active'"]
        params: dict[str, Any] = {"project_id": project_id}
        if repository_binding_id:
            filters.append("id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        bindings = connection.execute(
            text(
                f"""
                SELECT id, default_branch
                FROM github_repository_bindings
                WHERE {" AND ".join(filters)}
                ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, created_at
                """
            ),
            params,
        ).mappings().all()
        if not bindings:
            raise GitHubReconciliationNotFoundError(
                "No active GitHub repository binding matched the poll request."
            )
        runs = []
        for binding in bindings:
            requested_refs = (
                self._normalize_refs(refs)
                if refs
                else self._discover_refs(
                    connection,
                    repository_binding_id=binding["id"],
                    default_branch=binding["default_branch"] or "main",
                )
            )
            runs.append(
                self._schedule_run(
                    connection,
                    project_id=project_id,
                    repository_binding_id=binding["id"],
                    source="poll",
                    requested_refs=requested_refs,
                    dedupe_key=f"{safe_dedupe}:{binding['id']}",
                    trigger_signal_id=None,
                    repair_of_run_id=None,
                    actor=actor,
                )
            )
        return runs

    def repair(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        run = self._locked_run(connection, project_id, run_id)
        if run["status"] not in {"failed", "deadlettered"}:
            raise GitHubReconciliationStateError(
                "Only failed or deadlettered reconciliation runs can be repaired."
            )
        repaired = self._schedule_run(
            connection,
            project_id=project_id,
            repository_binding_id=run["repository_binding_id"],
            source="repair",
            requested_refs=list(run["requested_refs"] or []),
            dedupe_key=self._required_text(dedupe_key, "dedupe_key"),
            trigger_signal_id=run["trigger_signal_id"],
            repair_of_run_id=run["id"],
            actor=actor,
        )
        emit_event(
            connection,
            project_id,
            "github.reconciliation_repair_scheduled",
            actor,
            "github_reconciliation_run",
            repaired["id"],
            {"repair_of_run_id": run["id"]},
        )
        return repaired

    def execute(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        job_id: str,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        job = self.jobs.require_active_lease(
            connection, worker_id, job_id, lease_token
        )
        if (
            job["project_id"] != project_id
            or job["kind"] != "github.repository.reconcile"
            or str((job.get("payload") or {}).get("run_id") or "") != run_id
        ):
            raise GitHubReconciliationStateError(
                "Job lease does not belong to this reconciliation run."
            )
        run = self._locked_run(connection, project_id, run_id)
        if run["status"] == "succeeded":
            job_result = self.jobs.complete(
                connection,
                worker_id,
                job_id,
                lease_token,
                {"reconciliation_run_id": run_id, "reused": True},
            )
            return {"run": self.get_run(connection, project_id, run_id), "job": job_result}
        if run["status"] not in {"queued", "failed"}:
            raise GitHubReconciliationStateError(
                f"Reconciliation run is {run['status']} and cannot execute."
            )
        attempt = int(job["attempt"])
        if attempt < 1 or attempt > int(run["max_attempts"]):
            raise GitHubReconciliationStateError(
                "Reconciliation job attempt is outside the run policy."
            )
        connection.execute(
            text(
                """
                UPDATE github_reconciliation_runs
                SET status = 'running', attempt_count = :attempt,
                    started_at = COALESCE(started_at, now()),
                    next_attempt_at = NULL, error = NULL, updated_at = now()
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id, "attempt": attempt},
        )
        if run["trigger_signal_id"]:
            connection.execute(
                text(
                    """
                    UPDATE github_reconciliation_signals
                    SET status = 'processing', updated_at = now()
                    WHERE id = :signal_id
                      AND status IN ('pending', 'failed')
                    """
                ),
                {"signal_id": run["trigger_signal_id"]},
            )
        try:
            adapter, _, binding = self.github_connections.adapter_for_binding(
                connection,
                project_id=project_id,
                binding_id=run["repository_binding_id"],
            )
            rate_limit, rate_receipt_id = self._provider_read(
                connection,
                project_id=project_id,
                run_id=run_id,
                binding=binding,
                dedupe_key=(
                    f"github-reconcile:{run_id}:rate-limit:{attempt}"
                ),
                effect_kind="github.repository.rate_limit",
                resource_ref="rate_limit/core",
                payload={"attempt": attempt},
                callback=adapter.rate_limit_status,
            )
            self._append_receipt(connection, run_id, rate_receipt_id)
            rate_limit = self._sanitize_rate_limit(rate_limit)
            connection.execute(
                text(
                    """
                    UPDATE github_reconciliation_runs
                    SET rate_limit_status = CAST(:rate_limit AS JSONB),
                        updated_at = now()
                    WHERE id = :run_id
                    """
                ),
                {"run_id": run_id, "rate_limit": json.dumps(rate_limit)},
            )
            if rate_limit["remaining"] <= 0:
                raise GitHubReconciliationProviderError(
                    "GitHub core API rate limit is exhausted."
                )

            observation_ids = []
            stale_signal = False
            for ref_name in list(run["requested_refs"] or []):
                observation, receipt_id = self._provider_read(
                    connection,
                    project_id=project_id,
                    run_id=run_id,
                    binding=binding,
                    dedupe_key=(
                        f"github-reconcile:{run_id}:ref:{ref_name}:{attempt}"
                    ),
                    effect_kind="github.repository.observe_ref",
                    resource_ref=f"refs/heads/{ref_name}",
                    payload={"ref_name": ref_name, "attempt": attempt},
                    callback=lambda ref=ref_name: adapter.branch_snapshot(ref),
                )
                self._append_receipt(connection, run_id, receipt_id)
                observation_id, classification = self._record_observation(
                    connection,
                    run=run,
                    ref_name=ref_name,
                    raw_observation=observation,
                    receipt_id=receipt_id,
                )
                observation_ids.append(observation_id)
                stale_signal = stale_signal or classification in {
                    "stale_signal",
                    "unexpected_reappearance",
                }
            self._invalidate_stale_work(
                connection,
                project_id=project_id,
                repository_binding_id=run["repository_binding_id"],
                actor=worker_id,
            )
        except Exception as error:
            return self._fail_execution(
                connection,
                run=run,
                job=job,
                job_id=job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                attempt=attempt,
                error=error,
            )

        connection.execute(
            text(
                """
                UPDATE github_reconciliation_runs
                SET status = 'succeeded',
                    observation_ids = CAST(:observation_ids AS JSONB),
                    error = NULL, completed_at = now(), updated_at = now()
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "observation_ids": json.dumps(observation_ids),
            },
        )
        if run["trigger_signal_id"]:
            latest = connection.execute(
                text(
                    """
                    SELECT commit_sha
                    FROM github_repository_ref_states
                    WHERE repository_binding_id = :repository_binding_id
                      AND ref_name = (
                        SELECT ref_name FROM github_reconciliation_signals
                        WHERE id = :signal_id
                      )
                    """
                ),
                {
                    "repository_binding_id": run["repository_binding_id"],
                    "signal_id": run["trigger_signal_id"],
                },
            ).scalar_one_or_none()
            connection.execute(
                text(
                    """
                    UPDATE github_reconciliation_signals
                    SET status = :status, observed_commit_sha = :observed_sha,
                        error = NULL, processed_at = now(), updated_at = now()
                    WHERE id = :signal_id
                    """
                ),
                {
                    "signal_id": run["trigger_signal_id"],
                    "status": "stale" if stale_signal else "applied",
                    "observed_sha": latest,
                },
            )
        connection.execute(
            text(
                """
                UPDATE github_repository_bindings
                SET last_synced_at = now(), updated_at = now()
                WHERE id = :repository_binding_id
                """
            ),
            {"repository_binding_id": run["repository_binding_id"]},
        )
        job_result = self.jobs.complete(
            connection,
            worker_id,
            job_id,
            lease_token,
            {
                "reconciliation_run_id": run_id,
                "observation_ids": observation_ids,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.reconciliation_succeeded",
            worker_id,
            "github_reconciliation_run",
            run_id,
            {
                "attempt": attempt,
                "observation_count": len(observation_ids),
                "stale_signal": stale_signal,
                "rate_limit": rate_limit,
            },
        )
        return {"run": self.get_run(connection, project_id, run_id), "job": job_result}

    def list_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._validate_limit(limit)
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        if status:
            filters.append("status = :status")
            params["status"] = self._required_text(status, "status")
        rows = connection.execute(
            text(
                f"""
                SELECT {self._run_columns()}
                FROM github_reconciliation_runs
                WHERE {" AND ".join(filters)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_run(row) for row in rows]

    def get_run(
        self,
        connection: Connection,
        project_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT {self._run_columns()}
                FROM github_reconciliation_runs
                WHERE project_id = :project_id AND id = :run_id
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().first()
        if row is None:
            raise GitHubReconciliationNotFoundError(
                "GitHub reconciliation run not found."
            )
        return self._serialize_run(row)

    def list_signals(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._validate_limit(limit)
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        if status:
            filters.append("status = :status")
            params["status"] = self._required_text(status, "status")
        rows = connection.execute(
            text(
                f"""
                SELECT {self._signal_columns()}
                FROM github_reconciliation_signals
                WHERE {" AND ".join(filters)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_signal(row) for row in rows]

    def get_signal(
        self,
        connection: Connection,
        project_id: str,
        signal_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT {self._signal_columns()}
                FROM github_reconciliation_signals
                WHERE project_id = :project_id AND id = :signal_id
                """
            ),
            {"project_id": project_id, "signal_id": signal_id},
        ).mappings().first()
        if row is None:
            raise GitHubReconciliationNotFoundError(
                "GitHub reconciliation signal not found."
            )
        return self._serialize_signal(row)

    def list_ref_states(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self._validate_limit(limit)
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        rows = connection.execute(
            text(
                f"""
                SELECT {self._ref_state_columns()}
                FROM github_repository_ref_states
                WHERE {" AND ".join(filters)}
                ORDER BY repository_binding_id, ref_name
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_ref_state(row) for row in rows]

    def _schedule_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        source: str,
        requested_refs: list[str],
        dedupe_key: str,
        trigger_signal_id: Optional[str],
        repair_of_run_id: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        safe_refs = self._normalize_refs(requested_refs)
        safe_dedupe = self._required_text(dedupe_key, "dedupe_key")
        replay = connection.execute(
            text(
                """
                SELECT id
                FROM github_reconciliation_runs
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": safe_dedupe},
        ).scalar_one_or_none()
        if replay:
            item = self.get_run(connection, project_id, str(replay))
            if (
                item["repository_binding_id"] != repository_binding_id
                or item["requested_refs"] != safe_refs
                or item["source"] != source
                or item["trigger_signal_id"] != trigger_signal_id
                or item["repair_of_run_id"] != repair_of_run_id
            ):
                raise GitHubReconciliationStateError(
                    "dedupe_key is bound to a different reconciliation run."
                )
            return item
        self.github_connections.get_repository_binding(
            connection, project_id, repository_binding_id
        )
        run_id = make_id("ghrecon")
        connection.execute(
            text(
                """
                INSERT INTO github_reconciliation_runs (
                    id, project_id, repository_binding_id, trigger_signal_id,
                    repair_of_run_id, source, status, dedupe_key,
                    requested_refs, max_attempts, created_by
                )
                VALUES (
                    :id, :project_id, :repository_binding_id,
                    :trigger_signal_id, :repair_of_run_id, :source, 'queued',
                    :dedupe_key, CAST(:requested_refs AS JSONB),
                    :max_attempts, :created_by
                )
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
                "trigger_signal_id": trigger_signal_id,
                "repair_of_run_id": repair_of_run_id,
                "source": source,
                "dedupe_key": safe_dedupe,
                "requested_refs": json.dumps(safe_refs),
                "max_attempts": self.MAX_ATTEMPTS,
                "created_by": actor,
            },
        )
        job = enqueue_job(
            connection,
            project_id,
            "github.repository.reconcile",
            {
                "run_id": run_id,
                "repository_binding_id": repository_binding_id,
                "source": source,
                "data_boundary": "internal",
            },
            priority=40 if source == "webhook" else 80,
            max_attempts=self.MAX_ATTEMPTS,
            data_boundary="internal",
            dedupe_key=f"github-reconcile-job:{run_id}",
        )
        connection.execute(
            text(
                """
                UPDATE github_reconciliation_runs
                SET job_id = :job_id, updated_at = now()
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id, "job_id": job["id"]},
        )
        emit_event(
            connection,
            project_id,
            "github.reconciliation_scheduled",
            actor,
            "github_reconciliation_run",
            run_id,
            {
                "source": source,
                "requested_refs": safe_refs,
                "trigger_signal_id": trigger_signal_id,
                "repair_of_run_id": repair_of_run_id,
                "job_id": job["id"],
            },
        )
        return self.get_run(connection, project_id, run_id)

    def _create_or_get_signal(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        inbound_webhook_id: str,
        delivery_id: str,
        provider_event: str,
        action: Optional[str],
        ref_name: str,
        expected_commit_sha: Optional[str],
        expected_deleted: bool,
        payload_hash: Optional[str],
        provider_occurred_at: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        ref = GitHubBranchService._normalize_ref(ref_name)
        expected_sha = self._optional_sha(expected_commit_sha)
        expected_identity = (
            "deleted" if expected_deleted else (expected_sha or "unknown")
        )
        dedupe_key = (
            f"github-webhook:{repository_binding_id}:{delivery_id}:"
            f"{ref}:{expected_identity}"
        )
        existing = connection.execute(
            text(
                """
                SELECT id
                FROM github_reconciliation_signals
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).scalar_one_or_none()
        if existing:
            return self.get_signal(connection, project_id, str(existing))
        signal_id = make_id("ghsig")
        connection.execute(
            text(
                """
                INSERT INTO github_reconciliation_signals (
                    id, project_id, repository_binding_id, inbound_webhook_id,
                    source, delivery_id, provider_event, action, ref_name,
                    expected_commit_sha, expected_deleted, payload_hash,
                    dedupe_key, status, provider_occurred_at
                )
                VALUES (
                    :id, :project_id, :repository_binding_id,
                    :inbound_webhook_id, 'webhook', :delivery_id,
                    :provider_event, :action, :ref_name, :expected_commit_sha,
                    :expected_deleted, :payload_hash, :dedupe_key, 'pending',
                    :provider_occurred_at
                )
                """
            ),
            {
                "id": signal_id,
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
                "inbound_webhook_id": inbound_webhook_id,
                "delivery_id": delivery_id,
                "provider_event": provider_event,
                "action": action,
                "ref_name": ref,
                "expected_commit_sha": expected_sha,
                "expected_deleted": expected_deleted,
                "payload_hash": payload_hash,
                "dedupe_key": dedupe_key,
                "provider_occurred_at": self._parse_datetime(
                    provider_occurred_at
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "github.reconciliation_signal_ingested",
            actor,
            "github_reconciliation_signal",
            signal_id,
            {
                "delivery_id": delivery_id,
                "provider_event": provider_event,
                "ref_name": ref,
                "expected_commit_sha": expected_sha,
                "expected_deleted": expected_deleted,
            },
        )
        return self.get_signal(connection, project_id, signal_id)

    def _provider_read(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        binding: dict[str, Any],
        dedupe_key: str,
        effect_kind: str,
        resource_ref: str,
        payload: dict[str, Any],
        callback: Any,
    ) -> tuple[dict[str, Any], str]:
        scope = GitHubBranchService._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            effect_kind,
            {
                "repository_binding_id": binding["id"],
                "repository": binding["full_name"],
                "reconciliation_run_id": run_id,
                "resource_ref": resource_ref,
            },
            payload,
            data_boundary="internal",
        )
        self._append_receipt(connection, run_id, str(receipt["id"]))
        if not created:
            receipt_scope = receipt.get("effect_scope") or {}
            if (
                receipt.get("effect_kind") != effect_kind
                or receipt_scope.get("reconciliation_run_id") != run_id
                or receipt_scope.get("resource_ref") != resource_ref
            ):
                raise GitHubReconciliationStateError(
                    "Receipt dedupe key is bound to a different repository read."
                )
            if receipt["status"] == "succeeded":
                return dict(receipt.get("result") or {}), str(receipt["id"])
        try:
            result = dict(callback() or {})
        except Exception as error:
            safe_error = {
                "type": error.__class__.__name__,
                "message": "GitHub reconciliation provider read failed.",
            }
            self.receipts.mark_failed(connection, receipt["id"], safe_error)
            raise GitHubReconciliationProviderError(
                safe_error["message"]
            ) from error
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            f"{binding['full_name']}:{resource_ref}",
            result,
        )
        return result, str(receipt["id"])

    def _record_observation(
        self,
        connection: Connection,
        *,
        run: Any,
        ref_name: str,
        raw_observation: dict[str, Any],
        receipt_id: str,
    ) -> tuple[str, str]:
        observation = self._sanitize_branch_observation(
            ref_name, raw_observation
        )
        signal = None
        if run["trigger_signal_id"]:
            signal = connection.execute(
                text(
                    """
                    SELECT id, expected_commit_sha, expected_deleted
                    FROM github_reconciliation_signals
                    WHERE id = :signal_id
                    """
                ),
                {"signal_id": run["trigger_signal_id"]},
            ).mappings().first()
        previous = connection.execute(
            text(
                """
                SELECT exists, commit_sha, tree_sha
                FROM github_repository_ref_states
                WHERE repository_binding_id = :repository_binding_id
                  AND ref_name = :ref_name
                """
            ),
            {
                "repository_binding_id": run["repository_binding_id"],
                "ref_name": ref_name,
            },
        ).mappings().first()
        expected_sha = signal["expected_commit_sha"] if signal else None
        classification = self._classify_observation(
            observation=observation,
            previous=previous,
            expected_commit_sha=expected_sha,
            expected_deleted=bool(signal and signal["expected_deleted"]),
        )
        new_id = make_id("ghobs")
        observation_id = connection.execute(
            text(
                """
                INSERT INTO github_repository_observations (
                    id, project_id, repository_binding_id,
                    reconciliation_run_id, signal_id, ref_name,
                    expected_commit_sha, exists, commit_sha, tree_sha,
                    protected, protection, classification, receipt_id
                )
                VALUES (
                    :id, :project_id, :repository_binding_id, :run_id,
                    :signal_id, :ref_name, :expected_commit_sha, :exists,
                    :commit_sha, :tree_sha, :protected,
                    CAST(:protection AS JSONB), :classification, :receipt_id
                )
                ON CONFLICT (reconciliation_run_id, ref_name)
                DO UPDATE SET
                    expected_commit_sha = EXCLUDED.expected_commit_sha,
                    exists = EXCLUDED.exists,
                    commit_sha = EXCLUDED.commit_sha,
                    tree_sha = EXCLUDED.tree_sha,
                    protected = EXCLUDED.protected,
                    protection = EXCLUDED.protection,
                    classification = EXCLUDED.classification,
                    receipt_id = EXCLUDED.receipt_id,
                    observed_at = now()
                RETURNING id
                """
            ),
            {
                "id": new_id,
                "project_id": run["project_id"],
                "repository_binding_id": run["repository_binding_id"],
                "run_id": run["id"],
                "signal_id": signal["id"] if signal else None,
                "ref_name": ref_name,
                "expected_commit_sha": expected_sha,
                "exists": observation["exists"],
                "commit_sha": observation["commit_sha"],
                "tree_sha": observation["tree_sha"],
                "protected": observation["protected"],
                "protection": json.dumps(observation["protection"]),
                "classification": classification,
                "receipt_id": receipt_id,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO github_repository_ref_states (
                    id, project_id, repository_binding_id, ref_name, exists,
                    commit_sha, tree_sha, protected, protection,
                    latest_observation_id, latest_signal_id,
                    latest_receipt_id, observed_at
                )
                VALUES (
                    :id, :project_id, :repository_binding_id, :ref_name,
                    :exists, :commit_sha, :tree_sha, :protected,
                    CAST(:protection AS JSONB), :observation_id, :signal_id,
                    :receipt_id, now()
                )
                ON CONFLICT (repository_binding_id, ref_name)
                DO UPDATE SET
                    exists = EXCLUDED.exists,
                    commit_sha = EXCLUDED.commit_sha,
                    tree_sha = EXCLUDED.tree_sha,
                    protected = EXCLUDED.protected,
                    protection = EXCLUDED.protection,
                    latest_observation_id = EXCLUDED.latest_observation_id,
                    latest_signal_id = EXCLUDED.latest_signal_id,
                    latest_receipt_id = EXCLUDED.latest_receipt_id,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = now()
                """
            ),
            {
                "id": make_id("ghref"),
                "project_id": run["project_id"],
                "repository_binding_id": run["repository_binding_id"],
                "ref_name": ref_name,
                "exists": observation["exists"],
                "commit_sha": observation["commit_sha"],
                "tree_sha": observation["tree_sha"],
                "protected": observation["protected"],
                "protection": json.dumps(observation["protection"]),
                "observation_id": observation_id,
                "signal_id": signal["id"] if signal else None,
                "receipt_id": receipt_id,
            },
        )
        return str(observation_id), classification

    def _fail_execution(
        self,
        connection: Connection,
        *,
        run: Any,
        job: dict[str, Any],
        job_id: str,
        worker_id: str,
        lease_token: str,
        attempt: int,
        error: Exception,
    ) -> dict[str, Any]:
        rate_limited = (
            isinstance(error, GitHubReconciliationProviderError)
            and "rate limit" in str(error).lower()
        )
        retry_delay = (
            self._rate_limit_delay(run["id"], connection)
            if rate_limited
            else min(900, 30 * (2 ** max(0, attempt - 1)))
        )
        safe_error = {
            "type": "GitHubRateLimited" if rate_limited else error.__class__.__name__,
            "message": (
                "GitHub reconciliation is rate limited."
                if rate_limited
                else "GitHub reconciliation provider read failed."
            ),
            "attempt": attempt,
        }
        job_result = self.jobs.fail(
            connection,
            worker_id,
            job_id,
            lease_token,
            safe_error,
            retryable=True,
            retry_delay_seconds=retry_delay,
        )
        terminal = job_result["status"] == "deadlettered"
        connection.execute(
            text(
                """
                UPDATE github_reconciliation_runs
                SET status = :status, error = CAST(:error AS JSONB),
                    next_attempt_at = :next_attempt_at,
                    deadletter_id = :deadletter_id,
                    completed_at = CASE WHEN :terminal THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run["id"],
                "status": "deadlettered" if terminal else "failed",
                "error": json.dumps(safe_error),
                "next_attempt_at": job_result.get("run_after"),
                "deadletter_id": job_result.get("deadletter_id"),
                "terminal": terminal,
            },
        )
        if run["trigger_signal_id"]:
            connection.execute(
                text(
                    """
                    UPDATE github_reconciliation_signals
                    SET status = :status, error = CAST(:error AS JSONB),
                        processed_at = CASE WHEN :terminal THEN now() ELSE NULL END,
                        updated_at = now()
                    WHERE id = :signal_id
                    """
                ),
                {
                    "signal_id": run["trigger_signal_id"],
                    "status": "deadlettered" if terminal else "failed",
                    "error": json.dumps(safe_error),
                    "terminal": terminal,
                },
            )
        emit_event(
            connection,
            run["project_id"],
            (
                "github.reconciliation_deadlettered"
                if terminal
                else "github.reconciliation_retry_scheduled"
            ),
            worker_id,
            "github_reconciliation_run",
            run["id"],
            {
                "attempt": attempt,
                "error": safe_error,
                "next_attempt_at": job_result.get("run_after"),
                "deadletter_id": job_result.get("deadletter_id"),
            },
        )
        return {
            "run": self.get_run(connection, run["project_id"], run["id"]),
            "job": job_result,
        }

    def _invalidate_stale_work(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        actor: str,
    ) -> None:
        states = connection.execute(
            text(
                """
                SELECT ref_name, exists, commit_sha, tree_sha
                FROM github_repository_ref_states
                WHERE project_id = :project_id
                  AND repository_binding_id = :repository_binding_id
                """
            ),
            {
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
            },
        ).mappings().all()
        by_ref = {row["ref_name"]: row for row in states}
        authorizations = connection.execute(
            text(
                """
                SELECT id, source_ref, target_ref, head_commit_sha,
                       head_tree_sha, base_commit_sha, base_tree_sha
                FROM github_merge_authorizations
                WHERE project_id = :project_id
                  AND repository_binding_id = :repository_binding_id
                  AND status IN (
                    'pending_approval', 'authorized', 'queued', 'applying'
                  )
                """
            ),
            {
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
            },
        ).mappings().all()
        for item in authorizations:
            source = by_ref.get(item["source_ref"])
            target = by_ref.get(item["target_ref"])
            stale = (
                source
                and (
                    not source["exists"]
                    or source["commit_sha"] != item["head_commit_sha"]
                    or source["tree_sha"] != item["head_tree_sha"]
                )
            ) or (
                target
                and (
                    not target["exists"]
                    or target["commit_sha"] != item["base_commit_sha"]
                    or target["tree_sha"] != item["base_tree_sha"]
                )
            )
            if stale:
                self._mark_work_stale(
                    connection,
                    project_id=project_id,
                    table="github_merge_authorizations",
                    entity_type="github_merge_authorization",
                    entity_id=item["id"],
                    actor=actor,
                )
        sync_runs = connection.execute(
            text(
                """
                SELECT r.id, r.source_ref, r.target_ref,
                       s.source_commit_sha, s.source_tree_sha,
                       s.target_commit_sha, s.target_tree_sha
                FROM github_code_sync_runs r
                JOIN github_branch_snapshots s ON s.id = r.initial_snapshot_id
                WHERE r.project_id = :project_id
                  AND r.repository_binding_id = :repository_binding_id
                  AND r.status IN (
                    'planned', 'pending_approval', 'approved', 'applying',
                    'conflicted'
                  )
                """
            ),
            {
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
            },
        ).mappings().all()
        for item in sync_runs:
            source = by_ref.get(item["source_ref"])
            target = by_ref.get(item["target_ref"])
            stale = (
                source
                and (
                    source["commit_sha"] != item["source_commit_sha"]
                    or source["tree_sha"] != item["source_tree_sha"]
                )
            ) or (
                target
                and (
                    target["commit_sha"] != item["target_commit_sha"]
                    or target["tree_sha"] != item["target_tree_sha"]
                )
            )
            if stale:
                self._mark_work_stale(
                    connection,
                    project_id=project_id,
                    table="github_code_sync_runs",
                    entity_type="github_code_sync_run",
                    entity_id=item["id"],
                    actor=actor,
                )

    @staticmethod
    def _mark_work_stale(
        connection: Connection,
        *,
        project_id: str,
        table: str,
        entity_type: str,
        entity_id: str,
        actor: str,
    ) -> None:
        if table not in {
            "github_merge_authorizations",
            "github_code_sync_runs",
        }:
            raise ValueError("Unsupported reconciled work table.")
        error = {
            "type": "ReconciliationDrift",
            "message": "Fresh GitHub reconciliation invalidated pinned branch state.",
        }
        status = "blocked" if table == "github_merge_authorizations" else "failed"
        active_statuses = (
            "('pending_approval', 'authorized', 'queued', 'applying')"
            if table == "github_merge_authorizations"
            else (
                "('planned', 'pending_approval', 'approved', 'applying', "
                "'conflicted')"
            )
        )
        result = connection.execute(
            text(
                f"""
                UPDATE {table}
                SET status = :status, error = CAST(:error AS JSONB),
                    updated_at = now()
                WHERE id = :entity_id
                  AND status IN {active_statuses}
                """
            ),
            {
                "entity_id": entity_id,
                "status": status,
                "error": json.dumps(error),
            },
        )
        if result.rowcount != 1:
            return
        emit_event(
            connection,
            project_id,
            "github.reconciliation_drift_blocked",
            actor,
            entity_type,
            entity_id,
            error,
        )

    def _discover_refs(
        self,
        connection: Connection,
        *,
        repository_binding_id: str,
        default_branch: str,
    ) -> list[str]:
        refs = {GitHubBranchService._normalize_ref(default_branch)}
        rows = connection.execute(
            text(
                """
                SELECT ref_name
                FROM github_branch_leases
                WHERE repository_binding_id = :binding_id
                  AND status = 'active'
                UNION
                SELECT ref_name
                FROM github_repository_ref_states
                WHERE repository_binding_id = :binding_id
                UNION
                SELECT source_ref
                FROM github_code_sync_runs
                WHERE repository_binding_id = :binding_id
                  AND status IN (
                    'planned', 'pending_approval', 'approved', 'applying',
                    'conflicted'
                  )
                UNION
                SELECT target_ref
                FROM github_code_sync_runs
                WHERE repository_binding_id = :binding_id
                  AND status IN (
                    'planned', 'pending_approval', 'approved', 'applying',
                    'conflicted'
                  )
                UNION
                SELECT source_ref
                FROM github_merge_authorizations
                WHERE repository_binding_id = :binding_id
                  AND status IN (
                    'pending_approval', 'authorized', 'queued', 'applying'
                  )
                UNION
                SELECT target_ref
                FROM github_merge_authorizations
                WHERE repository_binding_id = :binding_id
                  AND status IN (
                    'pending_approval', 'authorized', 'queued', 'applying'
                  )
                """
            ),
            {"binding_id": repository_binding_id},
        ).scalars().all()
        for value in rows:
            if value:
                refs.add(GitHubBranchService._normalize_ref(value))
        return sorted(refs)[: self.MAX_REFS]

    @classmethod
    def _refs_from_payload(
        cls,
        provider_event: str,
        payload: dict[str, Any],
        *,
        default_branch: str,
        repository_full_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        occurred_at = (
            ((payload.get("head_commit") or {}).get("timestamp"))
            or ((payload.get("pull_request") or {}).get("updated_at"))
            or ((payload.get("check_suite") or {}).get("updated_at"))
            or ((payload.get("check_run") or {}).get("completed_at"))
        )
        if provider_event == "push":
            ref = cls._branch_ref(payload.get("ref"))
            after = str(payload.get("after") or "")
            if ref:
                refs.append(
                    {
                        "ref_name": ref,
                        "expected_commit_sha": (
                            None if cls._ZERO_SHA.fullmatch(after) else after
                        ),
                        "expected_deleted": bool(
                            payload.get("deleted")
                            or cls._ZERO_SHA.fullmatch(after)
                        ),
                        "provider_occurred_at": occurred_at,
                    }
                )
        elif provider_event in {"create", "delete"}:
            if payload.get("ref_type") == "branch" and payload.get("ref"):
                refs.append(
                    {
                        "ref_name": str(payload["ref"]),
                        "expected_commit_sha": None,
                        "expected_deleted": provider_event == "delete",
                        "provider_occurred_at": occurred_at,
                    }
                )
        elif provider_event in {"pull_request", "pull_request_review"}:
            pull = payload.get("pull_request") or {}
            for side in ("head", "base"):
                item = pull.get(side) or {}
                item_repository = str(
                    ((item.get("repo") or {}).get("full_name")) or ""
                )
                if (
                    repository_full_name
                    and item_repository
                    and item_repository.casefold()
                    != repository_full_name.casefold()
                ):
                    continue
                if item.get("ref"):
                    refs.append(
                        {
                            "ref_name": item["ref"],
                            "expected_commit_sha": item.get("sha"),
                            "expected_deleted": False,
                            "provider_occurred_at": occurred_at,
                        }
                    )
        elif provider_event in {"check_run", "check_suite"}:
            suite = (
                (payload.get("check_run") or {}).get("check_suite")
                or payload.get("check_suite")
                or {}
            )
            head_repository = str(
                ((suite.get("head_repository") or {}).get("full_name")) or ""
            )
            same_repository = not (
                repository_full_name
                and head_repository
                and head_repository.casefold()
                != repository_full_name.casefold()
            )
            if suite.get("head_branch") and same_repository:
                refs.append(
                    {
                        "ref_name": suite["head_branch"],
                        "expected_commit_sha": suite.get("head_sha"),
                        "expected_deleted": False,
                        "provider_occurred_at": occurred_at,
                    }
                )
        elif provider_event == "status":
            for branch in payload.get("branches") or []:
                if isinstance(branch, dict) and branch.get("name"):
                    refs.append(
                        {
                            "ref_name": branch["name"],
                            "expected_commit_sha": payload.get("sha"),
                            "expected_deleted": False,
                            "provider_occurred_at": occurred_at,
                        }
                    )
        if not refs:
            refs.append(
                {
                    "ref_name": default_branch,
                    "expected_commit_sha": None,
                    "expected_deleted": False,
                    "provider_occurred_at": occurred_at,
                }
            )
        unique: dict[str, dict[str, Any]] = {}
        for item in refs:
            try:
                ref_name = GitHubBranchService._normalize_ref(item["ref_name"])
            except ValueError:
                continue
            unique[ref_name] = {**item, "ref_name": ref_name}
        return list(unique.values())[: cls.MAX_REFS]

    @staticmethod
    def _branch_ref(value: Any) -> Optional[str]:
        raw = str(value or "")
        prefix = "refs/heads/"
        if not raw.startswith(prefix):
            return None
        return raw[len(prefix) :]

    @classmethod
    def _classify_observation(
        cls,
        *,
        observation: dict[str, Any],
        previous: Optional[Any],
        expected_commit_sha: Optional[str],
        expected_deleted: bool,
    ) -> str:
        if expected_deleted:
            return (
                "deleted"
                if not observation["exists"]
                else "unexpected_reappearance"
            )
        if expected_commit_sha:
            return (
                "current"
                if observation["exists"]
                and observation["commit_sha"] == expected_commit_sha
                else "stale_signal"
            )
        if not observation["exists"]:
            return "missing"
        if previous is None:
            return "current"
        if not previous["exists"]:
            return "advanced"
        if (
            previous["commit_sha"] == observation["commit_sha"]
            and previous["tree_sha"] == observation["tree_sha"]
        ):
            return "current"
        return "changed"

    @classmethod
    def _sanitize_branch_observation(
        cls, ref_name: str, value: dict[str, Any]
    ) -> dict[str, Any]:
        exists = bool(value.get("exists", False))
        commit_sha = str(value.get("commit_sha") or "").lower() or None
        tree_sha = str(value.get("tree_sha") or "").lower() or None
        if exists and (
            not commit_sha
            or not tree_sha
            or not cls._SHA.fullmatch(commit_sha)
            or not cls._SHA.fullmatch(tree_sha)
        ):
            raise GitHubReconciliationProviderError(
                f"GitHub returned an invalid identity for {ref_name}."
            )
        return {
            "exists": exists,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "protected": bool(value.get("protected", False)),
            "protection": dict(value.get("protection") or {}),
        }

    @staticmethod
    def _sanitize_rate_limit(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "resource": str(value.get("resource") or "core"),
            "limit": max(0, int(value.get("limit") or 0)),
            "remaining": max(0, int(value.get("remaining") or 0)),
            "used": max(0, int(value.get("used") or 0)),
            "reset_epoch": max(0, int(value.get("reset_epoch") or 0)),
            "observed_at": now_utc().isoformat(),
        }

    @staticmethod
    def _rate_limit_delay(run_id: str, connection: Connection) -> int:
        rate = connection.execute(
            text(
                """
                SELECT rate_limit_status
                FROM github_reconciliation_runs
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id},
        ).scalar_one_or_none() or {}
        reset_epoch = int(rate.get("reset_epoch") or 0)
        if reset_epoch <= 0:
            return 300
        remaining = reset_epoch - int(now_utc().timestamp()) + 5
        return min(3600, max(30, remaining))

    @classmethod
    def _normalize_refs(cls, values: Optional[list[str]]) -> list[str]:
        normalized = sorted(
            {
                GitHubBranchService._normalize_ref(value)
                for value in (values or [])
            }
        )
        if not normalized or len(normalized) > cls.MAX_REFS:
            raise ValueError(
                f"refs must contain between 1 and {cls.MAX_REFS} unique branches."
            )
        return normalized

    @classmethod
    def _optional_sha(cls, value: Any) -> Optional[str]:
        normalized = str(value or "").strip().lower()
        if not normalized or cls._ZERO_SHA.fullmatch(normalized):
            return None
        if not cls._SHA.fullmatch(normalized):
            return None
        return normalized

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _append_receipt(
        connection: Connection, run_id: str, receipt_id: str
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE github_reconciliation_runs
                SET receipt_ids = CASE
                      WHEN receipt_ids ? :receipt_id THEN receipt_ids
                      ELSE receipt_ids || CAST(:receipt AS JSONB)
                    END,
                    updated_at = now()
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "receipt_id": receipt_id,
                "receipt": json.dumps([receipt_id]),
            },
        )

    def _locked_run(
        self, connection: Connection, project_id: str, run_id: str
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._run_columns()}
                FROM github_reconciliation_runs
                WHERE project_id = :project_id AND id = :run_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "run_id": run_id},
        ).mappings().first()
        if row is None:
            raise GitHubReconciliationNotFoundError(
                "GitHub reconciliation run not found."
            )
        return row

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty.")
        return normalized

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")

    @staticmethod
    def _run_columns() -> str:
        return """
            id, project_id, repository_binding_id, trigger_signal_id,
            repair_of_run_id, source, status, dedupe_key, requested_refs,
            attempt_count, max_attempts, job_id, observation_ids, receipt_ids,
            rate_limit_status, next_attempt_at, deadletter_id, error,
            created_by, started_at, completed_at, created_at, updated_at
        """

    @staticmethod
    def _signal_columns() -> str:
        return """
            id, project_id, repository_binding_id, inbound_webhook_id, source,
            delivery_id, provider_event, action, ref_name,
            expected_commit_sha, expected_deleted, payload_hash, dedupe_key,
            status, reconciliation_run_id, observed_commit_sha, error,
            provider_occurred_at, processed_at, created_at, updated_at
        """

    @staticmethod
    def _ref_state_columns() -> str:
        return """
            id, project_id, repository_binding_id, ref_name, exists,
            commit_sha, tree_sha, protected, protection,
            latest_observation_id, latest_signal_id, latest_receipt_id,
            observed_at, created_at, updated_at
        """

    @staticmethod
    def _serialize_run(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "trigger_signal_id": row["trigger_signal_id"],
            "repair_of_run_id": row["repair_of_run_id"],
            "source": row["source"],
            "status": row["status"],
            "dedupe_key": row["dedupe_key"],
            "requested_refs": list(row["requested_refs"] or []),
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "job_id": row["job_id"],
            "observation_ids": list(row["observation_ids"] or []),
            "receipt_ids": list(row["receipt_ids"] or []),
            "rate_limit_status": row["rate_limit_status"] or {},
            "next_attempt_at": (
                row["next_attempt_at"].isoformat()
                if row["next_attempt_at"]
                else None
            ),
            "deadletter_id": row["deadletter_id"],
            "error": row["error"],
            "created_by": row["created_by"],
            "started_at": (
                row["started_at"].isoformat() if row["started_at"] else None
            ),
            "completed_at": (
                row["completed_at"].isoformat() if row["completed_at"] else None
            ),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _serialize_signal(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "inbound_webhook_id": row["inbound_webhook_id"],
            "source": row["source"],
            "delivery_id": row["delivery_id"],
            "provider_event": row["provider_event"],
            "action": row["action"],
            "ref_name": row["ref_name"],
            "expected_commit_sha": row["expected_commit_sha"],
            "expected_deleted": bool(row["expected_deleted"]),
            "payload_hash": row["payload_hash"],
            "dedupe_key": row["dedupe_key"],
            "status": row["status"],
            "reconciliation_run_id": row["reconciliation_run_id"],
            "observed_commit_sha": row["observed_commit_sha"],
            "error": row["error"],
            "provider_occurred_at": (
                row["provider_occurred_at"].isoformat()
                if row["provider_occurred_at"]
                else None
            ),
            "processed_at": (
                row["processed_at"].isoformat() if row["processed_at"] else None
            ),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _serialize_ref_state(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "ref_name": row["ref_name"],
            "exists": bool(row["exists"]),
            "commit_sha": row["commit_sha"],
            "tree_sha": row["tree_sha"],
            "protected": bool(row["protected"]),
            "protection": row["protection"] or {},
            "latest_observation_id": row["latest_observation_id"],
            "latest_signal_id": row["latest_signal_id"],
            "latest_receipt_id": row["latest_receipt_id"],
            "observed_at": row["observed_at"].isoformat(),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
