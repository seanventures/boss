from __future__ import annotations

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services.github_adapter import GitHubMergeConflictError
from apps.api.tests.test_github_branch_topology import (
    FakeBranchAdapter,
    _setup_repository,
)


PROJECT_ID = "boss-dogfood"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_MERGE = "d" * 40
TREE_A = "1" * 40
TREE_B = "2" * 40
TREE_MERGE = "3" * 40


class FakeSyncAdapter(FakeBranchAdapter):
    refs: dict[str, dict] = {}
    comparison: dict = {}
    sha_trees: dict[str, str] = {}
    conflict = False
    fail_fast_forward = False
    mutation_calls: list[tuple] = []

    def branch_snapshot(self, branch_name: str) -> dict:
        item = type(self).refs.get(branch_name)
        if item is None:
            return {
                "ref": branch_name,
                "exists": False,
                "commit_sha": None,
                "tree_sha": None,
                "parents": [],
                "protected": False,
                "protection": {},
            }
        return dict(item)

    def create_branch_ref(self, branch_name: str, commit_sha: str) -> dict:
        type(self).mutation_calls.append(("create_branch_ref", branch_name, commit_sha))
        if type(self).refs.get(branch_name, {}).get("exists"):
            raise RuntimeError("branch already exists")
        type(self).refs[branch_name] = self._ref(
            commit_sha, type(self).sha_trees[commit_sha]
        )
        return {"ref": branch_name, "commit_sha": commit_sha}

    def fast_forward_ref(
        self,
        branch_name: str,
        *,
        expected_sha: str,
        new_sha: str,
    ) -> dict:
        type(self).mutation_calls.append(
            ("fast_forward_ref", branch_name, expected_sha, new_sha)
        )
        if type(self).fail_fast_forward:
            raise RuntimeError("provider failure detail")
        current = type(self).refs[branch_name]
        if current["commit_sha"] != expected_sha:
            raise RuntimeError("unexpected branch head")
        type(self).refs[branch_name] = self._ref(
            new_sha, type(self).sha_trees[new_sha]
        )
        return {
            "ref": branch_name,
            "previous_sha": expected_sha,
            "commit_sha": new_sha,
            "forced": False,
        }

    def merge_branch(
        self,
        *,
        base_branch: str,
        head_sha: str,
        message: str,
    ) -> dict:
        type(self).mutation_calls.append(
            ("merge_branch", base_branch, head_sha, message)
        )
        if type(self).conflict:
            raise GitHubMergeConflictError("conflict")
        type(self).refs[base_branch] = self._ref(SHA_MERGE, TREE_MERGE)
        type(self).sha_trees[SHA_MERGE] = TREE_MERGE
        return {
            "base": base_branch,
            "head": head_sha,
            "commit_sha": SHA_MERGE,
            "tree_sha": TREE_MERGE,
        }

    def compare_changed_files(
        self,
        _base_sha: str,
        _head_sha: str,
        *,
        limit: int = 300,
    ) -> list[dict]:
        return [
            {
                "filename": "engine/example.py",
                "previous_filename": None,
                "status": "modified",
                "additions": 4,
                "deletions": 2,
                "changes": 6,
                "patch": "@@ conflict evidence @@",
            }
        ][:limit]

    def create_resolution_commit(
        self,
        *,
        base_tree_sha: str,
        parent_shas: list[str],
        resolved_files: list[dict],
        message: str,
    ) -> dict:
        type(self).mutation_calls.append(
            (
                "create_resolution_commit",
                base_tree_sha,
                tuple(parent_shas),
                tuple(item["path"] for item in resolved_files),
                message,
            )
        )
        type(self).sha_trees[SHA_MERGE] = TREE_MERGE
        return {
            "commit_sha": SHA_MERGE,
            "tree_sha": TREE_MERGE,
            "parents": parent_shas,
            "files": [item["path"] for item in resolved_files],
        }

    @staticmethod
    def _ref(commit_sha: str, tree_sha: str, *, protected: bool = False) -> dict:
        return {
            "exists": True,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "parents": [],
            "protected": protected,
            "protection": {"enabled": protected},
        }


def _configure_fake(
    monkeypatch,
    *,
    source_sha: str,
    source_tree: str,
    target_sha: str,
    target_tree: str,
    ahead_by: int,
    behind_by: int,
    conflict: bool = False,
) -> None:
    FakeSyncAdapter.refs = {
        "ai/codex/work": FakeSyncAdapter._ref(source_sha, source_tree),
        "main": FakeSyncAdapter._ref(
            target_sha, target_tree, protected=True
        ),
    }
    FakeSyncAdapter.sha_trees = {
        source_sha: source_tree,
        target_sha: target_tree,
    }
    FakeSyncAdapter.comparison = {
        "status": "diverged" if ahead_by and behind_by else "behind",
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "total_commits": ahead_by + behind_by,
        "merge_base_commit_sha": SHA_C,
        "merge_base_tree_sha": TREE_A,
    }
    FakeSyncAdapter.conflict = conflict
    FakeSyncAdapter.fail_fast_forward = False
    FakeSyncAdapter.mutation_calls = []
    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        FakeSyncAdapter,
    )


def _policy(client, binding_id: str, **overrides) -> dict:
    payload = {
        "project_id": PROJECT_ID,
        "repository_binding_id": binding_id,
        "protected_ref_patterns": ["main"],
        "allowed_branch_prefixes": ["ai/", "feat/", "fix/"],
        "allowed_update_methods": ["fast_forward", "merge_commit"],
        "allowed_merge_methods": ["merge", "squash"],
        "required_checks": [],
        "required_approvals": 0,
        "require_pull_request": False,
        "require_linear_history": False,
        "automation_updates_enabled": True,
        "force_push_allowed": False,
        "actor": "ceo",
    }
    payload.update(overrides)
    response = client.post(
        "/v1/github/branch-policies",
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _snapshot(client, binding_id: str, key: str) -> dict:
    response = client.post(
        "/v1/github/branch-snapshots/capture",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding_id,
            "source_ref": "ai/codex/work",
            "target_ref": "main",
            "dedupe_key": key,
            "actor": "ceo",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    return response.json()["snapshot"]


def _plan(
    client,
    binding_id: str,
    snapshot_id: str,
    key: str,
    *,
    resolution_branch: str | None = None,
) -> dict:
    response = client.post(
        "/v1/github/code-sync-runs/plan",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding_id,
            "snapshot_id": snapshot_id,
            "dedupe_key": key,
            "resolution_branch": resolution_branch,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _lease(client, binding_id: str, ref_name: str, key: str) -> dict:
    response = client.post(
        "/v1/github/branch-leases",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding_id,
            "ref_name": ref_name,
            "owner_kind": "code_task_run",
            "owner_id": key,
            "run_id": key,
            "acquire_key": f"lease:{key}",
            "ttl_seconds": 300,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _apply(client, run_id: str, lease: dict):
    return client.post(
        f"/v1/github/code-sync-runs/{run_id}/apply",
        json={
            "project_id": PROJECT_ID,
            "lease_id": lease["lease"]["id"],
            "lease_token": lease["lease_token"],
            "fencing_token": lease["lease"]["fencing_token"],
            "actor": "ceo",
        },
    )


def test_safe_sync_fast_forwards_owned_branch_with_receipts(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_B,
        target_tree=TREE_B,
        ahead_by=0,
        behind_by=1,
    )
    _policy(client, binding["id"])
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:behind")
    run = _plan(client, binding["id"], snapshot["id"], "sync:plan:behind")
    assert run["action"] == "fast_forward_source"
    assert run["status"] == "planned"
    assert run["rollback_ref"] == "ai/codex/work"
    assert run["rollback_sha"] == SHA_A

    lease = _lease(client, binding["id"], "ai/codex/work", "sync-behind")
    applied = _apply(client, run["id"], lease)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["status"] == "succeeded"
    assert result["result_snapshot_id"]
    assert FakeSyncAdapter.refs["ai/codex/work"]["commit_sha"] == SHA_B
    assert FakeSyncAdapter.refs["main"]["commit_sha"] == SHA_B
    assert FakeSyncAdapter.mutation_calls == [
        ("fast_forward_ref", "ai/codex/work", SHA_A, SHA_B)
    ]

    with engine.begin() as connection:
        changeset = connection.execute(
            text(
                """
                SELECT status, expected_cost_usd_micros, reservation_id,
                       rollback_plan
                FROM changesets
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": result["changeset_id"]},
        ).mappings().one()
        receipts = connection.execute(
            text(
                """
                SELECT provider, action, status, request_hash, result, error
                FROM resource_receipts
                WHERE changeset_id = :changeset_id
                """
            ),
            {"changeset_id": result["changeset_id"]},
        ).mappings().all()
        events = connection.execute(
            text(
                """
                SELECT event_type
                FROM events
                WHERE project_id = :project_id
                  AND entity_id = :sync_run_id
                """
            ),
            {"project_id": PROJECT_ID, "sync_run_id": run["id"]},
        ).scalars().all()
    assert changeset["status"] == "applied"
    assert changeset["expected_cost_usd_micros"] == 0
    assert changeset["reservation_id"] is None
    assert changeset["rollback_plan"][0]["sha"] == SHA_A
    assert len(receipts) == 1
    assert receipts[0]["provider"] == "github"
    assert receipts[0]["action"] == "fast_forward_ref"
    assert receipts[0]["status"] == "succeeded"
    assert receipts[0]["result"]["forced"] is False
    assert receipts[0]["error"] is None
    assert "github.code_sync_succeeded" in events


def test_sync_rejects_stale_snapshot_before_provider_write(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_B,
        target_tree=TREE_B,
        ahead_by=0,
        behind_by=1,
    )
    _policy(client, binding["id"])
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:stale")
    run = _plan(client, binding["id"], snapshot["id"], "sync:plan:stale")
    lease = _lease(client, binding["id"], "ai/codex/work", "sync-stale")

    FakeSyncAdapter.refs["ai/codex/work"] = FakeSyncAdapter._ref(SHA_C, TREE_A)
    FakeSyncAdapter.sha_trees[SHA_C] = TREE_A
    applied = _apply(client, run["id"], lease)
    assert applied.status_code == 200
    assert applied.json()["status"] == "failed"
    assert applied.json()["error"]["type"] == "StaleRemoteTopology"
    assert FakeSyncAdapter.mutation_calls == []


def test_equal_sync_is_idempotent_noop_without_changeset(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_A,
        target_tree=TREE_A,
        ahead_by=0,
        behind_by=0,
    )
    _policy(client, binding["id"])
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:equal")
    first = _plan(client, binding["id"], snapshot["id"], "sync:plan:equal")
    replay = _plan(client, binding["id"], snapshot["id"], "sync:plan:equal")
    assert first["id"] == replay["id"]
    assert first["action"] == "noop"
    assert first["status"] == "noop"
    assert first["changeset_id"] is None
    assert first["receipt_ids"] == []
    assert FakeSyncAdapter.mutation_calls == []

    collision = client.post(
        "/v1/github/code-sync-runs/plan",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": "different-binding",
            "snapshot_id": snapshot["id"],
            "dedupe_key": "sync:plan:equal",
            "actor": "ceo",
        },
    )
    assert collision.status_code == 409
    assert "different code sync plan" in collision.json()["detail"]


def test_provider_failures_retry_then_deadletter_without_leaking_detail(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_B,
        target_tree=TREE_B,
        ahead_by=0,
        behind_by=1,
    )
    _policy(client, binding["id"])
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:deadletter")
    run = _plan(client, binding["id"], snapshot["id"], "sync:plan:deadletter")
    lease = _lease(client, binding["id"], "ai/codex/work", "sync-deadletter")
    FakeSyncAdapter.fail_fast_forward = True

    states = []
    for _ in range(3):
        response = _apply(client, run["id"], lease)
        assert response.status_code == 200
        states.append(response.json()["status"])
        assert "provider failure detail" not in response.text
    assert states == ["failed", "failed", "deadlettered"]

    with engine.begin() as connection:
        receipt_states = connection.execute(
            text(
                """
                SELECT status, error
                FROM resource_receipts
                WHERE changeset_id = :changeset_id
                ORDER BY created_at
                """
            ),
            {"changeset_id": run["changeset_id"]},
        ).mappings().all()
        deadletter = connection.execute(
            text(
                """
                SELECT source_kind, source_id, reason, payload
                FROM deadletters
                WHERE source_kind = 'github_code_sync_run'
                  AND source_id = :sync_run_id
                """
            ),
            {"sync_run_id": run["id"]},
        ).mappings().one()
    assert [item["status"] for item in receipt_states] == [
        "failed",
        "failed",
        "failed",
    ]
    assert all(
        item["error"]["message"] == "GitHub provider action failed."
        for item in receipt_states
    )
    assert deadletter["source_id"] == run["id"]
    assert deadletter["payload"]["attempt"] == 3
    assert "provider failure detail" not in str(deadletter)


def test_protected_target_requires_later_merge_authorization(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_B,
        target_tree=TREE_B,
        ahead_by=1,
        behind_by=0,
    )
    _policy(client, binding["id"])
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:protected")
    run = _plan(client, binding["id"], snapshot["id"], "sync:plan:protected")
    assert run["action"] == "fast_forward_target"
    assert run["status"] == "pending_approval"

    lease = _lease(client, binding["id"], "main", "sync-protected")
    blocked = _apply(client, run["id"], lease)
    assert blocked.status_code == 409
    assert "pending_approval" in blocked.json()["detail"]
    assert FakeSyncAdapter.mutation_calls == []


def test_divergence_fails_closed_for_linear_or_signed_policy(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_B,
        target_tree=TREE_B,
        ahead_by=1,
        behind_by=1,
    )
    _policy(
        client,
        binding["id"],
        require_linear_history=True,
        require_signed_commits=True,
    )
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:strict-policy")
    run = _plan(
        client,
        binding["id"],
        snapshot["id"],
        "sync:plan:strict-policy",
    )
    assert run["action"] == "create_resolution_branch"
    assert run["status"] == "blocked"
    assert run["changeset_id"] is None
    assert FakeSyncAdapter.mutation_calls == []


def test_divergence_creates_conflict_set_and_reviewed_resolution_branch(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure_fake(
        monkeypatch,
        source_sha=SHA_A,
        source_tree=TREE_A,
        target_sha=SHA_B,
        target_tree=TREE_B,
        ahead_by=2,
        behind_by=3,
        conflict=True,
    )
    _policy(client, binding["id"])
    snapshot = _snapshot(client, binding["id"], "sync:snapshot:diverged")
    resolution_branch = "ai/boss/resolve/work-into-main"
    run = _plan(
        client,
        binding["id"],
        snapshot["id"],
        "sync:plan:diverged",
        resolution_branch=resolution_branch,
    )
    assert run["action"] == "create_resolution_branch"
    assert run["status"] == "planned"
    lease = _lease(client, binding["id"], resolution_branch, "sync-diverged")

    applied = _apply(client, run["id"], lease)
    assert applied.status_code == 200, applied.text
    conflicted_run = applied.json()
    assert conflicted_run["status"] == "conflicted"
    assert conflicted_run["conflict_set_id"]
    assert FakeSyncAdapter.refs["main"]["commit_sha"] == SHA_B
    conflict = client.get(
        f"/v1/github/conflict-sets/{conflicted_run['conflict_set_id']}",
        params={"project_id": PROJECT_ID},
    ).json()
    assert conflict["status"] == "open"
    assert conflict["resolution_branch"] == resolution_branch
    assert conflict["conflict_count"] == 1
    assert conflict["files"][0]["filename"] == "engine/example.py"

    bad_evidence = client.post(
        f"/v1/github/conflict-sets/{conflict['id']}/resolution-plans",
        json={
            "project_id": PROJECT_ID,
            "resolved_files": [
                {
                    "path": "engine/example.py",
                    "content": "resolved = True\n",
                }
            ],
            "validation_evidence": {
                "red": {
                    "command": "pytest test_example.py",
                    "exit_code": 0,
                    "evidence_ref": "artifact://red",
                },
                "green": {
                    "command": "pytest test_example.py",
                    "exit_code": 0,
                    "evidence_ref": "artifact://green",
                },
            },
            "actor": "ceo",
        },
    )
    assert bad_evidence.status_code == 409

    proposed = client.post(
        f"/v1/github/conflict-sets/{conflict['id']}/resolution-plans",
        json={
            "project_id": PROJECT_ID,
            "resolved_files": [
                {
                    "path": "engine/example.py",
                    "content": "resolved = True\n",
                    "mode": "100644",
                }
            ],
            "validation_evidence": {
                "red": {
                    "command": "pytest test_example.py",
                    "exit_code": 1,
                    "evidence_ref": "artifact://red-run",
                },
                "green": {
                    "command": "pytest test_example.py",
                    "exit_code": 0,
                    "evidence_ref": "artifact://green-run",
                },
            },
            "actor": "ceo",
        },
    )
    assert proposed.status_code == 201, proposed.text
    plan = proposed.json()
    assert plan["status"] == "proposed"
    assert plan["changeset_id"]

    self_review = client.post(
        f"/v1/github/resolution-plans/{plan['id']}/approve",
        json={
            "project_id": PROJECT_ID,
            "review_evidence": {
                "verdict": "approved",
                "summary": "looks good",
            },
            "actor": "ceo",
        },
    )
    assert self_review.status_code == 409
    assert "independent reviewer" in self_review.json()["detail"]

    approved = client.post(
        f"/v1/github/resolution-plans/{plan['id']}/approve",
        json={
            "project_id": PROJECT_ID,
            "review_evidence": {
                "verdict": "approved",
                "summary": "Reviewed conflict markers and red-to-green proof.",
            },
            "actor": "cto",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "cto"

    resolved = client.post(
        f"/v1/github/resolution-plans/{plan['id']}/apply",
        json={
            "project_id": PROJECT_ID,
            "lease_id": lease["lease"]["id"],
            "lease_token": lease["lease_token"],
            "fencing_token": lease["lease"]["fencing_token"],
            "actor": "ceo",
        },
    )
    assert resolved.status_code == 200, resolved.text
    payload = resolved.json()
    assert payload["plan"]["status"] == "applied"
    assert payload["conflict_set"]["status"] == "resolved"
    assert payload["sync_run"]["status"] == "succeeded"
    assert payload["plan"]["result_commit_sha"] == SHA_MERGE
    assert FakeSyncAdapter.refs[resolution_branch]["commit_sha"] == SHA_MERGE
    assert FakeSyncAdapter.refs["main"]["commit_sha"] == SHA_B

    with engine.begin() as connection:
        receipts = connection.execute(
            text(
                """
                SELECT changeset_id, action, status
                FROM resource_receipts
                WHERE project_id = :project_id AND provider = 'github'
                ORDER BY created_at
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().all()
        resolution_changeset = connection.execute(
            text("SELECT status FROM changesets WHERE id = :changeset_id"),
            {"changeset_id": plan["changeset_id"]},
        ).scalar_one()
    assert [(item["action"], item["status"]) for item in receipts] == [
        ("create_branch_ref", "succeeded"),
        ("merge_branch", "failed"),
        ("create_resolution_commit", "succeeded"),
        ("fast_forward_ref", "succeeded"),
    ]
    assert resolution_changeset == "applied"
