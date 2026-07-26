from __future__ import annotations

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT_ID = "boss-dogfood"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SHA_E = "e" * 40
SHA_F = "f" * 40
TREE_1 = "1" * 40
TREE_2 = "2" * 40
TREE_3 = "3" * 40


class FakeBranchAdapter:
    refs: dict[str, dict] = {}
    comparison: dict = {}
    branch_calls = 0
    compare_calls = 0
    check_calls = 0
    fail = False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def branch_snapshot(self, branch_name: str) -> dict:
        type(self).branch_calls += 1
        if type(self).fail:
            raise RuntimeError("provider detail that must be redacted")
        return dict(type(self).refs[branch_name])

    def repo_metadata(self) -> dict:
        return {
            "id": 4242,
            "full_name": "seanheiney/boss",
            "default_branch": "main",
            "visibility": "private",
            "archived": False,
            "html_url": "https://github.com/seanheiney/boss",
            "owner": {"login": "seanheiney"},
            "permissions": {"pull": True, "push": True},
        }

    def compare_commits(self, _base_sha: str, _head_sha: str) -> dict:
        type(self).compare_calls += 1
        return dict(type(self).comparison)

    def commit_checks(self, _commit_sha: str) -> list[dict]:
        type(self).check_calls += 1
        return [
            {
                "name": "test",
                "status": "completed",
                "conclusion": "success",
                "app": "github-actions",
            }
        ]


def _setup_repository(client, monkeypatch) -> tuple[dict, dict]:
    FakeBranchAdapter.fail = False
    secret = client.post(
        "/v1/secret-refs",
        json={
            "project_id": PROJECT_ID,
            "ref_kind": "env",
            "ref_key": "BOSS_BRANCH_TEST_GITHUB_TOKEN",
            "provider": "github",
            "secret_kind": "api_token",
            "actor": "ceo",
        },
    )
    assert secret.status_code == 201
    monkeypatch.setenv("BOSS_BRANCH_TEST_GITHUB_TOKEN", "never-persist-branch-token")
    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        FakeBranchAdapter,
    )
    created = client.post(
        "/v1/github/connections",
        json={
            "project_id": PROJECT_ID,
            "connection_label": "branch-test",
            "auth_method": "personal_access_token",
            "credential_ref": "env:BOSS_BRANCH_TEST_GITHUB_TOKEN",
            "required_permissions": ["pull", "push"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    github_connection = created.json()
    bound = client.post(
        "/v1/github/repository-bindings",
        json={
            "project_id": PROJECT_ID,
            "connection_id": github_connection["id"],
            "repository": "seanheiney/boss",
            "role": "primary",
            "actor": "ceo",
        },
    )
    assert bound.status_code == 201
    binding = bound.json()
    tested = client.post(
        f"/v1/github/connections/{github_connection['id']}/test",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "dedupe_key": "branch-test:connection",
            "actor": "ceo",
        },
    )
    assert tested.status_code == 200
    assert tested.json()["status"] == "passed"
    return tested.json()["connection"], binding


def _create_policy(client, binding_id: str, **overrides) -> dict:
    payload = {
        "project_id": PROJECT_ID,
        "repository_binding_id": binding_id,
        "protected_ref_patterns": ["main", "release/*"],
        "allowed_branch_prefixes": ["ai/", "feat/", "fix/"],
        "allowed_update_methods": ["fast_forward", "rebase"],
        "allowed_merge_methods": ["squash", "rebase"],
        "required_checks": ["test"],
        "required_approvals": 1,
        "require_pull_request": True,
        "automation_updates_enabled": True,
        "force_push_allowed": False,
        "actor": "ceo",
    }
    payload.update(overrides)
    response = client.post("/v1/github/branch-policies", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _capture(client, binding_id: str, sequence: str):
    return client.post(
        "/v1/github/branch-snapshots/capture",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding_id,
            "source_ref": "ai/codex/work",
            "target_ref": "main",
            "dedupe_key": f"github-snapshot:{sequence}",
            "actor": "ceo",
        },
    )


def _set_topology(
    *,
    source_sha: str | None,
    source_tree: str | None,
    target_sha: str | None,
    target_tree: str | None,
    ahead_by: int,
    behind_by: int,
) -> None:
    FakeBranchAdapter.refs = {
        "ai/codex/work": {
            "exists": source_sha is not None,
            "commit_sha": source_sha,
            "tree_sha": source_tree,
            "parents": [SHA_F] if source_sha else [],
            "protected": False,
            "protection": {},
        },
        "main": {
            "exists": target_sha is not None,
            "commit_sha": target_sha,
            "tree_sha": target_tree,
            "parents": [SHA_F] if target_sha else [],
            "protected": True,
            "protection": {"enabled": True},
        },
    }
    FakeBranchAdapter.comparison = {
        "status": "diverged" if ahead_by and behind_by else "ahead",
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "total_commits": ahead_by + behind_by,
        "merge_base_commit_sha": SHA_F if source_sha and target_sha else None,
        "merge_base_tree_sha": TREE_3 if source_sha and target_sha else None,
    }


def test_branch_policy_is_versioned_and_evaluates_protection(client, monkeypatch):
    _, binding = _setup_repository(client, monkeypatch)
    policy = _create_policy(
        client,
        binding["id"],
        automation_updates_enabled=False,
    )
    assert policy["version"] == 1
    assert policy["status"] == "active"

    force_push = client.post(
        f"/v1/github/branch-policies/{policy['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "ref": "main",
            "operation": "update",
            "method": "force_push",
        },
    )
    assert force_push.status_code == 200
    evaluation = force_push.json()
    assert evaluation["status"] == "blocked"
    assert evaluation["protected"] is True
    assert {
        "force_push_denied",
        "protected_ref_automation_disabled",
        "update_method_denied",
    }.issubset(evaluation["reasons"])

    prefix_denied = client.post(
        f"/v1/github/branch-policies/{policy['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "ref": "random/work",
            "operation": "create",
        },
    )
    assert prefix_denied.json()["status"] == "blocked"
    assert prefix_denied.json()["reasons"] == ["branch_prefix_denied"]

    revised = client.patch(
        f"/v1/github/branch-policies/{policy['id']}",
        json={
            "project_id": PROJECT_ID,
            "automation_updates_enabled": True,
            "required_approvals": 2,
            "actor": "ceo",
        },
    )
    assert revised.status_code == 200
    policy_v2 = revised.json()
    assert policy_v2["version"] == 2
    assert policy_v2["supersedes_policy_id"] == policy["id"]
    assert policy_v2["required_approvals"] == 2

    history = client.get(
        "/v1/github/branch-policies",
        params={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "include_history": "true",
        },
    )
    assert history.status_code == 200
    assert [item["version"] for item in history.json()["items"]] == [2, 1]
    assert history.json()["items"][1]["status"] == "superseded"
    cross_project = client.get(
        f"/v1/github/branch-policies/{policy_v2['id']}",
        params={"project_id": "not-this-project"},
    )
    assert cross_project.status_code == 404


def test_branch_policy_rejects_unsafe_rules_and_unauthorized_actor(
    client, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    unsafe_pattern = client.post(
        "/v1/github/branch-policies",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "branch_name_pattern": "[",
            "actor": "ceo",
        },
    )
    assert unsafe_pattern.status_code == 409
    assert "valid regex" in unsafe_pattern.json()["detail"]

    unauthorized = client.post(
        "/v1/github/branch-policies",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "actor": "coo",
        },
    )
    assert unauthorized.status_code == 403


def test_branch_snapshots_classify_topology_and_replay_receipt(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _create_policy(client, binding["id"])
    FakeBranchAdapter.branch_calls = 0
    FakeBranchAdapter.compare_calls = 0
    FakeBranchAdapter.check_calls = 0
    FakeBranchAdapter.fail = False

    cases = [
        ("equal", SHA_A, TREE_1, SHA_A, TREE_1, 0, 0, "equal"),
        ("rewrite", SHA_A, TREE_1, SHA_B, TREE_1, 0, 0, "tree_equivalent_rewrite"),
        ("ahead", SHA_C, TREE_2, SHA_B, TREE_1, 2, 0, "fast_forward"),
        ("behind", SHA_B, TREE_1, SHA_C, TREE_2, 0, 2, "behind"),
        ("diverged", SHA_D, TREE_2, SHA_E, TREE_3, 2, 3, "diverged"),
        ("missing", None, None, SHA_E, TREE_3, 0, 0, "missing"),
    ]
    snapshots: dict[str, dict] = {}
    for sequence, source_sha, source_tree, target_sha, target_tree, ahead, behind, expected in cases:
        _set_topology(
            source_sha=source_sha,
            source_tree=source_tree,
            target_sha=target_sha,
            target_tree=target_tree,
            ahead_by=ahead,
            behind_by=behind,
        )
        captured = _capture(client, binding["id"], sequence)
        assert captured.status_code == 200, captured.text
        payload = captured.json()
        assert payload["status"] == "succeeded"
        assert payload["duplicate"] is False
        assert payload["snapshot"]["topology_classification"] == expected
        assert payload["snapshot"]["classification"] == expected
        snapshots[sequence] = payload

    _set_topology(
        source_sha=SHA_C,
        source_tree=TREE_2,
        target_sha=SHA_B,
        target_tree=TREE_1,
        ahead_by=2,
        behind_by=0,
    )
    replayed = _capture(client, binding["id"], "ahead")
    assert replayed.status_code == 200
    assert replayed.json()["duplicate"] is True
    assert (
        replayed.json()["snapshot"]["id"]
        == snapshots["ahead"]["snapshot"]["id"]
    )
    assert FakeBranchAdapter.branch_calls == 12
    assert FakeBranchAdapter.compare_calls == 5
    assert FakeBranchAdapter.check_calls == 6

    dedupe_collision = client.post(
        "/v1/github/branch-snapshots/capture",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "source_ref": "ai/codex/work",
            "target_ref": "release/other",
            "dedupe_key": "github-snapshot:ahead",
            "actor": "ceo",
        },
    )
    assert dedupe_collision.status_code == 409
    assert "different GitHub snapshot" in dedupe_collision.json()["detail"]
    assert FakeBranchAdapter.branch_calls == 12

    listed = client.get(
        "/v1/github/branch-snapshots",
        params={"project_id": PROJECT_ID, "repository_binding_id": binding["id"]},
    )
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 6
    cross_project = client.get(
        f"/v1/github/branch-snapshots/{snapshots['equal']['snapshot']['id']}",
        params={"project_id": "not-this-project"},
    )
    assert cross_project.status_code == 404

    with engine.begin() as connection:
        receipts = connection.execute(
            text(
                """
                SELECT status, effect_kind, result
                FROM receipts
                WHERE project_id = :project_id
                  AND effect_kind = 'github.repository.snapshot'
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().all()
    assert len(receipts) == 6
    assert all(receipt["status"] == "succeeded" for receipt in receipts)
    assert all(receipt["result"]["snapshot_id"] for receipt in receipts)


def test_snapshot_preserves_topology_but_blocks_protected_update(
    client, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _create_policy(
        client,
        binding["id"],
        automation_updates_enabled=False,
    )
    FakeBranchAdapter.fail = False
    _set_topology(
        source_sha=SHA_C,
        source_tree=TREE_2,
        target_sha=SHA_B,
        target_tree=TREE_1,
        ahead_by=1,
        behind_by=0,
    )
    captured = _capture(client, binding["id"], "protected")
    assert captured.status_code == 200
    snapshot = captured.json()["snapshot"]
    assert snapshot["topology_classification"] == "fast_forward"
    assert snapshot["classification"] == "protected_blocked"
    assert snapshot["policy_evaluation"]["status"] == "blocked"
    assert "protected_ref_automation_disabled" in snapshot["policy_evaluation"][
        "reasons"
    ]


def test_snapshot_failure_is_receipted_redacted_and_project_scoped(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _create_policy(client, binding["id"])
    FakeBranchAdapter.fail = True
    failed = _capture(client, binding["id"], "failure")
    assert failed.status_code == 200
    payload = failed.json()
    assert payload["status"] == "failed"
    assert payload["snapshot"] is None
    assert payload["error"]["message"] == "GitHub branch snapshot failed."
    assert "provider detail" not in failed.text

    cross_project = client.get(
        "/v1/github/branch-snapshots/not-real",
        params={"project_id": "not-this-project"},
    )
    assert cross_project.status_code == 404

    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                """
                SELECT status, error
                FROM receipts
                WHERE id = :receipt_id
                """
            ),
            {"receipt_id": payload["receipt_id"]},
        ).mappings().one()
        failure_event = connection.execute(
            text(
                """
                SELECT data
                FROM events
                WHERE project_id = :project_id
                  AND event_type = 'github.branch_snapshot_failed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().one()
    assert receipt["status"] == "failed"
    assert receipt["error"]["message"] == "GitHub branch snapshot failed."
    assert "provider detail" not in str(failure_event["data"])
