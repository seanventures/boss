from __future__ import annotations

from datetime import timedelta
import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, json_hash, make_id, now_utc
from .github_branch_leases import GitHubBranchLeaseService
from .github_branches import GitHubBranchService
from .github_connections import GitHubConnectionService
from .receipts import ReceiptService


class GitHubMergeAuthorizationNotFoundError(RuntimeError):
    pass


class GitHubMergeAuthorizationStateError(RuntimeError):
    pass


class GitHubMergeAuthorizationStaleError(RuntimeError):
    pass


class GitHubMergeAuthorizationService:
    """Exact-snapshot authorization and serialized protected-branch merging."""

    MAX_APPLY_ATTEMPTS = 3
    MIN_EXPIRY_SECONDS = 60
    MAX_EXPIRY_SECONDS = 86_400
    _SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
    _PASSING_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}

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

    def request_authorization(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        pull_request_number: int,
        source_ref: str,
        target_ref: str,
        merge_method: str,
        dedupe_key: str,
        evidence_bundle: dict[str, Any],
        expires_in_seconds: int,
        code_sync_run_id: Optional[str],
        resolution_plan_id: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        source = GitHubBranchService._normalize_ref(source_ref)
        target = GitHubBranchService._normalize_ref(target_ref)
        if source == target:
            raise ValueError("source_ref and target_ref must be different.")
        if pull_request_number < 1:
            raise ValueError("pull_request_number must be positive.")
        method = self._required_text(merge_method, "merge_method").lower()
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError("merge_method must be merge, squash, or rebase.")
        expiry_seconds = int(expires_in_seconds)
        if not self.MIN_EXPIRY_SECONDS <= expiry_seconds <= self.MAX_EXPIRY_SECONDS:
            raise ValueError(
                "expires_in_seconds must be between 60 and 86400."
            )
        safe_dedupe_key = self._required_text(dedupe_key, "dedupe_key")
        evidence = self._validate_evidence_bundle(evidence_bundle)
        created_by = self._required_text(actor, "actor")

        replay = connection.execute(
            text(
                """
                SELECT id
                FROM github_merge_authorizations
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": safe_dedupe_key},
        ).scalar_one_or_none()
        if replay:
            item = self.get(connection, project_id, str(replay))
            if (
                item["repository_binding_id"] != repository_binding_id
                or item["pull_request_number"] != pull_request_number
                or item["source_ref"] != source
                or item["target_ref"] != target
                or item["merge_method"] != method
            ):
                raise GitHubMergeAuthorizationStateError(
                    "dedupe_key is already bound to a different merge authorization."
                )
            return item

        policy = self.github_branches.active_policy_for_binding(
            connection, project_id, repository_binding_id
        )
        evaluation = self.github_branches.evaluate_policy(
            policy,
            ref=target,
            operation="merge",
            method=method,
        )
        if policy["require_pull_request"] is False and evaluation["protected"]:
            evaluation = {
                **evaluation,
                "status": "blocked",
                "reasons": sorted(
                    set(evaluation["reasons"])
                    | {"protected_ref_requires_pull_request"}
                ),
            }

        adapter, _, binding = self.github_connections.adapter_for_binding(
            connection,
            project_id=project_id,
            binding_id=repository_binding_id,
        )
        observation, observation_receipt_id = self._observe_provider(
            connection,
            project_id=project_id,
            binding=binding,
            adapter=adapter,
            pull_request_number=pull_request_number,
            dedupe_key=(
                f"github-merge:{project_id}:{safe_dedupe_key}:observe"
            ),
            actor=created_by,
        )
        self._assert_requested_snapshot(
            observation,
            binding=binding,
            pull_request_number=pull_request_number,
            source_ref=source,
            target_ref=target,
        )
        self._assert_linked_work(
            connection,
            project_id=project_id,
            repository_binding_id=repository_binding_id,
            source_ref=source,
            target_ref=target,
            head_commit_sha=observation["pull_request"]["head_sha"],
            code_sync_run_id=code_sync_run_id,
            resolution_plan_id=resolution_plan_id,
        )

        authorization_id = make_id("ghmerge")
        rollback_plan = {
            "strategy": "create_revert_pull_request",
            "automatic": False,
            "requires_new_authorization": True,
            "target_ref": target,
            "restore_tree_from": observation["base"]["commit_sha"],
            "previous_target_sha": observation["base"]["commit_sha"],
            "merge_sha": None,
        }
        changeset_id = self._create_changeset(
            connection,
            project_id=project_id,
            authorization_id=authorization_id,
            repository=binding["full_name"],
            pull_request_number=pull_request_number,
            source_ref=source,
            target_ref=target,
            head_sha=observation["head"]["commit_sha"],
            base_sha=observation["base"]["commit_sha"],
            merge_method=method,
            policy_evaluation=evaluation,
            rollback_plan=rollback_plan,
            actor=created_by,
        )
        status = (
            "blocked"
            if evaluation["status"] == "blocked"
            else "pending_approval"
        )
        if status == "blocked":
            connection.execute(
                text(
                    """
                    UPDATE changesets
                    SET status = 'failed',
                        failure_reason = 'Branch policy denied the merge.',
                        updated_at = now()
                    WHERE id = :changeset_id
                    """
                ),
                {"changeset_id": changeset_id},
            )
        connection.execute(
            text(
                """
                INSERT INTO github_merge_authorizations (
                    id, project_id, repository_binding_id, branch_policy_id,
                    branch_policy_version, code_sync_run_id,
                    resolution_plan_id, pull_request_number, pull_request_url,
                    source_ref, target_ref, head_commit_sha, head_tree_sha,
                    base_commit_sha, base_tree_sha, merge_method, status,
                    dedupe_key, required_checks, observed_checks,
                    required_approvals, observed_reviews, evidence_bundle,
                    policy_evaluation, expires_at, changeset_id, receipt_ids,
                    rollback_plan, created_by
                )
                VALUES (
                    :id, :project_id, :repository_binding_id, :branch_policy_id,
                    :branch_policy_version, :code_sync_run_id,
                    :resolution_plan_id, :pull_request_number,
                    :pull_request_url, :source_ref, :target_ref,
                    :head_commit_sha, :head_tree_sha, :base_commit_sha,
                    :base_tree_sha, :merge_method, :status, :dedupe_key,
                    CAST(:required_checks AS JSONB),
                    CAST(:observed_checks AS JSONB), :required_approvals,
                    CAST(:observed_reviews AS JSONB),
                    CAST(:evidence_bundle AS JSONB),
                    CAST(:policy_evaluation AS JSONB), :expires_at,
                    :changeset_id, CAST(:receipt_ids AS JSONB),
                    CAST(:rollback_plan AS JSONB), :created_by
                )
                """
            ),
            {
                "id": authorization_id,
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
                "branch_policy_id": policy["id"],
                "branch_policy_version": policy["version"],
                "code_sync_run_id": code_sync_run_id,
                "resolution_plan_id": resolution_plan_id,
                "pull_request_number": pull_request_number,
                "pull_request_url": observation["pull_request"]["url"],
                "source_ref": source,
                "target_ref": target,
                "head_commit_sha": observation["head"]["commit_sha"],
                "head_tree_sha": observation["head"]["tree_sha"],
                "base_commit_sha": observation["base"]["commit_sha"],
                "base_tree_sha": observation["base"]["tree_sha"],
                "merge_method": method,
                "status": status,
                "dedupe_key": safe_dedupe_key,
                "required_checks": json.dumps(policy["required_checks"]),
                "observed_checks": json.dumps(observation["checks"]),
                "required_approvals": policy["required_approvals"],
                "observed_reviews": json.dumps(observation["reviews"]),
                "evidence_bundle": json.dumps(evidence),
                "policy_evaluation": json.dumps(evaluation),
                "expires_at": now_utc() + timedelta(seconds=expiry_seconds),
                "changeset_id": changeset_id,
                "receipt_ids": json.dumps([observation_receipt_id]),
                "rollback_plan": json.dumps(rollback_plan),
                "created_by": created_by,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.merge_authorization_requested",
            created_by,
            "github_merge_authorization",
            authorization_id,
            {
                "repository_binding_id": repository_binding_id,
                "pull_request_number": pull_request_number,
                "source_ref": source,
                "target_ref": target,
                "head_commit_sha": observation["head"]["commit_sha"],
                "base_commit_sha": observation["base"]["commit_sha"],
                "branch_policy_id": policy["id"],
                "branch_policy_version": policy["version"],
                "status": status,
                "changeset_id": changeset_id,
            },
        )
        return self.get(connection, project_id, authorization_id)

    def authorize(
        self,
        connection: Connection,
        *,
        project_id: str,
        authorization_id: str,
        review_evidence: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        row = self._locked(connection, project_id, authorization_id)
        reviewer = self._required_text(actor, "actor")
        evidence = dict(review_evidence or {})
        if reviewer == row["created_by"]:
            raise GitHubMergeAuthorizationStateError(
                "Merge authorization requires an independent approver."
            )
        if str(evidence.get("verdict") or "").lower() != "approved":
            raise ValueError("review_evidence.verdict must be approved.")
        if not str(evidence.get("summary") or "").strip():
            raise ValueError("review_evidence.summary must be non-empty.")
        if row["status"] != "pending_approval":
            raise GitHubMergeAuthorizationStateError(
                f"Merge authorization is {row['status']} and cannot be authorized."
            )
        if row["expires_at"] <= now_utc():
            self._mark_expired(connection, row, reviewer)
            return self.get(connection, project_id, authorization_id)
        policy = self.github_branches.active_policy_for_binding(
            connection, project_id, row["repository_binding_id"]
        )
        self._assert_policy_current(row, policy)
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'authorized', authorized_by = :actor,
                    authorized_at = now(),
                    evidence_bundle = evidence_bundle
                      || CAST(:review_evidence AS JSONB),
                    updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": authorization_id,
                "actor": reviewer,
                "review_evidence": json.dumps(
                    {"authorization_review": evidence}
                ),
            },
        )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'approved', approved_by = :actor,
                    approved_at = now(), updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": row["changeset_id"], "actor": reviewer},
        )
        emit_event(
            connection,
            project_id,
            "github.merge_authorization_approved",
            reviewer,
            "github_merge_authorization",
            authorization_id,
            {
                "head_commit_sha": row["head_commit_sha"],
                "base_commit_sha": row["base_commit_sha"],
                "expires_at": row["expires_at"].isoformat(),
            },
        )
        return self.get(connection, project_id, authorization_id)

    def enqueue(
        self,
        connection: Connection,
        *,
        project_id: str,
        authorization_id: str,
        actor: str,
    ) -> dict[str, Any]:
        row = self._locked(connection, project_id, authorization_id)
        queued_by = self._required_text(actor, "actor")
        if row["status"] == "queued":
            return self._serialize(row)
        if row["status"] != "authorized":
            raise GitHubMergeAuthorizationStateError(
                f"Merge authorization is {row['status']} and cannot be queued."
            )
        if row["expires_at"] <= now_utc():
            self._mark_expired(connection, row, queued_by)
            return self.get(connection, project_id, authorization_id)
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'queued', queued_at = now(), updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {"authorization_id": authorization_id},
        )
        emit_event(
            connection,
            project_id,
            "github.merge_authorization_queued",
            queued_by,
            "github_merge_authorization",
            authorization_id,
            {
                "queue_position": row["queue_position"],
                "target_ref": row["target_ref"],
            },
        )
        return self.get(connection, project_id, authorization_id)

    def apply(
        self,
        connection: Connection,
        *,
        project_id: str,
        authorization_id: str,
        lease_id: str,
        lease_token: str,
        fencing_token: int,
        actor: str,
    ) -> dict[str, Any]:
        row = self._locked(connection, project_id, authorization_id)
        applied_by = self._required_text(actor, "actor")
        if row["status"] == "merged":
            return self._serialize(row)
        if row["status"] not in {"queued", "failed"}:
            raise GitHubMergeAuthorizationStateError(
                f"Merge authorization is {row['status']} and cannot be applied."
            )
        if row["expires_at"] <= now_utc():
            self._mark_expired(connection, row, applied_by)
            return self.get(connection, project_id, authorization_id)
        if int(row["apply_attempts"]) >= self.MAX_APPLY_ATTEMPTS:
            raise GitHubMergeAuthorizationStateError(
                "Merge authorization exhausted its apply attempts."
            )
        self._assert_queue_head(connection, row)
        lease = self.github_branch_leases.verify(
            connection,
            project_id=project_id,
            lease_id=lease_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            repository_binding_id=row["repository_binding_id"],
            ref_name=row["target_ref"],
        )
        policy = self.github_branches.active_policy_for_binding(
            connection, project_id, row["repository_binding_id"]
        )
        self._assert_policy_current(row, policy)
        adapter, _, binding = self.github_connections.adapter_for_binding(
            connection,
            project_id=project_id,
            binding_id=row["repository_binding_id"],
        )

        attempt = int(row["apply_attempts"]) + 1
        observation, preflight_receipt_id = self._observe_provider(
            connection,
            project_id=project_id,
            binding=binding,
            adapter=adapter,
            pull_request_number=int(row["pull_request_number"]),
            dedupe_key=(
                f"github-merge:{authorization_id}:preflight:{attempt}"
            ),
            actor=applied_by,
        )
        self._append_receipt(
            connection, authorization_id, preflight_receipt_id
        )
        try:
            self._assert_fresh(row, observation)
            self._assert_merge_ready(row, observation)
        except GitHubMergeAuthorizationStaleError as error:
            self._block_stale(
                connection,
                row=row,
                observation=observation,
                error=error,
                actor=applied_by,
            )
            return self.get(connection, project_id, authorization_id)

        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'applying', apply_attempts = :attempt,
                    branch_lease_id = :lease_id,
                    fencing_token = :fencing_token,
                    observed_checks = CAST(:checks AS JSONB),
                    observed_reviews = CAST(:reviews AS JSONB),
                    applied_by = :actor, error = NULL, updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": authorization_id,
                "attempt": attempt,
                "lease_id": lease["id"],
                "fencing_token": lease["fencing_token"],
                "checks": json.dumps(observation["checks"]),
                "reviews": json.dumps(observation["reviews"]),
                "actor": applied_by,
            },
        )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'applying', applied_by = :actor, updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": row["changeset_id"], "actor": applied_by},
        )
        emit_event(
            connection,
            project_id,
            "github.merge_authorization_applying",
            applied_by,
            "github_merge_authorization",
            authorization_id,
            {
                "attempt": attempt,
                "branch_lease_id": lease["id"],
                "fencing_token": lease["fencing_token"],
            },
        )

        try:
            merge_result = self._provider_write(
                connection,
                project_id=project_id,
                authorization_id=authorization_id,
                binding=binding,
                changeset_id=row["changeset_id"],
                dedupe_key=f"github-merge:{authorization_id}:apply:{attempt}",
                payload={
                    "pull_request_number": row["pull_request_number"],
                    "expected_head_sha": row["head_commit_sha"],
                    "expected_base_sha": row["base_commit_sha"],
                    "source_ref": row["source_ref"],
                    "target_ref": row["target_ref"],
                    "merge_method": row["merge_method"],
                    "lease_id": lease["id"],
                    "fencing_token": lease["fencing_token"],
                    "force": False,
                },
                actor=applied_by,
                callback=lambda: adapter.merge_pull_request_guarded(
                    pull_number=int(row["pull_request_number"]),
                    expected_head_sha=row["head_commit_sha"],
                    expected_base_sha=row["base_commit_sha"],
                    expected_head_ref=row["source_ref"],
                    expected_base_ref=row["target_ref"],
                    merge_method=row["merge_method"],
                    commit_title=(
                        f"BOSS governed merge PR #{row['pull_request_number']}"
                    ),
                ),
            )
        except Exception as error:
            self._mark_failed(
                connection,
                row=row,
                attempt=attempt,
                error=error,
                actor=applied_by,
            )
            return self.get(connection, project_id, authorization_id)

        try:
            verification, verification_receipt_id = self._observe_provider(
                connection,
                project_id=project_id,
                binding=binding,
                adapter=adapter,
                pull_request_number=int(row["pull_request_number"]),
                dedupe_key=(
                    f"github-merge:{authorization_id}:verify:{attempt}"
                ),
                actor=applied_by,
            )
            self._append_receipt(
                connection, authorization_id, verification_receipt_id
            )
            if (
                verification["base_branch"]["commit_sha"]
                != merge_result["commit_sha"]
            ):
                raise GitHubMergeAuthorizationStaleError(
                    "GitHub target did not resolve to the reported merge commit."
                )
        except Exception as error:
            self._mark_unverified_merge(
                connection,
                row=row,
                merge_result=merge_result,
                error=error,
                actor=applied_by,
            )
            return self.get(connection, project_id, authorization_id)

        rollback_plan = {
            **dict(row["rollback_plan"] or {}),
            "merge_sha": merge_result["commit_sha"],
            "revert_candidate_parent_sha": row["base_commit_sha"],
        }
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'merged', result_merge_sha = :result_merge_sha,
                    result_target_tree_sha = :result_target_tree_sha,
                    rollback_plan = CAST(:rollback_plan AS JSONB),
                    error = NULL, merged_at = now(), updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": authorization_id,
                "result_merge_sha": merge_result["commit_sha"],
                "result_target_tree_sha": verification["base_branch"][
                    "tree_sha"
                ],
                "rollback_plan": json.dumps(rollback_plan),
            },
        )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'applied', applied_by = :actor,
                    applied_at = now(), failure_reason = NULL,
                    rollback_plan = CAST(:rollback_plan AS JSONB),
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {
                "changeset_id": row["changeset_id"],
                "actor": applied_by,
                "rollback_plan": json.dumps([rollback_plan]),
            },
        )
        emit_event(
            connection,
            project_id,
            "github.merge_authorization_merged",
            applied_by,
            "github_merge_authorization",
            authorization_id,
            {
                "pull_request_number": row["pull_request_number"],
                "merge_sha": merge_result["commit_sha"],
                "target_ref": row["target_ref"],
                "previous_target_sha": row["base_commit_sha"],
                "rollback_plan": rollback_plan,
            },
        )
        return self.get(connection, project_id, authorization_id)

    def list(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        target_ref: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        if target_ref:
            filters.append("target_ref = :target_ref")
            params["target_ref"] = GitHubBranchService._normalize_ref(target_ref)
        if status:
            filters.append("status = :status")
            params["status"] = self._required_text(status, "status")
        rows = connection.execute(
            text(
                f"""
                SELECT {self._columns()}
                FROM github_merge_authorizations
                WHERE {" AND ".join(filters)}
                ORDER BY queue_position
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize(row) for row in rows]

    def get(
        self,
        connection: Connection,
        project_id: str,
        authorization_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT {self._columns()}
                FROM github_merge_authorizations
                WHERE project_id = :project_id AND id = :authorization_id
                """
            ),
            {
                "project_id": project_id,
                "authorization_id": authorization_id,
            },
        ).mappings().first()
        if row is None:
            raise GitHubMergeAuthorizationNotFoundError(
                "GitHub merge authorization not found."
            )
        return self._serialize(row)

    def _observe_provider(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding: dict[str, Any],
        adapter: Any,
        pull_request_number: int,
        dedupe_key: str,
        actor: str,
    ) -> tuple[dict[str, Any], str]:
        scope = GitHubBranchService._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "github.merge_authorization.observe",
            {
                "repository_binding_id": binding["id"],
                "repository": binding["full_name"],
                "pull_request_number": pull_request_number,
            },
            {"operation": "observe_merge_candidate", "actor": actor},
            data_boundary="internal",
        )
        if not created:
            receipt_scope = receipt.get("effect_scope") or {}
            if (
                receipt.get("effect_kind")
                != "github.merge_authorization.observe"
                or receipt_scope.get("repository_binding_id") != binding["id"]
                or int(receipt_scope.get("pull_request_number") or 0)
                != pull_request_number
            ):
                raise GitHubMergeAuthorizationStateError(
                    "Observation dedupe key is bound to a different merge candidate."
                )
            if receipt["status"] == "succeeded":
                return dict(receipt.get("result") or {}), str(receipt["id"])
        try:
            pull = self._sanitize_pull_request(
                adapter.pull_request_snapshot(pull_request_number)
            )
            head = self._sanitize_commit(
                adapter.commit_identity(pull["head_sha"]),
                pull["head_sha"],
            )
            base = self._sanitize_commit(
                adapter.commit_identity(pull["base_sha"]),
                pull["base_sha"],
            )
            base_branch = self._sanitize_branch(
                adapter.branch_snapshot(pull["base_ref"]),
                pull["base_ref"],
            )
            checks = self._sanitize_checks(
                adapter.commit_checks(pull["head_sha"])
            )
            reviews = self._effective_reviews(
                adapter.pull_request_reviews(pull_request_number)
            )
            observation = {
                "pull_request": pull,
                "head": head,
                "base": base,
                "base_branch": base_branch,
                "checks": checks,
                "reviews": reviews,
            }
        except Exception as error:
            safe_error = {
                "type": error.__class__.__name__,
                "message": "GitHub merge candidate observation failed.",
            }
            self.receipts.mark_failed(connection, receipt["id"], safe_error)
            raise GitHubMergeAuthorizationStateError(
                safe_error["message"]
            ) from error
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            f"{binding['full_name']}#{pull_request_number}",
            observation,
        )
        return observation, str(receipt["id"])

    def _provider_write(
        self,
        connection: Connection,
        *,
        project_id: str,
        authorization_id: str,
        binding: dict[str, Any],
        changeset_id: str,
        dedupe_key: str,
        payload: dict[str, Any],
        actor: str,
        callback: Any,
    ) -> dict[str, Any]:
        operation = connection.execute(
            text(
                """
                SELECT id
                FROM changeset_operations
                WHERE changeset_id = :changeset_id AND operation_index = 0
                """
            ),
            {"changeset_id": changeset_id},
        ).mappings().one()
        existing = connection.execute(
            text(
                """
                SELECT id, changeset_id, operation_id, provider, action,
                       resource_ref, status, result
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
                or existing["action"] != "merge_pull_request"
                or existing["resource_ref"]
                != f"pull/{payload['pull_request_number']}"
            ):
                raise GitHubMergeAuthorizationStateError(
                    "Provider action dedupe key is bound to a different merge."
                )
            if existing["status"] == "succeeded":
                return dict(existing["result"] or {})
        scope = GitHubBranchService._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "github.merge_pull_request",
            {
                "repository_binding_id": binding["id"],
                "repository": binding["full_name"],
                "changeset_id": changeset_id,
                "operation_id": operation["id"],
                "authorization_id": authorization_id,
                "resource_ref": f"pull/{payload['pull_request_number']}",
            },
            payload,
            data_boundary="internal",
        )
        if not created:
            receipt_scope = receipt.get("effect_scope") or {}
            if (
                receipt.get("effect_kind") != "github.merge_pull_request"
                or receipt_scope.get("authorization_id") != authorization_id
                or receipt_scope.get("operation_id") != operation["id"]
            ):
                raise GitHubMergeAuthorizationStateError(
                    "Receipt dedupe key is bound to a different GitHub merge."
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
                        :receipt_id, :dedupe_key, 'github',
                        'merge_pull_request', 'github_pull_request',
                        :resource_ref, 'in_progress', :request_hash
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
                    "resource_ref": f"pull/{payload['pull_request_number']}",
                    "request_hash": json_hash(payload),
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE github_merge_authorizations
                    SET receipt_ids = receipt_ids || CAST(:receipt AS JSONB),
                        resource_receipt_ids =
                          resource_receipt_ids || CAST(:resource_receipt AS JSONB),
                        updated_at = now()
                    WHERE id = :authorization_id
                    """
                ),
                {
                    "authorization_id": authorization_id,
                    "receipt": json.dumps([receipt["id"]]),
                    "resource_receipt": json.dumps([resource_receipt_id]),
                },
            )
        try:
            result = callback()
        except Exception as error:
            safe_error = {
                "type": error.__class__.__name__,
                "message": "GitHub protected merge failed.",
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
                    "action": "merge_pull_request",
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
                "provider_ref": result.get("commit_sha"),
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
                "action": "merge_pull_request",
                "provider_ref": result.get("commit_sha"),
            },
        )
        return result

    def _create_changeset(
        self,
        connection: Connection,
        *,
        project_id: str,
        authorization_id: str,
        repository: str,
        pull_request_number: int,
        source_ref: str,
        target_ref: str,
        head_sha: str,
        base_sha: str,
        merge_method: str,
        policy_evaluation: dict[str, Any],
        rollback_plan: dict[str, Any],
        actor: str,
    ) -> str:
        changeset_id = make_id("chg")
        operation = {
            "skill_id": "github.merge_pull_request",
            "action": "merge_pull_request",
            "resource_ref": f"pull/{pull_request_number}",
            "desired_state": {
                "repository": repository,
                "source_ref": source_ref,
                "target_ref": target_ref,
                "expected_head_sha": head_sha,
                "expected_base_sha": base_sha,
                "merge_method": merge_method,
                "force": False,
            },
        }
        connection.execute(
            text(
                """
                INSERT INTO changesets (
                    id, project_id, title, summary, status, risk_tier,
                    operations, expected_cost_usd_micros, cost_quote,
                    rollback_plan, expected_receipts, policy_result, created_by
                )
                VALUES (
                    :id, :project_id, :title, :summary, 'pending_approval', 3,
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
                    f"Governed merge {source_ref} -> {target_ref} "
                    f"(PR #{pull_request_number})"
                ),
                "summary": (
                    f"Exact-snapshot merge authorization {authorization_id}; "
                    "protected target, no direct push and no force update."
                ),
                "operations": json.dumps([operation]),
                "cost_quote": json.dumps(
                    {
                        "amount_usd_micros": 0,
                        "reservation_required": False,
                    }
                ),
                "rollback_plan": json.dumps([rollback_plan]),
                "expected_receipts": json.dumps(
                    [
                        {
                            "provider": "github",
                            "action": "merge_pull_request",
                            "resource_ref": f"pull/{pull_request_number}",
                        }
                    ]
                ),
                "policy_result": json.dumps(policy_evaluation),
                "created_by": actor,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO changeset_operations (
                    id, project_id, changeset_id, operation_index, skill_id,
                    action, resource_type, resource_ref, desired_state,
                    risk_tier, destructive, production_affecting
                )
                VALUES (
                    :id, :project_id, :changeset_id, 0,
                    'github.merge_pull_request', 'merge_pull_request',
                    'github_pull_request', :resource_ref,
                    CAST(:desired_state AS JSONB), 3, false, true
                )
                """
            ),
            {
                "id": make_id("chgo"),
                "project_id": project_id,
                "changeset_id": changeset_id,
                "resource_ref": f"pull/{pull_request_number}",
                "desired_state": json.dumps(operation["desired_state"]),
            },
        )
        return changeset_id

    def _assert_linked_work(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        source_ref: str,
        target_ref: str,
        head_commit_sha: str,
        code_sync_run_id: Optional[str],
        resolution_plan_id: Optional[str],
    ) -> None:
        if code_sync_run_id:
            run = connection.execute(
                text(
                    """
                    SELECT repository_binding_id, source_ref, target_ref,
                           resolution_branch, status
                    FROM github_code_sync_runs
                    WHERE project_id = :project_id AND id = :run_id
                    """
                ),
                {"project_id": project_id, "run_id": code_sync_run_id},
            ).mappings().first()
            if run is None:
                raise GitHubMergeAuthorizationStateError(
                    "Linked code sync run was not found."
                )
            expected_source = run["resolution_branch"] or run["source_ref"]
            if (
                run["repository_binding_id"] != repository_binding_id
                or expected_source != source_ref
                or run["target_ref"] != target_ref
                or run["status"] not in {"succeeded", "conflicted"}
            ):
                raise GitHubMergeAuthorizationStateError(
                    "Linked code sync run does not match the merge candidate."
                )
        if resolution_plan_id:
            plan = connection.execute(
                text(
                    """
                    SELECT p.status, p.result_commit_sha,
                           c.repository_binding_id, c.resolution_branch
                    FROM github_resolution_plans p
                    JOIN github_conflict_sets c ON c.id = p.conflict_set_id
                    WHERE p.project_id = :project_id AND p.id = :plan_id
                    """
                ),
                {"project_id": project_id, "plan_id": resolution_plan_id},
            ).mappings().first()
            if plan is None:
                raise GitHubMergeAuthorizationStateError(
                    "Linked resolution plan was not found."
                )
            if (
                plan["status"] != "applied"
                or plan["result_commit_sha"] != head_commit_sha
                or plan["repository_binding_id"] != repository_binding_id
                or plan["resolution_branch"] != source_ref
            ):
                raise GitHubMergeAuthorizationStateError(
                    "Linked resolution plan does not match the exact PR head."
                )

    @staticmethod
    def _assert_requested_snapshot(
        observation: dict[str, Any],
        *,
        binding: dict[str, Any],
        pull_request_number: int,
        source_ref: str,
        target_ref: str,
    ) -> None:
        pull = observation["pull_request"]
        if pull["number"] != pull_request_number:
            raise GitHubMergeAuthorizationStateError(
                "GitHub returned a different pull request."
            )
        if pull["state"] != "open":
            raise GitHubMergeAuthorizationStateError(
                "Only open pull requests can be authorized."
            )
        if pull["draft"]:
            raise GitHubMergeAuthorizationStateError(
                "Draft pull requests cannot be authorized."
            )
        if (
            pull["head_ref"] != source_ref
            or pull["base_ref"] != target_ref
            or pull["base_repository"].lower() != binding["full_name"].lower()
        ):
            raise GitHubMergeAuthorizationStateError(
                "Pull request refs or base repository do not match the request."
            )
        if observation["base_branch"]["commit_sha"] != pull["base_sha"]:
            raise GitHubMergeAuthorizationStaleError(
                "Pull request base is not the current target head."
            )

    @staticmethod
    def _assert_policy_current(row: Any, policy: dict[str, Any]) -> None:
        if (
            policy["id"] != row["branch_policy_id"]
            or int(policy["version"]) != int(row["branch_policy_version"])
        ):
            raise GitHubMergeAuthorizationStaleError(
                "Branch policy changed after merge authorization."
            )

    def _assert_fresh(
        self, row: Any, observation: dict[str, Any]
    ) -> None:
        pull = observation["pull_request"]
        expected = {
            "head_ref": row["source_ref"],
            "base_ref": row["target_ref"],
            "head_sha": row["head_commit_sha"],
            "base_sha": row["base_commit_sha"],
        }
        for field, value in expected.items():
            if pull[field] != value:
                raise GitHubMergeAuthorizationStaleError(
                    "Pull request changed after merge authorization."
                )
        if (
            observation["head"]["tree_sha"] != row["head_tree_sha"]
            or observation["base"]["tree_sha"] != row["base_tree_sha"]
            or observation["base_branch"]["commit_sha"]
            != row["base_commit_sha"]
            or observation["base_branch"]["tree_sha"] != row["base_tree_sha"]
        ):
            raise GitHubMergeAuthorizationStaleError(
                "Branch commit or tree identity changed after authorization."
            )

    def _assert_merge_ready(
        self, row: Any, observation: dict[str, Any]
    ) -> None:
        pull = observation["pull_request"]
        if pull["state"] != "open" or pull["draft"]:
            raise GitHubMergeAuthorizationStaleError(
                "Pull request is no longer an open, ready candidate."
            )
        if pull["mergeable"] is not True:
            raise GitHubMergeAuthorizationStaleError(
                "Pull request is not currently mergeable."
            )
        required_checks = list(row["required_checks"] or [])
        checks = {
            str(item.get("name") or ""): item
            for item in observation["checks"]
        }
        missing_or_failing = []
        for name in required_checks:
            check = checks.get(name)
            if (
                not check
                or check.get("status") != "completed"
                or check.get("conclusion")
                not in self._PASSING_CHECK_CONCLUSIONS
            ):
                missing_or_failing.append(name)
        if missing_or_failing:
            raise GitHubMergeAuthorizationStaleError(
                "Required checks are missing, pending, or failing."
            )
        reviews = observation["reviews"]
        if any(item["state"] == "CHANGES_REQUESTED" for item in reviews):
            raise GitHubMergeAuthorizationStaleError(
                "A current review requests changes."
            )
        approvals = {
            item["reviewer"]
            for item in reviews
            if item["state"] == "APPROVED"
            and item["commit_sha"] == row["head_commit_sha"]
        }
        if len(approvals) < int(row["required_approvals"]):
            raise GitHubMergeAuthorizationStaleError(
                "Required current-head approvals are missing."
            )

    def _assert_queue_head(self, connection: Connection, row: Any) -> None:
        earlier = connection.execute(
            text(
                """
                SELECT id
                FROM github_merge_authorizations
                WHERE repository_binding_id = :repository_binding_id
                  AND target_ref = :target_ref
                  AND queue_position < :queue_position
                  AND status IN ('queued', 'applying', 'failed')
                ORDER BY queue_position
                LIMIT 1
                """
            ),
            {
                "repository_binding_id": row["repository_binding_id"],
                "target_ref": row["target_ref"],
                "queue_position": row["queue_position"],
            },
        ).scalar_one_or_none()
        if earlier:
            raise GitHubMergeAuthorizationStateError(
                "A prior merge authorization owns the target queue."
            )

    def _block_stale(
        self,
        connection: Connection,
        *,
        row: Any,
        observation: dict[str, Any],
        error: Exception,
        actor: str,
    ) -> None:
        safe_error = {
            "type": "StaleMergeAuthorization",
            "message": str(error),
        }
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'blocked', error = CAST(:error AS JSONB),
                    observed_checks = CAST(:checks AS JSONB),
                    observed_reviews = CAST(:reviews AS JSONB),
                    updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": row["id"],
                "error": json.dumps(safe_error),
                "checks": json.dumps(observation["checks"]),
                "reviews": json.dumps(observation["reviews"]),
            },
        )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'failed', failure_reason = :reason,
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": row["changeset_id"], "reason": safe_error["message"]},
        )
        emit_event(
            connection,
            row["project_id"],
            "github.merge_authorization_blocked",
            actor,
            "github_merge_authorization",
            row["id"],
            safe_error,
        )

    def _mark_failed(
        self,
        connection: Connection,
        *,
        row: Any,
        attempt: int,
        error: Exception,
        actor: str,
    ) -> None:
        deadlettered = attempt >= self.MAX_APPLY_ATTEMPTS
        status = "deadlettered" if deadlettered else "failed"
        safe_error = {
            "type": error.__class__.__name__,
            "message": "GitHub protected merge failed.",
        }
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = :status, error = CAST(:error AS JSONB),
                    updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": row["id"],
                "status": status,
                "error": json.dumps(safe_error),
            },
        )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'failed', failure_reason = :reason,
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {
                "changeset_id": row["changeset_id"],
                "reason": safe_error["message"],
            },
        )
        if deadlettered:
            self._create_deadletter(
                connection,
                project_id=row["project_id"],
                source_id=row["id"],
                reason="github_protected_merge_failed",
                payload={"attempt": attempt, "error": safe_error},
            )
        emit_event(
            connection,
            row["project_id"],
            (
                "github.merge_authorization_deadlettered"
                if deadlettered
                else "github.merge_authorization_failed"
            ),
            actor,
            "github_merge_authorization",
            row["id"],
            {"attempt": attempt, "error": safe_error},
        )

    def _mark_unverified_merge(
        self,
        connection: Connection,
        *,
        row: Any,
        merge_result: dict[str, Any],
        error: Exception,
        actor: str,
    ) -> None:
        safe_error = {
            "type": "MergeResultVerificationFailed",
            "message": "GitHub accepted the merge, but result verification failed.",
        }
        rollback_plan = {
            **dict(row["rollback_plan"] or {}),
            "merge_sha": merge_result.get("commit_sha"),
            "verification_error": safe_error,
        }
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'blocked', result_merge_sha = :result_merge_sha,
                    rollback_plan = CAST(:rollback_plan AS JSONB),
                    error = CAST(:error AS JSONB), updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": row["id"],
                "result_merge_sha": merge_result.get("commit_sha"),
                "rollback_plan": json.dumps(rollback_plan),
                "error": json.dumps(safe_error),
            },
        )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'applied', applied_by = :actor,
                    applied_at = now(), failure_reason = :reason,
                    rollback_plan = CAST(:rollback_plan AS JSONB),
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {
                "changeset_id": row["changeset_id"],
                "actor": actor,
                "reason": safe_error["message"],
                "rollback_plan": json.dumps([rollback_plan]),
            },
        )
        self._create_deadletter(
            connection,
            project_id=row["project_id"],
            source_id=row["id"],
            reason="github_merge_result_verification_failed",
            payload={
                "error": safe_error,
                "merge_sha": merge_result.get("commit_sha"),
                "provider_error_type": error.__class__.__name__,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "github.merge_result_verification_failed",
            actor,
            "github_merge_authorization",
            row["id"],
            {
                "error": safe_error,
                "merge_sha": merge_result.get("commit_sha"),
                "rollback_plan": rollback_plan,
            },
        )

    @staticmethod
    def _create_deadletter(
        connection: Connection,
        *,
        project_id: str,
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
                  AND source_kind = 'github_merge_authorization'
                  AND source_id = :source_id
                  AND status = 'open'
                """
            ),
            {"project_id": project_id, "source_id": source_id},
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
                    :id, :project_id, 'github_merge_authorization', :source_id,
                    :reason, CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "id": make_id("dlq"),
                "project_id": project_id,
                "source_id": source_id,
                "reason": reason,
                "payload": json.dumps(payload),
            },
        )

    @staticmethod
    def _append_receipt(
        connection: Connection, authorization_id: str, receipt_id: str
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET receipt_ids = CASE
                      WHEN receipt_ids ? :receipt_id THEN receipt_ids
                      ELSE receipt_ids || CAST(:receipt AS JSONB)
                    END,
                    updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {
                "authorization_id": authorization_id,
                "receipt_id": receipt_id,
                "receipt": json.dumps([receipt_id]),
            },
        )

    @staticmethod
    def _mark_expired(
        connection: Connection, row: Any, actor: str
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE github_merge_authorizations
                SET status = 'expired', updated_at = now()
                WHERE id = :authorization_id
                """
            ),
            {"authorization_id": row["id"]},
        )
        emit_event(
            connection,
            row["project_id"],
            "github.merge_authorization_expired",
            actor,
            "github_merge_authorization",
            row["id"],
            {"expires_at": row["expires_at"].isoformat()},
        )

    def _locked(
        self,
        connection: Connection,
        project_id: str,
        authorization_id: str,
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._columns()}
                FROM github_merge_authorizations
                WHERE project_id = :project_id AND id = :authorization_id
                FOR UPDATE
                """
            ),
            {
                "project_id": project_id,
                "authorization_id": authorization_id,
            },
        ).mappings().first()
        if row is None:
            raise GitHubMergeAuthorizationNotFoundError(
                "GitHub merge authorization not found."
            )
        return row

    @classmethod
    def _sanitize_pull_request(cls, value: dict[str, Any]) -> dict[str, Any]:
        required = ("number", "head_ref", "head_sha", "base_ref", "base_sha")
        for field in required:
            if value.get(field) in {None, ""}:
                raise ValueError(f"GitHub pull request is missing {field}.")
        head_sha = str(value["head_sha"])
        base_sha = str(value["base_sha"])
        if not cls._SHA.fullmatch(head_sha) or not cls._SHA.fullmatch(base_sha):
            raise ValueError("GitHub pull request returned an invalid commit SHA.")
        return {
            "number": int(value["number"]),
            "url": str(value.get("url") or ""),
            "state": str(value.get("state") or "").lower(),
            "draft": bool(value.get("draft", False)),
            "mergeable": value.get("mergeable"),
            "mergeable_state": str(value.get("mergeable_state") or ""),
            "head_ref": GitHubBranchService._normalize_ref(value["head_ref"]),
            "head_sha": head_sha.lower(),
            "head_repository": str(value.get("head_repository") or ""),
            "base_ref": GitHubBranchService._normalize_ref(value["base_ref"]),
            "base_sha": base_sha.lower(),
            "base_repository": str(value.get("base_repository") or ""),
            "updated_at": value.get("updated_at"),
        }

    @classmethod
    def _sanitize_commit(
        cls, value: dict[str, Any], expected_sha: str
    ) -> dict[str, Any]:
        commit_sha = str(value.get("commit_sha") or "").lower()
        tree_sha = str(value.get("tree_sha") or "").lower()
        if (
            commit_sha != expected_sha.lower()
            or not cls._SHA.fullmatch(commit_sha)
            or not cls._SHA.fullmatch(tree_sha)
        ):
            raise ValueError("GitHub returned an invalid commit identity.")
        return {"commit_sha": commit_sha, "tree_sha": tree_sha}

    @classmethod
    def _sanitize_branch(
        cls, value: dict[str, Any], expected_ref: str
    ) -> dict[str, Any]:
        if not value.get("exists"):
            raise ValueError("GitHub target branch is missing.")
        commit = cls._sanitize_commit(value, str(value.get("commit_sha") or ""))
        return {
            "ref": expected_ref,
            **commit,
            "protected": bool(value.get("protected", False)),
        }

    @staticmethod
    def _sanitize_checks(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        checks = []
        for value in values[:100]:
            name = str(value.get("name") or "").strip()
            if not name:
                continue
            checks.append(
                {
                    "name": name[:255],
                    "status": str(value.get("status") or "").lower()[:64],
                    "conclusion": (
                        str(value.get("conclusion") or "").lower()[:64] or None
                    ),
                    "app": str(value.get("app") or "")[:255] or None,
                }
            )
        return checks

    @staticmethod
    def _effective_reviews(
        values: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        ordered = sorted(
            values[:100],
            key=lambda item: (
                str(item.get("submitted_at") or ""),
                int(item.get("id") or 0),
            ),
        )
        for value in ordered:
            reviewer = str(value.get("reviewer") or "").strip()
            state = str(value.get("state") or "").upper()
            if not reviewer or state not in {
                "APPROVED",
                "CHANGES_REQUESTED",
                "DISMISSED",
            }:
                continue
            latest[reviewer.lower()] = {
                "id": int(value.get("id") or 0),
                "reviewer": reviewer,
                "state": state,
                "commit_sha": str(value.get("commit_sha") or "").lower(),
                "submitted_at": value.get("submitted_at"),
            }
        return sorted(latest.values(), key=lambda item: item["reviewer"].lower())

    @staticmethod
    def _validate_evidence_bundle(
        value: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = dict(value or {})
        refs = evidence.get("evidence_refs")
        if not isinstance(refs, list) or not refs or len(refs) > 100:
            raise ValueError(
                "evidence_bundle.evidence_refs must contain between 1 and 100 references."
            )
        normalized = []
        for ref in refs:
            safe_ref = str(ref or "").strip()
            if not safe_ref or len(safe_ref) > 2048:
                raise ValueError("Evidence references must be non-empty and bounded.")
            normalized.append(safe_ref)
        evidence["evidence_refs"] = normalized
        return evidence

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty.")
        if "\x00" in normalized:
            raise ValueError(f"{field} contains an invalid character.")
        return normalized

    @staticmethod
    def _columns() -> str:
        return """
            id, project_id, repository_binding_id, branch_policy_id,
            branch_policy_version, code_sync_run_id, resolution_plan_id,
            pull_request_number, pull_request_url, source_ref, target_ref,
            head_commit_sha, head_tree_sha, base_commit_sha, base_tree_sha,
            merge_method, status, dedupe_key, required_checks, observed_checks,
            required_approvals, observed_reviews, evidence_bundle,
            policy_evaluation, authorized_by, authorized_at, expires_at,
            queue_position, queued_at, branch_lease_id, fencing_token,
            changeset_id, apply_attempts, receipt_ids, resource_receipt_ids,
            result_merge_sha, result_target_tree_sha, rollback_plan, error,
            created_by, applied_by, created_at, updated_at, merged_at
        """

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "branch_policy_id": row["branch_policy_id"],
            "branch_policy_version": row["branch_policy_version"],
            "code_sync_run_id": row["code_sync_run_id"],
            "resolution_plan_id": row["resolution_plan_id"],
            "pull_request_number": row["pull_request_number"],
            "pull_request_url": row["pull_request_url"],
            "source_ref": row["source_ref"],
            "target_ref": row["target_ref"],
            "head_commit_sha": row["head_commit_sha"],
            "head_tree_sha": row["head_tree_sha"],
            "base_commit_sha": row["base_commit_sha"],
            "base_tree_sha": row["base_tree_sha"],
            "merge_method": row["merge_method"],
            "status": row["status"],
            "dedupe_key": row["dedupe_key"],
            "required_checks": list(row["required_checks"] or []),
            "observed_checks": list(row["observed_checks"] or []),
            "required_approvals": row["required_approvals"],
            "observed_reviews": list(row["observed_reviews"] or []),
            "evidence_bundle": row["evidence_bundle"] or {},
            "policy_evaluation": row["policy_evaluation"] or {},
            "authorized_by": row["authorized_by"],
            "authorized_at": (
                row["authorized_at"].isoformat()
                if row["authorized_at"]
                else None
            ),
            "expires_at": row["expires_at"].isoformat(),
            "queue_position": row["queue_position"],
            "queued_at": (
                row["queued_at"].isoformat() if row["queued_at"] else None
            ),
            "branch_lease_id": row["branch_lease_id"],
            "fencing_token": row["fencing_token"],
            "changeset_id": row["changeset_id"],
            "apply_attempts": row["apply_attempts"],
            "receipt_ids": list(row["receipt_ids"] or []),
            "resource_receipt_ids": list(
                row["resource_receipt_ids"] or []
            ),
            "result_merge_sha": row["result_merge_sha"],
            "result_target_tree_sha": row["result_target_tree_sha"],
            "rollback_plan": row["rollback_plan"] or {},
            "error": row["error"],
            "created_by": row["created_by"],
            "applied_by": row["applied_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "merged_at": (
                row["merged_at"].isoformat() if row["merged_at"] else None
            ),
        }
