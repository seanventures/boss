from __future__ import annotations

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_github_branch_topology import (
    FakeBranchAdapter,
    _setup_repository,
)
from apps.api.tests.test_github_code_sync import _lease, _policy


PROJECT_ID = "boss-dogfood"
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
STALE_SHA = "c" * 40
MERGE_SHA = "d" * 40
HEAD_TREE = "1" * 40
BASE_TREE = "2" * 40
STALE_TREE = "3" * 40
MERGE_TREE = "4" * 40


class FakeMergeAdapter(FakeBranchAdapter):
    refs: dict[str, dict] = {}
    trees: dict[str, str] = {}
    pull: dict = {}
    checks: list[dict] = []
    reviews: list[dict] = []
    mutation_calls: list[tuple] = []
    fail_merge = False
    verify_wrong_target = False

    def branch_snapshot(self, branch_name: str) -> dict:
        return dict(type(self).refs[branch_name])

    def commit_identity(self, commit_sha: str) -> dict:
        return {
            "commit_sha": commit_sha,
            "tree_sha": type(self).trees[commit_sha],
        }

    def pull_request_snapshot(self, pull_number: int) -> dict:
        assert pull_number == type(self).pull["number"]
        return dict(type(self).pull)

    def commit_checks(self, _commit_sha: str) -> list[dict]:
        return [dict(item) for item in type(self).checks]

    def pull_request_reviews(self, _pull_number: int) -> list[dict]:
        return [dict(item) for item in type(self).reviews]

    def merge_pull_request_guarded(
        self,
        *,
        pull_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
        expected_head_ref: str,
        expected_base_ref: str,
        merge_method: str,
        commit_title: str,
    ) -> dict:
        type(self).mutation_calls.append(
            (
                "merge_pull_request_guarded",
                pull_number,
                expected_head_sha,
                expected_base_sha,
                expected_head_ref,
                expected_base_ref,
                merge_method,
                commit_title,
            )
        )
        if type(self).fail_merge:
            raise RuntimeError("provider detail that must be redacted")
        current = type(self).pull
        assert current["head_sha"] == expected_head_sha
        assert current["base_sha"] == expected_base_sha
        result_sha = STALE_SHA if type(self).verify_wrong_target else MERGE_SHA
        result_tree = STALE_TREE if type(self).verify_wrong_target else MERGE_TREE
        type(self).refs[expected_base_ref] = self._ref(
            result_sha, result_tree, protected=True
        )
        type(self).trees[MERGE_SHA] = MERGE_TREE
        type(self).trees[STALE_SHA] = STALE_TREE
        type(self).pull = {**current, "state": "closed"}
        return {
            "pull_request_number": pull_number,
            "commit_sha": MERGE_SHA,
            "merged": True,
            "message": "merged",
            "merge_method": merge_method,
            "previous_target_sha": expected_base_sha,
            "authorized_head_sha": expected_head_sha,
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


def _configure(monkeypatch, *, pull_number: int = 42) -> None:
    FakeMergeAdapter.refs = {
        "ai/codex/work": FakeMergeAdapter._ref(HEAD_SHA, HEAD_TREE),
        "main": FakeMergeAdapter._ref(BASE_SHA, BASE_TREE, protected=True),
    }
    FakeMergeAdapter.trees = {
        HEAD_SHA: HEAD_TREE,
        BASE_SHA: BASE_TREE,
    }
    FakeMergeAdapter.pull = {
        "number": pull_number,
        "url": f"https://github.com/seanheiney/boss/pull/{pull_number}",
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_ref": "ai/codex/work",
        "head_sha": HEAD_SHA,
        "head_repository": "seanheiney/boss",
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "base_repository": "seanheiney/boss",
        "updated_at": "2026-07-26T16:00:00Z",
    }
    FakeMergeAdapter.checks = [
        {
            "name": "test",
            "status": "completed",
            "conclusion": "success",
            "app": "local-validation",
        }
    ]
    FakeMergeAdapter.reviews = [
        {
            "id": 1,
            "reviewer": "cto",
            "state": "APPROVED",
            "commit_sha": HEAD_SHA,
            "submitted_at": "2026-07-26T16:01:00Z",
        }
    ]
    FakeMergeAdapter.mutation_calls = []
    FakeMergeAdapter.fail_merge = False
    FakeMergeAdapter.verify_wrong_target = False
    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        FakeMergeAdapter,
    )


def _request(client, binding_id: str, *, key: str, pull_number: int = 42):
    response = client.post(
        "/v1/github/merge-authorizations",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding_id,
            "pull_request_number": pull_number,
            "source_ref": "ai/codex/work",
            "target_ref": "main",
            "merge_method": "merge",
            "dedupe_key": key,
            "evidence_bundle": {
                "evidence_refs": [
                    "artifact://test-report",
                    "artifact://review-packet",
                ]
            },
            "expires_in_seconds": 3600,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _authorize_and_enqueue(client, authorization_id: str):
    approved = client.post(
        f"/v1/github/merge-authorizations/{authorization_id}/authorize",
        json={
            "project_id": PROJECT_ID,
            "review_evidence": {
                "verdict": "approved",
                "summary": "Independent release review passed.",
            },
            "actor": "cto",
        },
    )
    assert approved.status_code == 200, approved.text
    queued = client.post(
        f"/v1/github/merge-authorizations/{authorization_id}/enqueue",
        json={"project_id": PROJECT_ID, "actor": "cto"},
    )
    assert queued.status_code == 200, queued.text
    return queued.json()


def _apply(client, authorization_id: str, lease: dict):
    return client.post(
        f"/v1/github/merge-authorizations/{authorization_id}/apply",
        json={
            "project_id": PROJECT_ID,
            "lease_id": lease["lease"]["id"],
            "lease_token": lease["lease_token"],
            "fencing_token": lease["lease"]["fencing_token"],
            "actor": "ceo",
        },
    )


def test_governed_merge_requires_independent_authorization_and_receipts(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    requested = _request(
        client, binding["id"], key="merge-authorization:success"
    )
    assert requested["status"] == "pending_approval"
    assert requested["head_commit_sha"] == HEAD_SHA
    assert requested["base_commit_sha"] == BASE_SHA
    assert requested["rollback_plan"]["previous_target_sha"] == BASE_SHA

    self_approval = client.post(
        f"/v1/github/merge-authorizations/{requested['id']}/authorize",
        json={
            "project_id": PROJECT_ID,
            "review_evidence": {
                "verdict": "approved",
                "summary": "Self approval.",
            },
            "actor": "ceo",
        },
    )
    assert self_approval.status_code == 409
    assert "independent approver" in self_approval.json()["detail"]

    queued = _authorize_and_enqueue(client, requested["id"])
    assert queued["status"] == "queued"
    lease = _lease(client, binding["id"], "main", "merge-success")
    response = _apply(client, requested["id"], lease)
    assert response.status_code == 200, response.text
    merged = response.json()
    assert merged["status"] == "merged"
    assert merged["result_merge_sha"] == MERGE_SHA
    assert merged["result_target_tree_sha"] == MERGE_TREE
    assert merged["rollback_plan"]["strategy"] == "create_revert_pull_request"
    assert merged["rollback_plan"]["automatic"] is False
    assert len(merged["receipt_ids"]) == 4
    assert len(merged["resource_receipt_ids"]) == 1
    assert len(FakeMergeAdapter.mutation_calls) == 1

    with engine.begin() as connection:
        changeset = connection.execute(
            text(
                """
                SELECT status, risk_tier, expected_cost_usd_micros,
                       reservation_id, rollback_plan
                FROM changesets
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": merged["changeset_id"]},
        ).mappings().one()
        resource_receipt = connection.execute(
            text(
                """
                SELECT provider, action, resource_type, status, result, error
                FROM resource_receipts
                WHERE changeset_id = :changeset_id
                """
            ),
            {"changeset_id": merged["changeset_id"]},
        ).mappings().one()
    assert changeset["status"] == "applied"
    assert changeset["risk_tier"] == 3
    assert changeset["expected_cost_usd_micros"] == 0
    assert changeset["reservation_id"] is None
    assert changeset["rollback_plan"][0]["requires_new_authorization"] is True
    assert resource_receipt["provider"] == "github"
    assert resource_receipt["action"] == "merge_pull_request"
    assert resource_receipt["resource_type"] == "github_pull_request"
    assert resource_receipt["status"] == "succeeded"
    assert resource_receipt["result"]["commit_sha"] == MERGE_SHA
    assert resource_receipt["error"] is None


def test_merge_blocks_when_exact_head_changes_before_apply(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    requested = _request(client, binding["id"], key="merge-authorization:stale")
    _authorize_and_enqueue(client, requested["id"])
    lease = _lease(client, binding["id"], "main", "merge-stale")

    FakeMergeAdapter.trees[STALE_SHA] = STALE_TREE
    FakeMergeAdapter.refs["ai/codex/work"] = FakeMergeAdapter._ref(
        STALE_SHA, STALE_TREE
    )
    FakeMergeAdapter.pull = {
        **FakeMergeAdapter.pull,
        "head_sha": STALE_SHA,
    }
    response = _apply(client, requested["id"], lease)
    assert response.status_code == 200, response.text
    blocked = response.json()
    assert blocked["status"] == "blocked"
    assert blocked["error"]["type"] == "StaleMergeAuthorization"
    assert "changed after merge authorization" in blocked["error"]["message"]
    assert FakeMergeAdapter.mutation_calls == []


def test_merge_queue_serializes_each_project_target(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch, pull_number=42)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    first = _request(client, binding["id"], key="merge-queue:first")
    _authorize_and_enqueue(client, first["id"])

    FakeMergeAdapter.pull = {
        **FakeMergeAdapter.pull,
        "number": 43,
        "url": "https://github.com/seanheiney/boss/pull/43",
    }
    second = _request(
        client,
        binding["id"],
        key="merge-queue:second",
        pull_number=43,
    )
    _authorize_and_enqueue(client, second["id"])
    assert first["queue_position"] < second["queue_position"]

    lease = _lease(client, binding["id"], "main", "merge-queue")
    out_of_order = _apply(client, second["id"], lease)
    assert out_of_order.status_code == 409
    assert "prior merge authorization" in out_of_order.json()["detail"]
    assert FakeMergeAdapter.mutation_calls == []


def test_merge_rechecks_checks_and_current_head_reviews(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    requested = _request(client, binding["id"], key="merge-proof:stale")
    _authorize_and_enqueue(client, requested["id"])
    lease = _lease(client, binding["id"], "main", "merge-proof")

    FakeMergeAdapter.checks = [
        {
            "name": "test",
            "status": "completed",
            "conclusion": "failure",
            "app": "local-validation",
        }
    ]
    response = _apply(client, requested["id"], lease)
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert "Required checks" in response.json()["error"]["message"]
    assert FakeMergeAdapter.mutation_calls == []


def test_review_comments_do_not_clear_a_current_request_for_changes(
    client, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    requested = _request(client, binding["id"], key="merge-proof:changes")
    _authorize_and_enqueue(client, requested["id"])
    lease = _lease(client, binding["id"], "main", "merge-review")
    FakeMergeAdapter.reviews = [
        {
            "id": 1,
            "reviewer": "cto",
            "state": "CHANGES_REQUESTED",
            "commit_sha": HEAD_SHA,
            "submitted_at": "2026-07-26T16:01:00Z",
        },
        {
            "id": 2,
            "reviewer": "cto",
            "state": "COMMENTED",
            "commit_sha": HEAD_SHA,
            "submitted_at": "2026-07-26T16:02:00Z",
        },
    ]
    response = _apply(client, requested["id"], lease)
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert "requests changes" in response.json()["error"]["message"]
    assert FakeMergeAdapter.mutation_calls == []


def test_merge_failures_are_redacted_and_deadletter_after_bounded_retries(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    requested = _request(
        client, binding["id"], key="merge-authorization:deadletter"
    )
    _authorize_and_enqueue(client, requested["id"])
    lease = _lease(client, binding["id"], "main", "merge-deadletter")
    FakeMergeAdapter.fail_merge = True

    states = []
    for _ in range(3):
        response = _apply(client, requested["id"], lease)
        assert response.status_code == 200
        assert "provider detail that must be redacted" not in response.text
        states.append(response.json()["status"])
    assert states == ["failed", "failed", "deadlettered"]

    with engine.begin() as connection:
        receipts = connection.execute(
            text(
                """
                SELECT status, error
                FROM resource_receipts
                WHERE changeset_id = :changeset_id
                ORDER BY created_at
                """
            ),
            {"changeset_id": requested["changeset_id"]},
        ).mappings().all()
        deadletter = connection.execute(
            text(
                """
                SELECT source_kind, source_id, payload
                FROM deadletters
                WHERE source_kind = 'github_merge_authorization'
                  AND source_id = :source_id
                """
            ),
            {"source_id": requested["id"]},
        ).mappings().one()
    assert [item["status"] for item in receipts] == [
        "failed",
        "failed",
        "failed",
    ]
    assert all(
        item["error"]["message"] == "GitHub protected merge failed."
        for item in receipts
    )
    assert deadletter["payload"]["attempt"] == 3
    assert "provider detail that must be redacted" not in str(deadletter)


def test_accepted_merge_with_failed_verification_is_terminal_not_replayed(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    _policy(
        client,
        binding["id"],
        required_checks=["test"],
        required_approvals=1,
        require_pull_request=True,
    )
    requested = _request(
        client, binding["id"], key="merge-authorization:verify-failure"
    )
    _authorize_and_enqueue(client, requested["id"])
    lease = _lease(client, binding["id"], "main", "merge-verify-failure")
    FakeMergeAdapter.verify_wrong_target = True

    response = _apply(client, requested["id"], lease)
    assert response.status_code == 200
    blocked = response.json()
    assert blocked["status"] == "blocked"
    assert blocked["result_merge_sha"] == MERGE_SHA
    assert blocked["error"]["type"] == "MergeResultVerificationFailed"
    assert len(FakeMergeAdapter.mutation_calls) == 1

    replay = _apply(client, requested["id"], lease)
    assert replay.status_code == 409
    assert len(FakeMergeAdapter.mutation_calls) == 1
    with engine.begin() as connection:
        deadletter = connection.execute(
            text(
                """
                SELECT reason, payload
                FROM deadletters
                WHERE source_kind = 'github_merge_authorization'
                  AND source_id = :source_id
                """
            ),
            {"source_id": requested["id"]},
        ).mappings().one()
    assert deadletter["reason"] == "github_merge_result_verification_failed"
    assert deadletter["payload"]["merge_sha"] == MERGE_SHA
