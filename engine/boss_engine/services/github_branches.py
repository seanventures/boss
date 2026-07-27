from __future__ import annotations

from fnmatch import fnmatchcase
import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .github_connections import (
    GitHubConnectionService,
    GitHubConnectionStateError,
)
from .receipts import ReceiptService


class GitHubBranchPolicyNotFoundError(RuntimeError):
    pass


class GitHubBranchSnapshotNotFoundError(RuntimeError):
    pass


class GitHubBranchSnapshotError(RuntimeError):
    pass


class GitHubBranchService:
    """Versioned branch policy and immutable remote-topology evidence."""

    POLICY_STATUSES = {"active", "disabled"}
    UPDATE_METHODS = {"fast_forward", "merge_commit", "rebase", "force_push"}
    MERGE_METHODS = {"merge", "squash", "rebase"}
    OPERATIONS = {"observe", "create", "update", "merge", "delete"}
    TOPOLOGY_CLASSIFICATIONS = {
        "equal",
        "tree_equivalent_rewrite",
        "fast_forward",
        "behind",
        "diverged",
        "missing",
    }
    _SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")

    def __init__(
        self,
        settings: Settings,
        *,
        github_connections: Optional[GitHubConnectionService] = None,
    ) -> None:
        self.settings = settings
        self.github_connections = github_connections or GitHubConnectionService(
            settings
        )
        self.receipts = ReceiptService()

    def list_policies(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        if not include_history:
            filters.append("status IN ('active', 'disabled')")
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, repository_binding_id, version,
                       supersedes_policy_id, status, protected_ref_patterns,
                       allowed_branch_prefixes, branch_name_pattern,
                       allowed_update_methods, allowed_merge_methods,
                       required_checks, required_approvals,
                       require_pull_request, require_linear_history,
                       require_signed_commits, automation_updates_enabled,
                       force_push_allowed, metadata, created_by, created_at,
                       updated_at
                FROM github_branch_policies
                WHERE {" AND ".join(filters)}
                ORDER BY repository_binding_id, version DESC
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_policy(row) for row in rows]

    def get_policy(
        self,
        connection: Connection,
        project_id: str,
        policy_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, repository_binding_id, version,
                       supersedes_policy_id, status, protected_ref_patterns,
                       allowed_branch_prefixes, branch_name_pattern,
                       allowed_update_methods, allowed_merge_methods,
                       required_checks, required_approvals,
                       require_pull_request, require_linear_history,
                       require_signed_commits, automation_updates_enabled,
                       force_push_allowed, metadata, created_by, created_at,
                       updated_at
                FROM github_branch_policies
                WHERE project_id = :project_id AND id = :policy_id
                """
            ),
            {"project_id": project_id, "policy_id": policy_id},
        ).mappings().first()
        if row is None:
            raise GitHubBranchPolicyNotFoundError("GitHub branch policy not found.")
        return self._serialize_policy(row)

    def active_policy_for_binding(
        self,
        connection: Connection,
        project_id: str,
        repository_binding_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id
                FROM github_branch_policies
                WHERE project_id = :project_id
                  AND repository_binding_id = :repository_binding_id
                  AND status = 'active'
                """
            ),
            {
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
            },
        ).scalar_one_or_none()
        if row is None:
            raise GitHubBranchPolicyNotFoundError(
                "Repository has no active GitHub branch policy."
            )
        return self.get_policy(connection, project_id, str(row))

    def create_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        protected_ref_patterns: list[str],
        allowed_branch_prefixes: list[str],
        branch_name_pattern: str,
        allowed_update_methods: list[str],
        allowed_merge_methods: list[str],
        required_checks: list[str],
        required_approvals: int,
        require_pull_request: bool,
        require_linear_history: bool,
        require_signed_commits: bool,
        automation_updates_enabled: bool,
        force_push_allowed: bool,
        metadata: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        binding = self.github_connections.get_repository_binding(
            connection, project_id, repository_binding_id
        )
        if binding["status"] != "active":
            raise GitHubConnectionStateError("GitHub repository binding is disabled.")
        existing_policy = connection.execute(
            text(
                """
                SELECT id
                FROM github_branch_policies
                WHERE project_id = :project_id
                  AND repository_binding_id = :repository_binding_id
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
            },
        ).first()
        if existing_policy:
            raise ValueError(
                "Repository already has branch policy history; revise its latest version."
            )
        values = self._validate_policy_values(
            protected_ref_patterns=protected_ref_patterns,
            allowed_branch_prefixes=allowed_branch_prefixes,
            branch_name_pattern=branch_name_pattern,
            allowed_update_methods=allowed_update_methods,
            allowed_merge_methods=allowed_merge_methods,
            required_checks=required_checks,
            required_approvals=required_approvals,
            metadata=metadata,
        )
        return self._insert_policy(
            connection,
            project_id=project_id,
            repository_binding_id=repository_binding_id,
            version=1,
            supersedes_policy_id=None,
            status="active",
            require_pull_request=require_pull_request,
            require_linear_history=require_linear_history,
            require_signed_commits=require_signed_commits,
            automation_updates_enabled=automation_updates_enabled,
            force_push_allowed=force_push_allowed,
            actor=actor,
            **values,
        )

    def revise_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        policy_id: str,
        updates: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        existing = self.get_policy(connection, project_id, policy_id)
        if existing["status"] == "superseded":
            raise ValueError("A superseded branch policy cannot be revised.")
        status = str(updates.get("status") or "active").strip().lower()
        if status not in self.POLICY_STATUSES:
            raise ValueError("status must be active or disabled.")
        values = self._validate_policy_values(
            protected_ref_patterns=updates.get(
                "protected_ref_patterns", existing["protected_ref_patterns"]
            ),
            allowed_branch_prefixes=updates.get(
                "allowed_branch_prefixes", existing["allowed_branch_prefixes"]
            ),
            branch_name_pattern=updates.get(
                "branch_name_pattern", existing["branch_name_pattern"]
            ),
            allowed_update_methods=updates.get(
                "allowed_update_methods", existing["allowed_update_methods"]
            ),
            allowed_merge_methods=updates.get(
                "allowed_merge_methods", existing["allowed_merge_methods"]
            ),
            required_checks=updates.get(
                "required_checks", existing["required_checks"]
            ),
            required_approvals=updates.get(
                "required_approvals", existing["required_approvals"]
            ),
            metadata=updates.get("metadata", existing["metadata"]),
        )
        connection.execute(
            text(
                """
                UPDATE github_branch_policies
                SET status = 'superseded', updated_at = now()
                WHERE project_id = :project_id AND id = :policy_id
                """
            ),
            {"project_id": project_id, "policy_id": policy_id},
        )
        return self._insert_policy(
            connection,
            project_id=project_id,
            repository_binding_id=existing["repository_binding_id"],
            version=existing["version"] + 1,
            supersedes_policy_id=existing["id"],
            status=status,
            require_pull_request=updates.get(
                "require_pull_request", existing["require_pull_request"]
            ),
            require_linear_history=updates.get(
                "require_linear_history", existing["require_linear_history"]
            ),
            require_signed_commits=updates.get(
                "require_signed_commits", existing["require_signed_commits"]
            ),
            automation_updates_enabled=updates.get(
                "automation_updates_enabled",
                existing["automation_updates_enabled"],
            ),
            force_push_allowed=updates.get(
                "force_push_allowed", existing["force_push_allowed"]
            ),
            actor=actor,
            **values,
        )

    def evaluate_policy(
        self,
        policy: dict[str, Any],
        *,
        ref: str,
        operation: str,
        method: Optional[str],
    ) -> dict[str, Any]:
        branch = self._normalize_ref(ref)
        normalized_operation = operation.strip().lower()
        normalized_method = method.strip().lower() if method else None
        reasons: list[str] = []
        if normalized_operation not in self.OPERATIONS:
            raise ValueError(
                "operation must be one of: "
                + ", ".join(sorted(self.OPERATIONS))
                + "."
            )
        protected = any(
            fnmatchcase(branch, pattern)
            for pattern in policy["protected_ref_patterns"]
        )
        if not self._valid_branch_name(branch, policy["branch_name_pattern"]):
            reasons.append("branch_name_denied")
        if (
            normalized_operation in {"create", "update"}
            and not protected
            and not any(
                branch.startswith(prefix)
                for prefix in policy["allowed_branch_prefixes"]
            )
        ):
            reasons.append("branch_prefix_denied")
        if normalized_operation == "update":
            if normalized_method not in policy["allowed_update_methods"]:
                reasons.append("update_method_denied")
            if normalized_method == "force_push" and (
                not policy["force_push_allowed"] or protected
            ):
                reasons.append("force_push_denied")
        if normalized_operation == "merge":
            if normalized_method not in policy["allowed_merge_methods"]:
                reasons.append("merge_method_denied")
        if normalized_operation == "delete" and protected:
            reasons.append("protected_ref_delete_denied")
        if (
            protected
            and normalized_operation in {"create", "update", "merge", "delete"}
            and not policy["automation_updates_enabled"]
        ):
            reasons.append("protected_ref_automation_disabled")

        blocking_reasons = sorted(set(reasons))
        approvals_required = (
            normalized_operation in {"update", "merge"}
            and (
                protected
                or policy["require_pull_request"]
                or policy["required_approvals"] > 0
                or bool(policy["required_checks"])
            )
        )
        if blocking_reasons:
            status = "blocked"
        elif approvals_required:
            status = "approval_required"
        else:
            status = "allowed"
        return {
            "policy_id": policy["id"],
            "policy_version": policy["version"],
            "status": status,
            "ref": branch,
            "operation": normalized_operation,
            "method": normalized_method,
            "protected": protected,
            "reasons": blocking_reasons,
            "required_checks": policy["required_checks"],
            "required_approvals": policy["required_approvals"],
            "require_pull_request": policy["require_pull_request"],
            "require_linear_history": policy["require_linear_history"],
            "require_signed_commits": policy["require_signed_commits"],
        }

    def capture_snapshot(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        source_ref: str,
        target_ref: str,
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        source = self._normalize_ref(source_ref)
        target = self._normalize_ref(target_ref)
        if source == target:
            raise ValueError("source_ref and target_ref must be different.")
        policy = self.active_policy_for_binding(
            connection, project_id, repository_binding_id
        )
        adapter, _, binding = self.github_connections.adapter_for_binding(
            connection,
            project_id=project_id,
            binding_id=repository_binding_id,
        )
        scope = self._scope(connection, project_id)
        safe_dedupe_key = self._required_text(dedupe_key, "dedupe_key")
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            safe_dedupe_key,
            "github.repository.snapshot",
            {
                "repository_binding_id": repository_binding_id,
                "repository": binding["full_name"],
                "source_ref": source,
                "target_ref": target,
            },
            {
                "operation": "capture_branch_snapshot",
                "branch_policy_id": policy["id"],
                "branch_policy_version": policy["version"],
            },
            data_boundary="internal",
        )
        if not created:
            receipt_scope = receipt.get("effect_scope") or {}
            if (
                receipt.get("project_id") != project_id
                or receipt.get("effect_kind") != "github.repository.snapshot"
                or receipt_scope.get("repository_binding_id")
                != repository_binding_id
                or receipt_scope.get("source_ref") != source
                or receipt_scope.get("target_ref") != target
            ):
                raise ValueError(
                    "dedupe_key is already bound to a different GitHub snapshot."
                )
        if not created and receipt["status"] == "succeeded":
            snapshot_id = (receipt.get("result") or {}).get("snapshot_id")
            if snapshot_id:
                return {
                    "status": "succeeded",
                    "duplicate": True,
                    "receipt_id": receipt["id"],
                    "snapshot": self.get_snapshot(
                        connection, project_id, str(snapshot_id)
                    ),
                    "error": None,
                }
        try:
            source_data = self._sanitize_ref_snapshot(
                source, adapter.branch_snapshot(source)
            )
            target_data = self._sanitize_ref_snapshot(
                target, adapter.branch_snapshot(target)
            )
            comparison = self._compare(adapter, source_data, target_data)
            topology = self.classify_topology(
                source=source_data,
                target=target_data,
                comparison=comparison,
            )
            evaluation = self.evaluate_policy(
                policy,
                ref=target,
                operation="update" if topology == "fast_forward" else "observe",
                method="fast_forward" if topology == "fast_forward" else None,
            )
            classification = (
                "protected_blocked"
                if topology == "fast_forward" and evaluation["status"] == "blocked"
                else topology
            )
            checks = (
                self._sanitize_checks(adapter.commit_checks(target_data["commit_sha"]))
                if target_data["exists"]
                else []
            )
            snapshot_id = make_id("ghsnap")
            connection.execute(
                text(
                    """
                    INSERT INTO github_branch_snapshots (
                        id, project_id, repository_binding_id, branch_policy_id,
                        branch_policy_version, source_ref, target_ref,
                        source_commit_sha, source_tree_sha, target_commit_sha,
                        target_tree_sha, merge_base_commit_sha,
                        merge_base_tree_sha, ahead_by, behind_by,
                        topology_classification, classification, refs,
                        comparison, protection, checks, policy_evaluation,
                        receipt_id, dedupe_key, observed_by
                    )
                    VALUES (
                        :id, :project_id, :repository_binding_id,
                        :branch_policy_id, :branch_policy_version, :source_ref,
                        :target_ref, :source_commit_sha, :source_tree_sha,
                        :target_commit_sha, :target_tree_sha,
                        :merge_base_commit_sha, :merge_base_tree_sha, :ahead_by,
                        :behind_by, :topology_classification, :classification,
                        CAST(:refs AS JSONB), CAST(:comparison AS JSONB),
                        CAST(:protection AS JSONB), CAST(:checks AS JSONB),
                        CAST(:policy_evaluation AS JSONB), :receipt_id,
                        :dedupe_key, :observed_by
                    )
                    """
                ),
                {
                    "id": snapshot_id,
                    "project_id": project_id,
                    "repository_binding_id": repository_binding_id,
                    "branch_policy_id": policy["id"],
                    "branch_policy_version": policy["version"],
                    "source_ref": source,
                    "target_ref": target,
                    "source_commit_sha": source_data["commit_sha"],
                    "source_tree_sha": source_data["tree_sha"],
                    "target_commit_sha": target_data["commit_sha"],
                    "target_tree_sha": target_data["tree_sha"],
                    "merge_base_commit_sha": comparison.get(
                        "merge_base_commit_sha"
                    ),
                    "merge_base_tree_sha": comparison.get("merge_base_tree_sha"),
                    "ahead_by": comparison["ahead_by"],
                    "behind_by": comparison["behind_by"],
                    "topology_classification": topology,
                    "classification": classification,
                    "refs": json.dumps(
                        {"source": source_data, "target": target_data}
                    ),
                    "comparison": json.dumps(comparison),
                    "protection": json.dumps(
                        {
                            "source": source_data["protection"],
                            "target": target_data["protection"],
                        }
                    ),
                    "checks": json.dumps(checks),
                    "policy_evaluation": json.dumps(evaluation),
                    "receipt_id": receipt["id"],
                    "dedupe_key": safe_dedupe_key,
                    "observed_by": actor,
                },
            )
            result = {
                "snapshot_id": snapshot_id,
                "repository": binding["full_name"],
                "topology_classification": topology,
                "classification": classification,
                "source_commit_sha": source_data["commit_sha"],
                "target_commit_sha": target_data["commit_sha"],
            }
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                binding["full_name"],
                result,
            )
            emit_event(
                connection,
                project_id,
                "github.branch_snapshot_captured",
                actor,
                "github_branch_snapshot",
                snapshot_id,
                {
                    "repository_binding_id": repository_binding_id,
                    "branch_policy_id": policy["id"],
                    "source_ref": source,
                    "target_ref": target,
                    "topology_classification": topology,
                    "classification": classification,
                    "receipt_id": receipt["id"],
                },
            )
            return {
                "status": "succeeded",
                "duplicate": False,
                "receipt_id": receipt["id"],
                "snapshot": self.get_snapshot(
                    connection, project_id, snapshot_id
                ),
                "error": None,
            }
        except Exception as error:
            safe_error = {
                "type": error.__class__.__name__,
                "message": "GitHub branch snapshot failed.",
            }
            self.receipts.mark_failed(connection, receipt["id"], safe_error)
            emit_event(
                connection,
                project_id,
                "github.branch_snapshot_failed",
                actor,
                "github_repository_binding",
                repository_binding_id,
                {"receipt_id": receipt["id"], "error": safe_error},
            )
            return {
                "status": "failed",
                "duplicate": False,
                "receipt_id": receipt["id"],
                "snapshot": None,
                "error": safe_error,
            }

    def list_snapshots(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        rows = connection.execute(
            text(
                f"""
                SELECT id
                FROM github_branch_snapshots
                WHERE {" AND ".join(filters)}
                ORDER BY observed_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        ).scalars().all()
        return [
            self.get_snapshot(connection, project_id, str(snapshot_id))
            for snapshot_id in rows
        ]

    def get_snapshot(
        self,
        connection: Connection,
        project_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, repository_binding_id, branch_policy_id,
                       branch_policy_version, source_ref, target_ref,
                       source_commit_sha, source_tree_sha, target_commit_sha,
                       target_tree_sha, merge_base_commit_sha,
                       merge_base_tree_sha, ahead_by, behind_by,
                       topology_classification, classification, refs,
                       comparison, protection, checks, policy_evaluation,
                       receipt_id, dedupe_key, observed_by, observed_at,
                       created_at
                FROM github_branch_snapshots
                WHERE project_id = :project_id AND id = :snapshot_id
                """
            ),
            {"project_id": project_id, "snapshot_id": snapshot_id},
        ).mappings().first()
        if row is None:
            raise GitHubBranchSnapshotNotFoundError(
                "GitHub branch snapshot not found."
            )
        return self._serialize_snapshot(row)

    @classmethod
    def classify_topology(
        cls,
        *,
        source: dict[str, Any],
        target: dict[str, Any],
        comparison: dict[str, Any],
    ) -> str:
        if not source.get("exists") or not target.get("exists"):
            return "missing"
        if source.get("commit_sha") == target.get("commit_sha"):
            return "equal"
        if source.get("tree_sha") and source.get("tree_sha") == target.get(
            "tree_sha"
        ):
            return "tree_equivalent_rewrite"
        ahead_by = max(0, int(comparison.get("ahead_by") or 0))
        behind_by = max(0, int(comparison.get("behind_by") or 0))
        if ahead_by > 0 and behind_by == 0:
            return "fast_forward"
        if ahead_by == 0 and behind_by > 0:
            return "behind"
        return "diverged"

    def _insert_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        version: int,
        supersedes_policy_id: Optional[str],
        status: str,
        protected_ref_patterns: list[str],
        allowed_branch_prefixes: list[str],
        branch_name_pattern: str,
        allowed_update_methods: list[str],
        allowed_merge_methods: list[str],
        required_checks: list[str],
        required_approvals: int,
        require_pull_request: bool,
        require_linear_history: bool,
        require_signed_commits: bool,
        automation_updates_enabled: bool,
        force_push_allowed: bool,
        metadata: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        policy_id = make_id("ghpolicy")
        connection.execute(
            text(
                """
                INSERT INTO github_branch_policies (
                    id, project_id, repository_binding_id, version,
                    supersedes_policy_id, status, protected_ref_patterns,
                    allowed_branch_prefixes, branch_name_pattern,
                    allowed_update_methods, allowed_merge_methods,
                    required_checks, required_approvals, require_pull_request,
                    require_linear_history, require_signed_commits,
                    automation_updates_enabled, force_push_allowed, metadata,
                    created_by
                )
                VALUES (
                    :id, :project_id, :repository_binding_id, :version,
                    :supersedes_policy_id, :status,
                    CAST(:protected_ref_patterns AS JSONB),
                    CAST(:allowed_branch_prefixes AS JSONB),
                    :branch_name_pattern, CAST(:allowed_update_methods AS JSONB),
                    CAST(:allowed_merge_methods AS JSONB),
                    CAST(:required_checks AS JSONB), :required_approvals,
                    :require_pull_request, :require_linear_history,
                    :require_signed_commits, :automation_updates_enabled,
                    :force_push_allowed, CAST(:metadata AS JSONB), :created_by
                )
                """
            ),
            {
                "id": policy_id,
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
                "version": version,
                "supersedes_policy_id": supersedes_policy_id,
                "status": status,
                "protected_ref_patterns": json.dumps(protected_ref_patterns),
                "allowed_branch_prefixes": json.dumps(allowed_branch_prefixes),
                "branch_name_pattern": branch_name_pattern,
                "allowed_update_methods": json.dumps(allowed_update_methods),
                "allowed_merge_methods": json.dumps(allowed_merge_methods),
                "required_checks": json.dumps(required_checks),
                "required_approvals": required_approvals,
                "require_pull_request": require_pull_request,
                "require_linear_history": require_linear_history,
                "require_signed_commits": require_signed_commits,
                "automation_updates_enabled": automation_updates_enabled,
                "force_push_allowed": force_push_allowed,
                "metadata": json.dumps(metadata),
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.branch_policy_created"
            if version == 1
            else "github.branch_policy_revised",
            actor,
            "github_branch_policy",
            policy_id,
            {
                "repository_binding_id": repository_binding_id,
                "version": version,
                "supersedes_policy_id": supersedes_policy_id,
                "status": status,
            },
        )
        return self.get_policy(connection, project_id, policy_id)

    def _validate_policy_values(
        self,
        *,
        protected_ref_patterns: list[str],
        allowed_branch_prefixes: list[str],
        branch_name_pattern: str,
        allowed_update_methods: list[str],
        allowed_merge_methods: list[str],
        required_checks: list[str],
        required_approvals: int,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        patterns = self._normalize_list(
            protected_ref_patterns, "protected_ref_patterns"
        )
        prefixes = self._normalize_list(
            allowed_branch_prefixes, "allowed_branch_prefixes"
        )
        for prefix in prefixes:
            if not prefix.endswith("/"):
                raise ValueError("Each allowed branch prefix must end with '/'.")
            self._normalize_ref(prefix + "example")
        name_pattern = self._required_text(
            branch_name_pattern, "branch_name_pattern"
        )
        if len(name_pattern) > 512:
            raise ValueError("branch_name_pattern must be 512 characters or fewer.")
        if any(character in name_pattern for character in "()|\\{}"):
            raise ValueError(
                "branch_name_pattern must use a bounded character-class regex."
            )
        try:
            re.compile(name_pattern)
        except re.error as error:
            raise ValueError("branch_name_pattern must be a valid regex.") from error
        updates = self._normalize_list(
            allowed_update_methods, "allowed_update_methods"
        )
        invalid_updates = sorted(set(updates) - self.UPDATE_METHODS)
        if invalid_updates:
            raise ValueError(
                "Unsupported update methods: " + ", ".join(invalid_updates) + "."
            )
        merges = self._normalize_list(
            allowed_merge_methods, "allowed_merge_methods"
        )
        invalid_merges = sorted(set(merges) - self.MERGE_METHODS)
        if invalid_merges:
            raise ValueError(
                "Unsupported merge methods: " + ", ".join(invalid_merges) + "."
            )
        checks = self._normalize_list(required_checks, "required_checks", allow_empty=True)
        if required_approvals < 0 or required_approvals > 20:
            raise ValueError("required_approvals must be between 0 and 20.")
        safe_metadata = self.github_connections.secret_refs._validate_metadata(
            metadata
        )
        return {
            "protected_ref_patterns": patterns,
            "allowed_branch_prefixes": prefixes,
            "branch_name_pattern": name_pattern,
            "allowed_update_methods": updates,
            "allowed_merge_methods": merges,
            "required_checks": checks,
            "required_approvals": required_approvals,
            "metadata": safe_metadata,
        }

    @classmethod
    def _compare(
        cls,
        adapter: Any,
        source: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        if not source["exists"] or not target["exists"]:
            return {
                "status": "missing",
                "ahead_by": 0,
                "behind_by": 0,
                "total_commits": 0,
                "merge_base_commit_sha": None,
                "merge_base_tree_sha": None,
            }
        result = adapter.compare_commits(
            target["commit_sha"], source["commit_sha"]
        )
        return {
            "status": str(result.get("status") or ""),
            "ahead_by": max(0, int(result.get("ahead_by") or 0)),
            "behind_by": max(0, int(result.get("behind_by") or 0)),
            "total_commits": max(0, int(result.get("total_commits") or 0)),
            "merge_base_commit_sha": cls._optional_sha(
                result.get("merge_base_commit_sha")
            ),
            "merge_base_tree_sha": cls._optional_sha(
                result.get("merge_base_tree_sha")
            ),
        }

    @classmethod
    def _sanitize_ref_snapshot(
        cls,
        ref: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        exists = bool(value.get("exists", False))
        commit_sha = cls._optional_sha(value.get("commit_sha")) if exists else None
        tree_sha = cls._optional_sha(value.get("tree_sha")) if exists else None
        if exists and (not commit_sha or not tree_sha):
            raise GitHubBranchSnapshotError(
                f"GitHub returned incomplete immutable identity for {ref}."
            )
        parents = [
            sha
            for sha in (
                cls._optional_sha(parent) for parent in (value.get("parents") or [])
            )
            if sha
        ][:32]
        protection = value.get("protection") or {}
        if not isinstance(protection, dict):
            protection = {}
        return {
            "ref": ref,
            "exists": exists,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "parents": parents,
            "protected": bool(value.get("protected", False)),
            "protection": protection,
        }

    @staticmethod
    def _sanitize_checks(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        return [
            {
                "name": str(value.get("name") or "")[:255],
                "status": str(value.get("status") or "")[:64],
                "conclusion": (
                    str(value.get("conclusion"))[:64]
                    if value.get("conclusion") is not None
                    else None
                ),
                "app": (
                    str(value.get("app"))[:255]
                    if value.get("app") is not None
                    else None
                ),
            }
            for value in values[:500]
            if isinstance(value, dict) and value.get("name")
        ]

    @classmethod
    def _normalize_ref(cls, value: str) -> str:
        ref = cls._required_text(value, "ref")
        if ref.startswith("refs/heads/"):
            ref = ref[len("refs/heads/") :]
        if (
            len(ref) > 255
            or ref.startswith("/")
            or ref.endswith("/")
            or ref.endswith(".")
            or ".." in ref
            or "//" in ref
            or "@{" in ref
            or ref.endswith(".lock")
            or any(ord(character) < 32 for character in ref)
            or any(character in ref for character in " ~^:?*[\\")
        ):
            raise ValueError("ref is not a safe Git branch name.")
        return ref

    @classmethod
    def _valid_branch_name(cls, ref: str, pattern: str) -> bool:
        return bool(re.fullmatch(pattern, ref))

    @classmethod
    def _optional_sha(cls, value: Any) -> Optional[str]:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not cls._SHA.fullmatch(normalized):
            raise GitHubBranchSnapshotError(
                "GitHub returned an invalid immutable commit or tree identity."
            )
        return normalized.lower()

    @classmethod
    def _normalize_list(
        cls,
        values: list[str],
        field: str,
        *,
        allow_empty: bool = False,
    ) -> list[str]:
        if not isinstance(values, list):
            raise ValueError(f"{field} must be a list.")
        normalized = sorted(
            {str(value).strip() for value in values if str(value).strip()}
        )
        if not normalized and not allow_empty:
            raise ValueError(f"{field} must contain at least one value.")
        if any(len(value) > 255 for value in normalized):
            raise ValueError(f"{field} values must be 255 characters or fewer.")
        return normalized

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty.")
        return normalized

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
            raise ValueError("Project not found.")
        return dict(row)

    @staticmethod
    def _serialize_policy(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "version": row["version"],
            "supersedes_policy_id": row["supersedes_policy_id"],
            "status": row["status"],
            "protected_ref_patterns": row["protected_ref_patterns"] or [],
            "allowed_branch_prefixes": row["allowed_branch_prefixes"] or [],
            "branch_name_pattern": row["branch_name_pattern"],
            "allowed_update_methods": row["allowed_update_methods"] or [],
            "allowed_merge_methods": row["allowed_merge_methods"] or [],
            "required_checks": row["required_checks"] or [],
            "required_approvals": row["required_approvals"],
            "require_pull_request": bool(row["require_pull_request"]),
            "require_linear_history": bool(row["require_linear_history"]),
            "require_signed_commits": bool(row["require_signed_commits"]),
            "automation_updates_enabled": bool(
                row["automation_updates_enabled"]
            ),
            "force_push_allowed": bool(row["force_push_allowed"]),
            "metadata": row["metadata"] or {},
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _serialize_snapshot(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "branch_policy_id": row["branch_policy_id"],
            "branch_policy_version": row["branch_policy_version"],
            "source_ref": row["source_ref"],
            "target_ref": row["target_ref"],
            "source_commit_sha": row["source_commit_sha"],
            "source_tree_sha": row["source_tree_sha"],
            "target_commit_sha": row["target_commit_sha"],
            "target_tree_sha": row["target_tree_sha"],
            "merge_base_commit_sha": row["merge_base_commit_sha"],
            "merge_base_tree_sha": row["merge_base_tree_sha"],
            "ahead_by": row["ahead_by"],
            "behind_by": row["behind_by"],
            "topology_classification": row["topology_classification"],
            "classification": row["classification"],
            "refs": row["refs"] or {},
            "comparison": row["comparison"] or {},
            "protection": row["protection"] or {},
            "checks": row["checks"] or [],
            "policy_evaluation": row["policy_evaluation"] or {},
            "receipt_id": row["receipt_id"],
            "dedupe_key": row["dedupe_key"],
            "observed_by": row["observed_by"],
            "observed_at": row["observed_at"].isoformat(),
            "created_at": row["created_at"].isoformat(),
        }
