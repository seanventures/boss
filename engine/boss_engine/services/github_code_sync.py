from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, json_hash, make_id
from .github_adapter import GitHubMergeConflictError
from .github_branch_leases import GitHubBranchLeaseService
from .github_branches import GitHubBranchService
from .github_connections import GitHubConnectionService
from .receipts import ReceiptService


class GitHubCodeSyncNotFoundError(RuntimeError):
    pass


class GitHubConflictSetNotFoundError(RuntimeError):
    pass


class GitHubResolutionPlanNotFoundError(RuntimeError):
    pass


class GitHubCodeSyncStateError(RuntimeError):
    pass


class GitHubCodeSyncStaleError(RuntimeError):
    pass


class GitHubCodeSyncService:
    """Policy-gated, fenced and receipted GitHub branch synchronization."""

    MAX_APPLY_ATTEMPTS = 3
    _SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")

    def __init__(
        self,
        *,
        github_connections: GitHubConnectionService,
        github_branches: GitHubBranchService,
        github_branch_leases: GitHubBranchLeaseService,
    ) -> None:
        self.github_connections = github_connections
        self.github_branches = github_branches
        self.github_branch_leases = github_branch_leases
        self.receipts = ReceiptService()

    def plan(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        snapshot_id: str,
        dedupe_key: str,
        resolution_branch: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        safe_dedupe_key = self._required_text(dedupe_key, "dedupe_key")
        replay = connection.execute(
            text(
                """
                SELECT id
                FROM github_code_sync_runs
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": safe_dedupe_key},
        ).scalar_one_or_none()
        if replay:
            item = self.get(connection, project_id, str(replay))
            if (
                item["repository_binding_id"] != repository_binding_id
                or item["initial_snapshot_id"] != snapshot_id
            ):
                raise GitHubCodeSyncStateError(
                    "dedupe_key is already bound to a different code sync plan."
                )
            return item

        snapshot = self.github_branches.get_snapshot(
            connection, project_id, snapshot_id
        )
        if snapshot["repository_binding_id"] != repository_binding_id:
            raise GitHubCodeSyncStateError(
                "Snapshot does not belong to the requested repository binding."
            )
        policy = self.github_branches.get_policy(
            connection, project_id, snapshot["branch_policy_id"]
        )
        topology = snapshot["topology_classification"]
        source_ref = snapshot["source_ref"]
        target_ref = snapshot["target_ref"]
        action, status, evaluation = self._planned_action(
            policy=policy,
            snapshot=snapshot,
        )
        safe_resolution_branch: Optional[str] = None
        if action == "create_resolution_branch":
            safe_resolution_branch = (
                GitHubBranchService._normalize_ref(resolution_branch)
                if resolution_branch
                else self._default_resolution_branch(snapshot)
            )
            create_evaluation = self.github_branches.evaluate_policy(
                policy,
                ref=safe_resolution_branch,
                operation="create",
                method=None,
            )
            merge_evaluation = self.github_branches.evaluate_policy(
                policy,
                ref=safe_resolution_branch,
                operation="merge",
                method="merge",
            )
            statuses = {
                create_evaluation["status"],
                merge_evaluation["status"],
            }
            reasons = sorted(
                set(create_evaluation["reasons"])
                | set(merge_evaluation["reasons"])
            )
            if policy["require_linear_history"]:
                statuses.add("blocked")
                reasons.append("linear_history_denies_resolution_merge")
            if policy["require_signed_commits"]:
                statuses.add("blocked")
                reasons.append("signed_commit_unavailable")
            combined_status = (
                "blocked"
                if "blocked" in statuses
                else (
                    "approval_required"
                    if "approval_required" in statuses
                    else "allowed"
                )
            )
            evaluation = {
                **create_evaluation,
                "status": combined_status,
                "reasons": sorted(set(reasons)),
                "resolution_branch": safe_resolution_branch,
                "create_evaluation": create_evaluation,
                "merge_evaluation": merge_evaluation,
            }
            status = self._status_from_policy(evaluation)

        sync_run_id = make_id("ghsync")
        changeset_id = None
        if status not in {"noop", "blocked"}:
            changeset_id = self._create_changeset(
                connection,
                project_id=project_id,
                sync_run_id=sync_run_id,
                action=action,
                source_ref=source_ref,
                target_ref=target_ref,
                resolution_branch=safe_resolution_branch,
                source_sha=snapshot["source_commit_sha"],
                target_sha=snapshot["target_commit_sha"],
                policy_evaluation=evaluation,
                status=status,
                actor=actor,
            )
        rollback_ref, rollback_sha = self._rollback_identity(
            action=action,
            snapshot=snapshot,
            resolution_branch=safe_resolution_branch,
        )
        connection.execute(
            text(
                """
                INSERT INTO github_code_sync_runs (
                    id, project_id, repository_binding_id, branch_policy_id,
                    branch_policy_version, initial_snapshot_id, source_ref,
                    target_ref, topology_classification, action, status,
                    dedupe_key, changeset_id, resolution_branch, rollback_ref,
                    rollback_sha, created_by
                )
                VALUES (
                    :id, :project_id, :repository_binding_id, :branch_policy_id,
                    :branch_policy_version, :initial_snapshot_id, :source_ref,
                    :target_ref, :topology_classification, :action, :status,
                    :dedupe_key, :changeset_id, :resolution_branch,
                    :rollback_ref, :rollback_sha, :created_by
                )
                """
            ),
            {
                "id": sync_run_id,
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
                "branch_policy_id": policy["id"],
                "branch_policy_version": policy["version"],
                "initial_snapshot_id": snapshot_id,
                "source_ref": source_ref,
                "target_ref": target_ref,
                "topology_classification": topology,
                "action": action,
                "status": status,
                "dedupe_key": safe_dedupe_key,
                "changeset_id": changeset_id,
                "resolution_branch": safe_resolution_branch,
                "rollback_ref": rollback_ref,
                "rollback_sha": rollback_sha,
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.code_sync_planned",
            actor,
            "github_code_sync_run",
            sync_run_id,
            {
                "repository_binding_id": repository_binding_id,
                "snapshot_id": snapshot_id,
                "action": action,
                "status": status,
                "changeset_id": changeset_id,
                "policy_evaluation": evaluation,
            },
        )
        return self.get(connection, project_id, sync_run_id)

    def apply(
        self,
        connection: Connection,
        *,
        project_id: str,
        sync_run_id: str,
        lease_id: str,
        lease_token: str,
        fencing_token: int,
        actor: str,
    ) -> dict[str, Any]:
        run = self._locked_run(connection, project_id, sync_run_id)
        if run["status"] in {"succeeded", "noop", "conflicted"}:
            return self._serialize_run(run)
        if run["status"] not in {"planned", "approved", "failed"}:
            raise GitHubCodeSyncStateError(
                f"Code sync run is {run['status']} and cannot be applied."
            )
        if int(run["apply_attempts"]) >= self.MAX_APPLY_ATTEMPTS:
            raise GitHubCodeSyncStateError(
                "Code sync run exhausted its apply attempts."
            )
        mutable_ref = self._mutable_ref(run)
        lease = self.github_branch_leases.verify(
            connection,
            project_id=project_id,
            lease_id=lease_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            repository_binding_id=run["repository_binding_id"],
            ref_name=mutable_ref,
        )
        self._assert_active_policy(connection, run)
        attempt = int(run["apply_attempts"]) + 1
        snapshot_result = self.github_branches.capture_snapshot(
            connection,
            project_id=project_id,
            repository_binding_id=run["repository_binding_id"],
            source_ref=run["source_ref"],
            target_ref=run["target_ref"],
            dedupe_key=f"github-sync-preflight:{run['id']}:{attempt}",
            actor=actor,
        )
        if snapshot_result["status"] != "succeeded":
            return self._record_run_failure(
                connection,
                run=run,
                attempt=attempt,
                error_type="SnapshotFailed",
                actor=actor,
            )
        preflight = snapshot_result["snapshot"]
        initial = self.github_branches.get_snapshot(
            connection, project_id, run["initial_snapshot_id"]
        )
        try:
            self._assert_snapshot_identity(initial, preflight)
        except GitHubCodeSyncStaleError:
            return self._record_run_failure(
                connection,
                run=run,
                attempt=attempt,
                error_type="StaleRemoteTopology",
                actor=actor,
            )

        connection.execute(
            text(
                """
                UPDATE github_code_sync_runs
                SET status = 'applying', branch_lease_id = :lease_id,
                    fencing_token = :fencing_token, apply_attempts = :attempt,
                    applied_by = :actor, error = NULL, updated_at = now()
                WHERE id = :sync_run_id
                """
            ),
            {
                "lease_id": lease["id"],
                "fencing_token": lease["fencing_token"],
                "attempt": attempt,
                "actor": actor,
                "sync_run_id": run["id"],
            },
        )
        adapter, _, binding = self.github_connections.adapter_for_binding(
            connection,
            project_id=project_id,
            binding_id=run["repository_binding_id"],
        )
        try:
            if run["action"] in {
                "fast_forward_target",
                "fast_forward_source",
            }:
                result = self._apply_fast_forward(
                    connection,
                    run=run,
                    initial=initial,
                    adapter=adapter,
                    binding=binding,
                    attempt=attempt,
                    actor=actor,
                )
            elif run["action"] == "create_resolution_branch":
                result = self._apply_resolution_branch(
                    connection,
                    run=run,
                    initial=initial,
                    adapter=adapter,
                    binding=binding,
                    attempt=attempt,
                    actor=actor,
                )
            else:
                raise GitHubCodeSyncStateError(
                    f"Code sync action {run['action']} is not applicable."
                )
        except GitHubMergeConflictError:
            return self._record_conflict(
                connection,
                run=run,
                initial=initial,
                adapter=adapter,
                attempt=attempt,
                actor=actor,
            )
        except Exception as error:
            return self._record_run_failure(
                connection,
                run=run,
                attempt=attempt,
                error_type=error.__class__.__name__,
                actor=actor,
            )

        result_source = (
            run["resolution_branch"]
            if run["action"] == "create_resolution_branch"
            else run["source_ref"]
        )
        result_snapshot = self.github_branches.capture_snapshot(
            connection,
            project_id=project_id,
            repository_binding_id=run["repository_binding_id"],
            source_ref=result_source,
            target_ref=run["target_ref"],
            dedupe_key=f"github-sync-result:{run['id']}:{attempt}",
            actor=actor,
        )
        result_snapshot_id = (
            result_snapshot["snapshot"]["id"]
            if result_snapshot["status"] == "succeeded"
            else None
        )
        if result_snapshot_id is None:
            return self._record_run_failure(
                connection,
                run=run,
                attempt=attempt,
                error_type="ResultVerificationFailed",
                actor=actor,
            )
        self._finish_changeset(
            connection,
            changeset_id=run["changeset_id"],
            status="applied",
            actor=actor,
        )
        connection.execute(
            text(
                """
                UPDATE github_code_sync_runs
                SET status = 'succeeded', result_snapshot_id = :snapshot_id,
                    completed_at = now(), updated_at = now()
                WHERE id = :sync_run_id
                """
            ),
            {"snapshot_id": result_snapshot_id, "sync_run_id": run["id"]},
        )
        emit_event(
            connection,
            project_id,
            "github.code_sync_succeeded",
            actor,
            "github_code_sync_run",
            run["id"],
            {
                "action": run["action"],
                "attempt": attempt,
                "result": result,
                "result_snapshot_id": result_snapshot_id,
            },
        )
        return self.get(connection, project_id, run["id"])

    def propose_resolution(
        self,
        connection: Connection,
        *,
        project_id: str,
        conflict_set_id: str,
        resolved_files: list[dict[str, Any]],
        validation_evidence: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        conflict = self._locked_conflict(
            connection, project_id, conflict_set_id
        )
        if conflict["status"] not in {"open", "resolution_proposed"}:
            raise GitHubCodeSyncStateError(
                f"Conflict set is {conflict['status']} and cannot accept a proposal."
            )
        files = self._validate_resolved_files(
            resolved_files,
            conflict_files=conflict["files"] or [],
        )
        evidence = self._validate_red_green_evidence(validation_evidence)
        version = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM github_resolution_plans
                    WHERE conflict_set_id = :conflict_set_id
                    """
                ),
                {"conflict_set_id": conflict_set_id},
            ).scalar_one()
        )
        plan_id = make_id("ghresolve")
        changeset_id = self._create_resolution_changeset(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            conflict=conflict,
            resolved_files=files,
            actor=actor,
        )
        connection.execute(
            text(
                """
                INSERT INTO github_resolution_plans (
                    id, project_id, conflict_set_id, version, status,
                    resolved_files, validation_evidence, changeset_id, created_by
                )
                VALUES (
                    :id, :project_id, :conflict_set_id, :version, 'proposed',
                    CAST(:resolved_files AS JSONB),
                    CAST(:validation_evidence AS JSONB),
                    :changeset_id, :created_by
                )
                """
            ),
            {
                "id": plan_id,
                "project_id": project_id,
                "conflict_set_id": conflict_set_id,
                "version": version,
                "resolved_files": json.dumps(files),
                "validation_evidence": json.dumps(evidence),
                "changeset_id": changeset_id,
                "created_by": actor,
            },
        )
        connection.execute(
            text(
                """
                UPDATE github_conflict_sets
                SET status = 'resolution_proposed', updated_at = now()
                WHERE id = :conflict_set_id
                """
            ),
            {"conflict_set_id": conflict_set_id},
        )
        emit_event(
            connection,
            project_id,
            "github.conflict_resolution_proposed",
            actor,
            "github_resolution_plan",
            plan_id,
            {
                "conflict_set_id": conflict_set_id,
                "version": version,
                "changeset_id": changeset_id,
                "resolved_paths": [item["path"] for item in files],
                "red_green_proof": True,
            },
        )
        return self.get_resolution_plan(connection, project_id, plan_id)

    def approve_resolution(
        self,
        connection: Connection,
        *,
        project_id: str,
        plan_id: str,
        review_evidence: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        plan = self._locked_resolution_plan(connection, project_id, plan_id)
        if plan["status"] != "proposed":
            raise GitHubCodeSyncStateError(
                f"Resolution plan is {plan['status']} and cannot be approved."
            )
        if plan["created_by"] == actor:
            raise GitHubCodeSyncStateError(
                "Resolution plans require an independent reviewer."
            )
        review = dict(review_evidence or {})
        if str(review.get("verdict") or "").lower() != "approved":
            raise ValueError("review_evidence.verdict must be approved.")
        if not str(review.get("summary") or "").strip():
            raise ValueError("review_evidence.summary must be non-empty.")
        review["reviewer"] = actor
        connection.execute(
            text(
                """
                UPDATE github_resolution_plans
                SET status = 'approved', independent_review = CAST(:review AS JSONB),
                    approved_by = :actor, updated_at = now()
                WHERE id = :plan_id
                """
            ),
            {
                "plan_id": plan_id,
                "review": json.dumps(review),
                "actor": actor,
            },
        )
        connection.execute(
            text(
                """
                UPDATE github_conflict_sets
                SET status = 'approved', updated_at = now()
                WHERE id = :conflict_set_id
                """
            ),
            {"conflict_set_id": plan["conflict_set_id"]},
        )
        self._finish_changeset(
            connection,
            changeset_id=plan["changeset_id"],
            status="approved",
            actor=actor,
        )
        emit_event(
            connection,
            project_id,
            "github.conflict_resolution_approved",
            actor,
            "github_resolution_plan",
            plan_id,
            {
                "conflict_set_id": plan["conflict_set_id"],
                "independent_reviewer": True,
            },
        )
        return self.get_resolution_plan(connection, project_id, plan_id)

    def apply_resolution(
        self,
        connection: Connection,
        *,
        project_id: str,
        plan_id: str,
        lease_id: str,
        lease_token: str,
        fencing_token: int,
        actor: str,
    ) -> dict[str, Any]:
        plan = self._locked_resolution_plan(connection, project_id, plan_id)
        if plan["status"] == "applied":
            return self._resolution_apply_response(
                connection, project_id, plan_id
            )
        if plan["status"] != "approved":
            raise GitHubCodeSyncStateError(
                f"Resolution plan is {plan['status']} and cannot be applied."
            )
        conflict = self._locked_conflict(
            connection, project_id, plan["conflict_set_id"]
        )
        run = self._locked_run(connection, project_id, conflict["code_sync_run_id"])
        self._assert_active_policy(connection, run)
        policy = self.github_branches.get_policy(
            connection, project_id, run["branch_policy_id"]
        )
        if policy["require_linear_history"]:
            raise GitHubCodeSyncStateError(
                "Resolution merge commits are blocked by linear-history policy."
            )
        if policy["require_signed_commits"]:
            raise GitHubCodeSyncStateError(
                "Resolution commit signing is required but unavailable."
            )
        update_evaluation = self.github_branches.evaluate_policy(
            policy,
            ref=conflict["resolution_branch"],
            operation="update",
            method="fast_forward",
        )
        if update_evaluation["status"] == "blocked":
            raise GitHubCodeSyncStateError(
                "Branch policy blocks the resolution ref update: "
                + ", ".join(update_evaluation["reasons"])
            )
        self.github_branch_leases.verify(
            connection,
            project_id=project_id,
            lease_id=lease_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            repository_binding_id=run["repository_binding_id"],
            ref_name=conflict["resolution_branch"],
        )
        adapter, _, binding = self.github_connections.adapter_for_binding(
            connection,
            project_id=project_id,
            binding_id=run["repository_binding_id"],
        )
        resolution_head = adapter.branch_snapshot(conflict["resolution_branch"])
        target_head = adapter.branch_snapshot(run["target_ref"])
        if (
            not resolution_head["exists"]
            or resolution_head["commit_sha"]
            != conflict["resolution_branch_head_sha"]
            or resolution_head["tree_sha"] != conflict["ours_tree_sha"]
            or not target_head["exists"]
            or target_head["commit_sha"] != conflict["theirs_commit_sha"]
        ):
            raise GitHubCodeSyncStaleError(
                "Resolution inputs changed; capture fresh evidence and re-plan."
            )
        try:
            commit_result = self._perform_provider_action(
                connection,
                project_id=project_id,
                changeset_id=plan["changeset_id"],
                operation_index=0,
                binding=binding,
                dedupe_key=f"github-resolution:{plan_id}:commit",
                action="create_resolution_commit",
                resource_ref=conflict["resolution_branch"],
                payload={
                    "base_tree_sha": conflict["ours_tree_sha"],
                    "parent_shas": [
                        conflict["resolution_branch_head_sha"],
                        conflict["theirs_commit_sha"],
                    ],
                    "resolved_paths": [
                        item["path"] for item in (plan["resolved_files"] or [])
                    ],
                },
                actor=actor,
                callback=lambda: adapter.create_resolution_commit(
                    base_tree_sha=conflict["ours_tree_sha"],
                    parent_shas=[
                        conflict["resolution_branch_head_sha"],
                        conflict["theirs_commit_sha"],
                    ],
                    resolved_files=plan["resolved_files"] or [],
                    message=(
                        "Resolve conflicts for "
                        f"{run['source_ref']} into {run['target_ref']}"
                    ),
                ),
            )
            connection.execute(
                text(
                    """
                    UPDATE github_resolution_plans
                    SET result_commit_sha = :result_commit_sha, updated_at = now()
                    WHERE id = :plan_id
                    """
                ),
                {
                    "result_commit_sha": commit_result["commit_sha"],
                    "plan_id": plan_id,
                },
            )
            self._perform_provider_action(
                connection,
                project_id=project_id,
                changeset_id=plan["changeset_id"],
                operation_index=1,
                binding=binding,
                dedupe_key=f"github-resolution:{plan_id}:update-ref",
                action="fast_forward_ref",
                resource_ref=conflict["resolution_branch"],
                payload={
                    "expected_sha": conflict["resolution_branch_head_sha"],
                    "new_sha": commit_result["commit_sha"],
                    "force": False,
                },
                actor=actor,
                callback=lambda: adapter.fast_forward_ref(
                    conflict["resolution_branch"],
                    expected_sha=conflict["resolution_branch_head_sha"],
                    new_sha=commit_result["commit_sha"],
                ),
            )
        except Exception as error:
            connection.execute(
                text(
                    """
                    UPDATE github_resolution_plans
                    SET status = 'failed', updated_at = now()
                    WHERE id = :plan_id
                    """
                ),
                {"plan_id": plan_id},
            )
            self._finish_changeset(
                connection,
                changeset_id=plan["changeset_id"],
                status="deadlettered",
                actor=actor,
                failure_reason="GitHub conflict resolution provider action failed.",
            )
            self._create_deadletter(
                connection,
                project_id=project_id,
                source_kind="github_resolution_plan",
                source_id=plan_id,
                reason="GitHub conflict resolution provider action failed.",
                payload={"error_type": error.__class__.__name__},
            )
            emit_event(
                connection,
                project_id,
                "github.conflict_resolution_failed",
                actor,
                "github_resolution_plan",
                plan_id,
                {"error_type": error.__class__.__name__},
            )
            return self._resolution_apply_response(
                connection, project_id, plan_id
            )

        connection.execute(
            text(
                """
                UPDATE github_resolution_plans
                SET status = 'applied', applied_by = :actor,
                    applied_at = now(), updated_at = now()
                WHERE id = :plan_id
                """
            ),
            {"plan_id": plan_id, "actor": actor},
        )
        connection.execute(
            text(
                """
                UPDATE github_conflict_sets
                SET status = 'resolved', resolved_by_plan_id = :plan_id,
                    resolution_branch_head_sha = :result_commit_sha,
                    resolved_at = now(), updated_at = now()
                WHERE id = :conflict_set_id
                """
            ),
            {
                "plan_id": plan_id,
                "result_commit_sha": commit_result["commit_sha"],
                "conflict_set_id": conflict["id"],
            },
        )
        connection.execute(
            text(
                """
                UPDATE github_code_sync_runs
                SET status = 'succeeded', completed_at = now(), updated_at = now()
                WHERE id = :sync_run_id
                """
            ),
            {"sync_run_id": run["id"]},
        )
        self._finish_changeset(
            connection,
            changeset_id=plan["changeset_id"],
            status="applied",
            actor=actor,
        )
        emit_event(
            connection,
            project_id,
            "github.conflict_resolution_applied",
            actor,
            "github_resolution_plan",
            plan_id,
            {
                "conflict_set_id": conflict["id"],
                "sync_run_id": run["id"],
                "resolution_branch": conflict["resolution_branch"],
                "result_commit_sha": commit_result["commit_sha"],
                "target_branch_unchanged": True,
            },
        )
        return self._resolution_apply_response(connection, project_id, plan_id)

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
            params["status"] = status
        ids = connection.execute(
            text(
                f"""
                SELECT id
                FROM github_code_sync_runs
                WHERE {" AND ".join(filters)}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        ).scalars().all()
        return [self.get(connection, project_id, str(item)) for item in ids]

    def get(
        self,
        connection: Connection,
        project_id: str,
        sync_run_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT {self._run_columns()}
                FROM github_code_sync_runs
                WHERE project_id = :project_id AND id = :sync_run_id
                """
            ),
            {"project_id": project_id, "sync_run_id": sync_run_id},
        ).mappings().first()
        if row is None:
            raise GitHubCodeSyncNotFoundError("GitHub code sync run not found.")
        return self._serialize_run(row)

    def list_conflicts(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._validate_limit(limit)
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if status:
            filters.append("status = :status")
            params["status"] = status
        ids = connection.execute(
            text(
                f"""
                SELECT id
                FROM github_conflict_sets
                WHERE {" AND ".join(filters)}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        ).scalars().all()
        return [
            self.get_conflict(connection, project_id, str(item)) for item in ids
        ]

    def get_conflict(
        self,
        connection: Connection,
        project_id: str,
        conflict_set_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT {self._conflict_columns()}
                FROM github_conflict_sets
                WHERE project_id = :project_id AND id = :conflict_set_id
                """
            ),
            {"project_id": project_id, "conflict_set_id": conflict_set_id},
        ).mappings().first()
        if row is None:
            raise GitHubConflictSetNotFoundError(
                "GitHub conflict set not found."
            )
        return self._serialize_conflict(row)

    def list_resolution_plans(
        self,
        connection: Connection,
        project_id: str,
        *,
        conflict_set_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._validate_limit(limit)
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if conflict_set_id:
            filters.append("conflict_set_id = :conflict_set_id")
            params["conflict_set_id"] = conflict_set_id
        ids = connection.execute(
            text(
                f"""
                SELECT id
                FROM github_resolution_plans
                WHERE {" AND ".join(filters)}
                ORDER BY created_at DESC, version DESC
                LIMIT :limit
                """
            ),
            params,
        ).scalars().all()
        return [
            self.get_resolution_plan(connection, project_id, str(item))
            for item in ids
        ]

    def get_resolution_plan(
        self,
        connection: Connection,
        project_id: str,
        plan_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT {self._resolution_columns()}
                FROM github_resolution_plans
                WHERE project_id = :project_id AND id = :plan_id
                """
            ),
            {"project_id": project_id, "plan_id": plan_id},
        ).mappings().first()
        if row is None:
            raise GitHubResolutionPlanNotFoundError(
                "GitHub resolution plan not found."
            )
        return self._serialize_resolution_plan(row)

    def _apply_fast_forward(
        self,
        connection: Connection,
        *,
        run: Any,
        initial: dict[str, Any],
        adapter: Any,
        binding: dict[str, Any],
        attempt: int,
        actor: str,
    ) -> dict[str, Any]:
        if run["action"] == "fast_forward_target":
            ref = run["target_ref"]
            expected_sha = initial["target_commit_sha"]
            new_sha = initial["source_commit_sha"]
        else:
            ref = run["source_ref"]
            expected_sha = initial["source_commit_sha"]
            new_sha = initial["target_commit_sha"]
        return self._perform_provider_action(
            connection,
            project_id=run["project_id"],
            changeset_id=run["changeset_id"],
            operation_index=0,
            binding=binding,
            dedupe_key=f"github-sync:{run['id']}:{attempt}:fast-forward",
            action="fast_forward_ref",
            resource_ref=ref,
            payload={
                "expected_sha": expected_sha,
                "new_sha": new_sha,
                "force": False,
            },
            actor=actor,
            callback=lambda: adapter.fast_forward_ref(
                ref,
                expected_sha=expected_sha,
                new_sha=new_sha,
            ),
        )

    def _apply_resolution_branch(
        self,
        connection: Connection,
        *,
        run: Any,
        initial: dict[str, Any],
        adapter: Any,
        binding: dict[str, Any],
        attempt: int,
        actor: str,
    ) -> dict[str, Any]:
        resolution_branch = run["resolution_branch"]

        def create_or_reuse_ref() -> dict[str, Any]:
            current = adapter.branch_snapshot(resolution_branch)
            if current["exists"]:
                if current["commit_sha"] != initial["source_commit_sha"]:
                    raise GitHubCodeSyncStaleError(
                        "Resolution branch exists at an unexpected commit."
                    )
                return {
                    "ref": resolution_branch,
                    "commit_sha": current["commit_sha"],
                    "reused": True,
                }
            return adapter.create_branch_ref(
                resolution_branch, initial["source_commit_sha"]
            )

        self._perform_provider_action(
            connection,
            project_id=run["project_id"],
            changeset_id=run["changeset_id"],
            operation_index=0,
            binding=binding,
            dedupe_key=f"github-sync:{run['id']}:{attempt}:create-ref",
            action="create_branch_ref",
            resource_ref=resolution_branch,
            payload={"commit_sha": initial["source_commit_sha"]},
            actor=actor,
            callback=create_or_reuse_ref,
        )
        return self._perform_provider_action(
            connection,
            project_id=run["project_id"],
            changeset_id=run["changeset_id"],
            operation_index=1,
            binding=binding,
            dedupe_key=f"github-sync:{run['id']}:{attempt}:merge",
            action="merge_branch",
            resource_ref=resolution_branch,
            payload={
                "base_branch": resolution_branch,
                "head_sha": initial["target_commit_sha"],
            },
            actor=actor,
            callback=lambda: adapter.merge_branch(
                base_branch=resolution_branch,
                head_sha=initial["target_commit_sha"],
                message=(
                    f"Sync {run['target_ref']} into {run['source_ref']} "
                    "on a dedicated resolution branch"
                ),
            ),
        )

    def _record_conflict(
        self,
        connection: Connection,
        *,
        run: Any,
        initial: dict[str, Any],
        adapter: Any,
        attempt: int,
        actor: str,
    ) -> dict[str, Any]:
        existing = connection.execute(
            text(
                """
                SELECT id
                FROM github_conflict_sets
                WHERE code_sync_run_id = :sync_run_id
                """
            ),
            {"sync_run_id": run["id"]},
        ).scalar_one_or_none()
        if existing:
            conflict_id = str(existing)
        else:
            try:
                files = adapter.compare_changed_files(
                    initial["source_commit_sha"],
                    initial["target_commit_sha"],
                )
            except Exception as error:
                files = [
                    {
                        "filename": "",
                        "status": "evidence_unavailable",
                        "error_type": error.__class__.__name__,
                    }
                ]
            conflict_id = make_id("ghconflict")
            connection.execute(
                text(
                    """
                    INSERT INTO github_conflict_sets (
                        id, project_id, code_sync_run_id, repository_binding_id,
                        resolution_branch, resolution_branch_head_sha,
                        base_commit_sha, base_tree_sha, ours_commit_sha,
                        ours_tree_sha, theirs_commit_sha, theirs_tree_sha,
                        status, files, conflict_count
                    )
                    VALUES (
                        :id, :project_id, :code_sync_run_id,
                        :repository_binding_id, :resolution_branch,
                        :resolution_branch_head_sha, :base_commit_sha,
                        :base_tree_sha, :ours_commit_sha, :ours_tree_sha,
                        :theirs_commit_sha, :theirs_tree_sha, 'open',
                        CAST(:files AS JSONB), :conflict_count
                    )
                    """
                ),
                {
                    "id": conflict_id,
                    "project_id": run["project_id"],
                    "code_sync_run_id": run["id"],
                    "repository_binding_id": run["repository_binding_id"],
                    "resolution_branch": run["resolution_branch"],
                    "resolution_branch_head_sha": initial["source_commit_sha"],
                    "base_commit_sha": initial["merge_base_commit_sha"],
                    "base_tree_sha": initial["merge_base_tree_sha"],
                    "ours_commit_sha": initial["source_commit_sha"],
                    "ours_tree_sha": initial["source_tree_sha"],
                    "theirs_commit_sha": initial["target_commit_sha"],
                    "theirs_tree_sha": initial["target_tree_sha"],
                    "files": json.dumps(files),
                    "conflict_count": len(files),
                },
            )
        self._finish_changeset(
            connection,
            changeset_id=run["changeset_id"],
            status="conflicted",
            actor=actor,
            failure_reason="GitHub reported merge conflicts.",
        )
        connection.execute(
            text(
                """
                UPDATE github_code_sync_runs
                SET status = 'conflicted', conflict_set_id = :conflict_set_id,
                    updated_at = now()
                WHERE id = :sync_run_id
                """
            ),
            {"conflict_set_id": conflict_id, "sync_run_id": run["id"]},
        )
        emit_event(
            connection,
            run["project_id"],
            "github.code_sync_conflicted",
            actor,
            "github_code_sync_run",
            run["id"],
            {
                "conflict_set_id": conflict_id,
                "resolution_branch": run["resolution_branch"],
                "attempt": attempt,
            },
        )
        return self.get(connection, run["project_id"], run["id"])

    def _record_run_failure(
        self,
        connection: Connection,
        *,
        run: Any,
        attempt: int,
        error_type: str,
        actor: str,
    ) -> dict[str, Any]:
        deadlettered = attempt >= self.MAX_APPLY_ATTEMPTS
        status = "deadlettered" if deadlettered else "failed"
        safe_error = {
            "type": error_type,
            "message": "GitHub code sync apply failed.",
            "attempt": attempt,
        }
        connection.execute(
            text(
                """
                UPDATE github_code_sync_runs
                SET status = :status, apply_attempts = :attempt,
                    error = CAST(:error AS JSONB), updated_at = now()
                WHERE id = :sync_run_id
                """
            ),
            {
                "status": status,
                "attempt": attempt,
                "error": json.dumps(safe_error),
                "sync_run_id": run["id"],
            },
        )
        self._finish_changeset(
            connection,
            changeset_id=run["changeset_id"],
            status=status,
            actor=actor,
            failure_reason=safe_error["message"],
        )
        if deadlettered:
            self._create_deadletter(
                connection,
                project_id=run["project_id"],
                source_kind="github_code_sync_run",
                source_id=run["id"],
                reason=safe_error["message"],
                payload=safe_error,
            )
        emit_event(
            connection,
            run["project_id"],
            "github.code_sync_deadlettered"
            if deadlettered
            else "github.code_sync_failed",
            actor,
            "github_code_sync_run",
            run["id"],
            safe_error,
        )
        return self.get(connection, run["project_id"], run["id"])

    def _perform_provider_action(
        self,
        connection: Connection,
        *,
        project_id: str,
        changeset_id: str,
        operation_index: int,
        binding: dict[str, Any],
        dedupe_key: str,
        action: str,
        resource_ref: str,
        payload: dict[str, Any],
        actor: str,
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        operation = connection.execute(
            text(
                """
                SELECT id, skill_id, action, resource_type, resource_ref
                FROM changeset_operations
                WHERE changeset_id = :changeset_id
                  AND operation_index = :operation_index
                """
            ),
            {
                "changeset_id": changeset_id,
                "operation_index": operation_index,
            },
        ).mappings().one()
        existing = connection.execute(
            text(
                """
                SELECT id, changeset_id, operation_id, provider, action,
                       resource_ref, status, result, error
                FROM resource_receipts
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).mappings().first()
        if existing:
            if (
                existing["changeset_id"] != changeset_id
                or existing["operation_id"] != operation["id"]
                or existing["provider"] != "github"
                or existing["action"] != action
                or existing["resource_ref"] != resource_ref
            ):
                raise GitHubCodeSyncStateError(
                    "Provider action dedupe key is bound to a different effect."
                )
            if existing["status"] == "succeeded":
                return dict(existing["result"] or {})

        scope = GitHubBranchService._scope(connection, project_id)
        effect_scope = {
            "project_id": project_id,
            "repository_binding_id": binding["id"],
            "repository": binding["full_name"],
            "changeset_id": changeset_id,
            "operation_id": operation["id"],
            "resource_ref": resource_ref,
        }
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            f"github.{action}",
            effect_scope,
            payload,
            data_boundary="internal",
        )
        if not created:
            receipt_scope = receipt.get("effect_scope") or {}
            if (
                receipt.get("effect_kind") != f"github.{action}"
                or receipt_scope.get("repository_binding_id") != binding["id"]
                or receipt_scope.get("changeset_id") != changeset_id
                or receipt_scope.get("operation_id") != operation["id"]
                or receipt_scope.get("resource_ref") != resource_ref
            ):
                raise GitHubCodeSyncStateError(
                    "Receipt dedupe key is bound to a different GitHub effect."
                )

        resource_receipt_id = (
            str(existing["id"]) if existing else make_id("rrcpt")
        )
        if not existing:
            connection.execute(
                text(
                    """
                    INSERT INTO resource_receipts (
                        id, project_id, changeset_id, operation_id, receipt_id,
                        dedupe_key, provider, action, resource_type, resource_ref,
                        status, request_hash
                    )
                    VALUES (
                        :id, :project_id, :changeset_id, :operation_id,
                        :receipt_id, :dedupe_key, 'github', :action,
                        'github_branch_ref', :resource_ref, 'in_progress',
                        :request_hash
                    )
                    """
                ),
                {
                    "id": resource_receipt_id,
                    "project_id": project_id,
                    "changeset_id": changeset_id,
                    "operation_id": operation["id"],
                    "receipt_id": receipt["id"],
                    "dedupe_key": dedupe_key,
                    "action": action,
                    "resource_ref": resource_ref,
                    "request_hash": json_hash(payload),
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE github_code_sync_runs
                    SET receipt_ids =
                            receipt_ids || CAST(:receipt_id AS JSONB),
                        resource_receipt_ids =
                            resource_receipt_ids
                            || CAST(:resource_receipt_id AS JSONB),
                        updated_at = now()
                    WHERE changeset_id = :changeset_id
                    """
                ),
                {
                    "receipt_id": json.dumps([receipt["id"]]),
                    "resource_receipt_id": json.dumps([resource_receipt_id]),
                    "changeset_id": changeset_id,
                },
            )
        try:
            result = callback()
        except Exception as error:
            safe_error = {
                "type": error.__class__.__name__,
                "message": "GitHub provider action failed.",
            }
            self.receipts.mark_failed(connection, receipt["id"], safe_error)
            connection.execute(
                text(
                    """
                    UPDATE resource_receipts
                    SET status = 'failed', error = CAST(:error AS JSONB),
                        updated_at = now()
                    WHERE id = :resource_receipt_id
                    """
                ),
                {
                    "resource_receipt_id": resource_receipt_id,
                    "error": json.dumps(safe_error),
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE changeset_operations
                    SET status = 'failed',
                        resource_receipt_id = :resource_receipt_id,
                        updated_at = now()
                    WHERE id = :operation_id
                    """
                ),
                {
                    "operation_id": operation["id"],
                    "resource_receipt_id": resource_receipt_id,
                },
            )
            emit_event(
                connection,
                project_id,
                "resource_receipt.failed",
                actor,
                "resource_receipt",
                resource_receipt_id,
                {
                    "changeset_id": changeset_id,
                    "operation_id": operation["id"],
                    "provider": "github",
                    "action": action,
                    "error": safe_error,
                },
            )
            raise
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            str(result.get("commit_sha") or binding["full_name"]),
            result,
        )
        connection.execute(
            text(
                """
                UPDATE resource_receipts
                SET status = 'succeeded', provider_ref = :provider_ref,
                    result = CAST(:result AS JSONB), error = NULL,
                    updated_at = now()
                WHERE id = :resource_receipt_id
                """
            ),
            {
                "resource_receipt_id": resource_receipt_id,
                "provider_ref": str(
                    result.get("commit_sha") or binding["full_name"]
                ),
                "result": json.dumps(result),
            },
        )
        connection.execute(
            text(
                """
                UPDATE changeset_operations
                SET status = 'succeeded',
                    resource_receipt_id = :resource_receipt_id,
                    updated_at = now()
                WHERE id = :operation_id
                """
            ),
            {
                "operation_id": operation["id"],
                "resource_receipt_id": resource_receipt_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "resource_receipt.succeeded",
            actor,
            "resource_receipt",
            resource_receipt_id,
            {
                "changeset_id": changeset_id,
                "operation_id": operation["id"],
                "provider": "github",
                "action": action,
                "provider_ref": result.get("commit_sha"),
            },
        )
        return result

    def _create_changeset(
        self,
        connection: Connection,
        *,
        project_id: str,
        sync_run_id: str,
        action: str,
        source_ref: str,
        target_ref: str,
        resolution_branch: Optional[str],
        source_sha: Optional[str],
        target_sha: Optional[str],
        policy_evaluation: dict[str, Any],
        status: str,
        actor: str,
    ) -> str:
        changeset_id = make_id("chg")
        if action == "create_resolution_branch":
            operations = [
                {
                    "skill_id": "github.create_branch_ref",
                    "action": "create_branch_ref",
                    "resource_ref": resolution_branch,
                    "desired_state": {"commit_sha": source_sha},
                },
                {
                    "skill_id": "github.merge_branch",
                    "action": "merge_branch",
                    "resource_ref": resolution_branch,
                    "desired_state": {"head_sha": target_sha},
                },
            ]
            rollback = [
                {
                    "action": "delete_resolution_branch_after_approval",
                    "ref": resolution_branch,
                    "automatic": False,
                }
            ]
        else:
            mutable_ref = (
                target_ref if action == "fast_forward_target" else source_ref
            )
            previous_sha = (
                target_sha if action == "fast_forward_target" else source_sha
            )
            next_sha = (
                source_sha if action == "fast_forward_target" else target_sha
            )
            operations = [
                {
                    "skill_id": "github.fast_forward_ref",
                    "action": "fast_forward_ref",
                    "resource_ref": mutable_ref,
                    "desired_state": {
                        "expected_sha": previous_sha,
                        "new_sha": next_sha,
                        "force": False,
                    },
                }
            ]
            rollback = [
                {
                    "action": "restore_ref_with_explicit_approval",
                    "ref": mutable_ref,
                    "sha": previous_sha,
                    "automatic": False,
                }
            ]
        changeset_status = (
            "pending_approval" if status == "pending_approval" else "draft"
        )
        connection.execute(
            text(
                """
                INSERT INTO changesets (
                    id, project_id, title, summary, status, risk_tier,
                    operations, expected_cost_usd_micros, cost_quote,
                    rollback_plan, expected_receipts, policy_result, created_by
                )
                VALUES (
                    :id, :project_id, :title, :summary, :status, 2,
                    CAST(:operations AS JSONB), 0, CAST(:cost_quote AS JSONB),
                    CAST(:rollback_plan AS JSONB),
                    CAST(:expected_receipts AS JSONB),
                    CAST(:policy_result AS JSONB), :created_by
                )
                """
            ),
            {
                "id": changeset_id,
                "project_id": project_id,
                "title": f"GitHub code sync {source_ref} -> {target_ref}",
                "summary": (
                    f"Policy-gated GitHub sync run {sync_run_id}; no force push."
                ),
                "status": changeset_status,
                "operations": json.dumps(operations),
                "cost_quote": json.dumps(
                    {
                        "amount_usd_micros": 0,
                        "reservation_required": False,
                    }
                ),
                "rollback_plan": json.dumps(rollback),
                "expected_receipts": json.dumps(
                    [
                        {
                            "provider": "github",
                            "action": item["action"],
                            "resource_ref": item["resource_ref"],
                        }
                        for item in operations
                    ]
                ),
                "policy_result": json.dumps(policy_evaluation),
                "created_by": actor,
            },
        )
        for index, operation in enumerate(operations):
            connection.execute(
                text(
                    """
                    INSERT INTO changeset_operations (
                        id, project_id, changeset_id, operation_index, skill_id,
                        action, resource_type, resource_ref, desired_state,
                        risk_tier, destructive, production_affecting
                    )
                    VALUES (
                        :id, :project_id, :changeset_id, :operation_index,
                        :skill_id, :action, 'github_branch_ref', :resource_ref,
                        CAST(:desired_state AS JSONB), 2, false,
                        :production_affecting
                    )
                    """
                ),
                {
                    "id": make_id("chgo"),
                    "project_id": project_id,
                    "changeset_id": changeset_id,
                    "operation_index": index,
                    "skill_id": operation["skill_id"],
                    "action": operation["action"],
                    "resource_ref": operation["resource_ref"],
                    "desired_state": json.dumps(operation["desired_state"]),
                    "production_affecting": bool(
                        policy_evaluation.get("protected")
                    ),
                },
            )
        return changeset_id

    def _create_resolution_changeset(
        self,
        connection: Connection,
        *,
        project_id: str,
        plan_id: str,
        conflict: Any,
        resolved_files: list[dict[str, Any]],
        actor: str,
    ) -> str:
        changeset_id = make_id("chg")
        operations = [
            {
                "skill_id": "github.create_resolution_commit",
                "action": "create_resolution_commit",
                "resource_ref": conflict["resolution_branch"],
                "desired_state": {
                    "parents": [
                        conflict["resolution_branch_head_sha"],
                        conflict["theirs_commit_sha"],
                    ],
                    "resolved_paths": [item["path"] for item in resolved_files],
                },
            },
            {
                "skill_id": "github.fast_forward_ref",
                "action": "fast_forward_ref",
                "resource_ref": conflict["resolution_branch"],
                "desired_state": {
                    "expected_sha": conflict["resolution_branch_head_sha"],
                    "force": False,
                },
            },
        ]
        connection.execute(
            text(
                """
                INSERT INTO changesets (
                    id, project_id, title, summary, status, risk_tier,
                    operations, expected_cost_usd_micros, cost_quote,
                    rollback_plan, expected_receipts, policy_result, created_by
                )
                VALUES (
                    :id, :project_id, :title, :summary, 'pending_approval', 2,
                    CAST(:operations AS JSONB), 0, CAST(:cost_quote AS JSONB),
                    CAST(:rollback_plan AS JSONB),
                    CAST(:expected_receipts AS JSONB),
                    CAST(:policy_result AS JSONB), :created_by
                )
                """
            ),
            {
                "id": changeset_id,
                "project_id": project_id,
                "title": (
                    f"GitHub conflict resolution {conflict['resolution_branch']}"
                ),
                "summary": (
                    f"Reviewed resolution plan {plan_id}; target branch remains "
                    "unchanged until merge authorization."
                ),
                "operations": json.dumps(operations),
                "cost_quote": json.dumps(
                    {
                        "amount_usd_micros": 0,
                        "reservation_required": False,
                    }
                ),
                "rollback_plan": json.dumps(
                    [
                        {
                            "action": "restore_resolution_branch",
                            "ref": conflict["resolution_branch"],
                            "sha": conflict["resolution_branch_head_sha"],
                            "automatic": False,
                        }
                    ]
                ),
                "expected_receipts": json.dumps(
                    [
                        {
                            "provider": "github",
                            "action": operation["action"],
                            "resource_ref": operation["resource_ref"],
                        }
                        for operation in operations
                    ]
                ),
                "policy_result": json.dumps(
                    {
                        "allowed": True,
                        "requires_independent_review": True,
                        "requires_red_green_evidence": True,
                        "target_branch_write": False,
                    }
                ),
                "created_by": actor,
            },
        )
        for index, operation in enumerate(operations):
            connection.execute(
                text(
                    """
                    INSERT INTO changeset_operations (
                        id, project_id, changeset_id, operation_index, skill_id,
                        action, resource_type, resource_ref, desired_state,
                        risk_tier, destructive, production_affecting
                    )
                    VALUES (
                        :id, :project_id, :changeset_id, :operation_index,
                        :skill_id, :action, 'github_branch_ref', :resource_ref,
                        CAST(:desired_state AS JSONB), 2, false, false
                    )
                    """
                ),
                {
                    "id": make_id("chgo"),
                    "project_id": project_id,
                    "changeset_id": changeset_id,
                    "operation_index": index,
                    "skill_id": operation["skill_id"],
                    "action": operation["action"],
                    "resource_ref": operation["resource_ref"],
                    "desired_state": json.dumps(operation["desired_state"]),
                },
            )
        return changeset_id

    def _finish_changeset(
        self,
        connection: Connection,
        *,
        changeset_id: Optional[str],
        status: str,
        actor: str,
        failure_reason: Optional[str] = None,
    ) -> None:
        if not changeset_id:
            return
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = :status, applied_by = CASE
                        WHEN :status = 'applied' THEN :actor ELSE applied_by END,
                    applied_at = CASE
                        WHEN :status = 'applied' THEN now() ELSE applied_at END,
                    approved_by = CASE
                        WHEN :status = 'approved' THEN :actor ELSE approved_by END,
                    approved_at = CASE
                        WHEN :status = 'approved' THEN now() ELSE approved_at END,
                    failure_reason = :failure_reason, updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {
                "changeset_id": changeset_id,
                "status": status,
                "actor": actor,
                "failure_reason": failure_reason,
            },
        )

    def _planned_action(
        self,
        *,
        policy: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        topology = snapshot["topology_classification"]
        if snapshot["classification"] == "protected_blocked":
            return "blocked", "blocked", snapshot["policy_evaluation"]
        if topology == "equal":
            return "noop", "noop", snapshot["policy_evaluation"]
        if topology == "tree_equivalent_rewrite":
            return (
                "preserve_remote_identity",
                "noop",
                snapshot["policy_evaluation"],
            )
        if topology == "missing":
            return "blocked", "blocked", snapshot["policy_evaluation"]
        if topology == "fast_forward":
            evaluation = self.github_branches.evaluate_policy(
                policy,
                ref=snapshot["target_ref"],
                operation="update",
                method="fast_forward",
            )
            return (
                "fast_forward_target",
                self._status_from_policy(evaluation),
                evaluation,
            )
        if topology == "behind":
            evaluation = self.github_branches.evaluate_policy(
                policy,
                ref=snapshot["source_ref"],
                operation="update",
                method="fast_forward",
            )
            return (
                "fast_forward_source",
                self._status_from_policy(evaluation),
                evaluation,
            )
        return (
            "create_resolution_branch",
            "planned",
            snapshot["policy_evaluation"],
        )

    @staticmethod
    def _status_from_policy(evaluation: dict[str, Any]) -> str:
        return {
            "allowed": "planned",
            "approval_required": "pending_approval",
            "blocked": "blocked",
        }[evaluation["status"]]

    @staticmethod
    def _rollback_identity(
        *,
        action: str,
        snapshot: dict[str, Any],
        resolution_branch: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if action == "fast_forward_target":
            return snapshot["target_ref"], snapshot["target_commit_sha"]
        if action == "fast_forward_source":
            return snapshot["source_ref"], snapshot["source_commit_sha"]
        if action == "create_resolution_branch":
            return resolution_branch, snapshot["source_commit_sha"]
        return None, None

    @staticmethod
    def _default_resolution_branch(snapshot: dict[str, Any]) -> str:
        source = re.sub(r"[^A-Za-z0-9._-]+", "-", snapshot["source_ref"]).strip(
            "-"
        )
        target = re.sub(r"[^A-Za-z0-9._-]+", "-", snapshot["target_ref"]).strip(
            "-"
        )
        suffix = snapshot["id"].split("_")[-1][:12]
        return f"ai/boss/resolve/{source}-into-{target}-{suffix}"[:240]

    @staticmethod
    def _mutable_ref(run: Any) -> str:
        if run["action"] == "fast_forward_target":
            return run["target_ref"]
        if run["action"] == "fast_forward_source":
            return run["source_ref"]
        if run["action"] == "create_resolution_branch":
            return run["resolution_branch"]
        raise GitHubCodeSyncStateError(
            f"Code sync action {run['action']} has no mutable ref."
        )

    def _assert_active_policy(self, connection: Connection, run: Any) -> None:
        active = self.github_branches.active_policy_for_binding(
            connection,
            run["project_id"],
            run["repository_binding_id"],
        )
        if (
            active["id"] != run["branch_policy_id"]
            or int(active["version"]) != int(run["branch_policy_version"])
        ):
            raise GitHubCodeSyncStaleError(
                "Branch policy changed after this sync run was planned."
            )

    @staticmethod
    def _assert_snapshot_identity(
        initial: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        identity_fields = (
            "source_ref",
            "target_ref",
            "source_commit_sha",
            "source_tree_sha",
            "target_commit_sha",
            "target_tree_sha",
            "topology_classification",
        )
        if any(initial[field] != current[field] for field in identity_fields):
            raise GitHubCodeSyncStaleError(
                "Remote branch topology changed after planning."
            )

    def _locked_run(
        self,
        connection: Connection,
        project_id: str,
        sync_run_id: str,
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._run_columns()}
                FROM github_code_sync_runs
                WHERE project_id = :project_id AND id = :sync_run_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "sync_run_id": sync_run_id},
        ).mappings().first()
        if row is None:
            raise GitHubCodeSyncNotFoundError("GitHub code sync run not found.")
        return row

    def _locked_conflict(
        self,
        connection: Connection,
        project_id: str,
        conflict_set_id: str,
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._conflict_columns()}
                FROM github_conflict_sets
                WHERE project_id = :project_id AND id = :conflict_set_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "conflict_set_id": conflict_set_id},
        ).mappings().first()
        if row is None:
            raise GitHubConflictSetNotFoundError(
                "GitHub conflict set not found."
            )
        return row

    def _locked_resolution_plan(
        self,
        connection: Connection,
        project_id: str,
        plan_id: str,
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._resolution_columns()}
                FROM github_resolution_plans
                WHERE project_id = :project_id AND id = :plan_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "plan_id": plan_id},
        ).mappings().first()
        if row is None:
            raise GitHubResolutionPlanNotFoundError(
                "GitHub resolution plan not found."
            )
        return row

    def _resolution_apply_response(
        self,
        connection: Connection,
        project_id: str,
        plan_id: str,
    ) -> dict[str, Any]:
        plan = self.get_resolution_plan(connection, project_id, plan_id)
        conflict = self.get_conflict(
            connection, project_id, plan["conflict_set_id"]
        )
        run = self.get(connection, project_id, conflict["code_sync_run_id"])
        return {"plan": plan, "conflict_set": conflict, "sync_run": run}

    @staticmethod
    def _validate_resolved_files(
        values: list[dict[str, Any]],
        *,
        conflict_files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not values or len(values) > 100:
            raise ValueError("resolved_files must contain between 1 and 100 files.")
        allowed_paths = {
            str(item.get("filename") or "")
            for item in conflict_files
            if str(item.get("filename") or "").strip()
        }
        normalized = []
        seen: set[str] = set()
        total_bytes = 0
        for item in values:
            path = str(item.get("path") or "").strip()
            content = item.get("content")
            mode = str(item.get("mode") or "100644")
            if (
                not path
                or path.startswith("/")
                or ".." in path.split("/")
                or "\x00" in path
            ):
                raise ValueError("Resolved paths must be safe repository-relative paths.")
            if path in seen:
                raise ValueError("resolved_files contains a duplicate path.")
            if allowed_paths and path not in allowed_paths:
                raise ValueError(
                    "Each resolved path must appear in the conflict evidence."
                )
            if not isinstance(content, str):
                raise ValueError("Resolved file content must be UTF-8 text.")
            if mode not in {"100644", "100755"}:
                raise ValueError("Resolved file mode must be 100644 or 100755.")
            total_bytes += len(content.encode("utf-8"))
            if total_bytes > 2_000_000:
                raise ValueError("Resolved file content exceeds the 2 MB limit.")
            seen.add(path)
            normalized.append({"path": path, "content": content, "mode": mode})
        return normalized

    @staticmethod
    def _validate_red_green_evidence(value: dict[str, Any]) -> dict[str, Any]:
        evidence = dict(value or {})
        for key in ("red", "green"):
            item = evidence.get(key)
            if not isinstance(item, dict):
                raise ValueError(
                    "validation_evidence must contain red and green objects."
                )
            if not str(item.get("command") or "").strip():
                raise ValueError(
                    f"validation_evidence.{key}.command must be non-empty."
                )
            if item.get("exit_code") is None:
                raise ValueError(
                    f"validation_evidence.{key}.exit_code is required."
                )
            if not str(item.get("evidence_ref") or "").strip():
                raise ValueError(
                    f"validation_evidence.{key}.evidence_ref must be non-empty."
                )
        if int(evidence["red"]["exit_code"]) == 0:
            raise ValueError("Red proof must show a failing exit code.")
        if int(evidence["green"]["exit_code"]) != 0:
            raise ValueError("Green proof must show a successful exit code.")
        return evidence

    @staticmethod
    def _create_deadletter(
        connection: Connection,
        *,
        project_id: str,
        source_kind: str,
        source_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        existing = connection.execute(
            text(
                """
                SELECT id
                FROM deadletters
                WHERE project_id = :project_id
                  AND source_kind = :source_kind
                  AND source_id = :source_id
                  AND status = 'open'
                """
            ),
            {
                "project_id": project_id,
                "source_kind": source_kind,
                "source_id": source_id,
            },
        ).scalar_one_or_none()
        if existing:
            return
        connection.execute(
            text(
                """
                INSERT INTO deadletters (
                    id, project_id, source_kind, source_id, reason, payload
                )
                VALUES (
                    :id, :project_id, :source_kind, :source_id, :reason,
                    CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "id": make_id("dlq"),
                "project_id": project_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "reason": reason,
                "payload": json.dumps(payload),
            },
        )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty.")
        if "\x00" in normalized:
            raise ValueError(f"{field} contains an invalid character.")
        return normalized

    @staticmethod
    def _run_columns() -> str:
        return """
            id, project_id, repository_binding_id, branch_policy_id,
            branch_policy_version, initial_snapshot_id, result_snapshot_id,
            branch_lease_id, fencing_token, source_ref, target_ref,
            topology_classification, action, status, dedupe_key, changeset_id,
            conflict_set_id, decision_id, resolution_branch, rollback_ref,
            rollback_sha, apply_attempts, receipt_ids, resource_receipt_ids,
            error, created_by, approved_by, applied_by, created_at, updated_at,
            completed_at
        """

    @staticmethod
    def _conflict_columns() -> str:
        return """
            id, project_id, code_sync_run_id, repository_binding_id,
            resolution_branch, resolution_branch_head_sha, base_commit_sha,
            base_tree_sha, ours_commit_sha, ours_tree_sha, theirs_commit_sha,
            theirs_tree_sha, status, files, conflict_count, decision_id,
            resolved_by_plan_id, created_at, updated_at, resolved_at
        """

    @staticmethod
    def _resolution_columns() -> str:
        return """
            id, project_id, conflict_set_id, version, status, resolved_files,
            validation_evidence, independent_review, decision_id, changeset_id,
            result_commit_sha, created_by, approved_by, applied_by, created_at,
            updated_at, applied_at
        """

    def _serialize_run(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "branch_policy_id": row["branch_policy_id"],
            "branch_policy_version": row["branch_policy_version"],
            "initial_snapshot_id": row["initial_snapshot_id"],
            "result_snapshot_id": row["result_snapshot_id"],
            "branch_lease_id": row["branch_lease_id"],
            "fencing_token": row["fencing_token"],
            "source_ref": row["source_ref"],
            "target_ref": row["target_ref"],
            "topology_classification": row["topology_classification"],
            "action": row["action"],
            "status": row["status"],
            "dedupe_key": row["dedupe_key"],
            "changeset_id": row["changeset_id"],
            "conflict_set_id": row["conflict_set_id"],
            "decision_id": row["decision_id"],
            "resolution_branch": row["resolution_branch"],
            "rollback_ref": row["rollback_ref"],
            "rollback_sha": row["rollback_sha"],
            "apply_attempts": row["apply_attempts"],
            "receipt_ids": list(row["receipt_ids"] or []),
            "resource_receipt_ids": list(row["resource_receipt_ids"] or []),
            "error": row["error"],
            "created_by": row["created_by"],
            "approved_by": row["approved_by"],
            "applied_by": row["applied_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "completed_at": (
                row["completed_at"].isoformat() if row["completed_at"] else None
            ),
        }

    @staticmethod
    def _serialize_conflict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "code_sync_run_id": row["code_sync_run_id"],
            "repository_binding_id": row["repository_binding_id"],
            "resolution_branch": row["resolution_branch"],
            "resolution_branch_head_sha": row["resolution_branch_head_sha"],
            "base_commit_sha": row["base_commit_sha"],
            "base_tree_sha": row["base_tree_sha"],
            "ours_commit_sha": row["ours_commit_sha"],
            "ours_tree_sha": row["ours_tree_sha"],
            "theirs_commit_sha": row["theirs_commit_sha"],
            "theirs_tree_sha": row["theirs_tree_sha"],
            "status": row["status"],
            "files": row["files"] or [],
            "conflict_count": row["conflict_count"],
            "decision_id": row["decision_id"],
            "resolved_by_plan_id": row["resolved_by_plan_id"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "resolved_at": (
                row["resolved_at"].isoformat() if row["resolved_at"] else None
            ),
        }

    @staticmethod
    def _serialize_resolution_plan(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "conflict_set_id": row["conflict_set_id"],
            "version": row["version"],
            "status": row["status"],
            "resolved_files": row["resolved_files"] or [],
            "validation_evidence": row["validation_evidence"] or {},
            "independent_review": row["independent_review"] or {},
            "decision_id": row["decision_id"],
            "changeset_id": row["changeset_id"],
            "result_commit_sha": row["result_commit_sha"],
            "created_by": row["created_by"],
            "approved_by": row["approved_by"],
            "applied_by": row["applied_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "applied_at": (
                row["applied_at"].isoformat() if row["applied_at"] else None
            ),
        }
