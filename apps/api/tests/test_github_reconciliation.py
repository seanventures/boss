from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services.github_reconciliation import (
    GitHubReconciliationService,
)
from apps.api.tests.test_github_branch_topology import (
    FakeBranchAdapter,
    _setup_repository,
)


PROJECT_ID = "boss-dogfood"
WEBHOOK_SECRET = "github-reconciliation-secret"
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
FEATURE_SHA = "c" * 40
MAIN_TREE = "1" * 40
FEATURE_TREE = "2" * 40


class FakeReconciliationAdapter(FakeBranchAdapter):
    refs: dict[str, dict] = {}
    rate: dict = {}
    rate_calls = 0
    ref_calls: list[str] = []
    fail_reads = False

    def rate_limit_status(self) -> dict:
        type(self).rate_calls += 1
        return dict(type(self).rate)

    def branch_snapshot(self, branch_name: str) -> dict:
        type(self).ref_calls.append(branch_name)
        if type(self).fail_reads:
            raise RuntimeError("provider detail that must be redacted")
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

    @staticmethod
    def ref(commit_sha: str, tree_sha: str, *, protected: bool = False) -> dict:
        return {
            "exists": True,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "parents": [],
            "protected": protected,
            "protection": {"enabled": protected},
        }


def _configure(monkeypatch) -> None:
    FakeReconciliationAdapter.refs = {
        "main": FakeReconciliationAdapter.ref(
            NEW_SHA, MAIN_TREE, protected=True
        ),
        "ai/codex/work": FakeReconciliationAdapter.ref(
            FEATURE_SHA, FEATURE_TREE
        ),
    }
    FakeReconciliationAdapter.rate = {
        "resource": "core",
        "limit": 5000,
        "remaining": 4998,
        "used": 2,
        "reset_epoch": 4102444800,
    }
    FakeReconciliationAdapter.rate_calls = 0
    FakeReconciliationAdapter.ref_calls = []
    FakeReconciliationAdapter.fail_reads = False
    monkeypatch.setattr(
        api_main.github_connections,
        "adapter_factory",
        FakeReconciliationAdapter,
    )


def _claim(engine, worker_id: str = "reconcile-worker") -> dict:
    with engine.begin() as connection:
        lease = api_main.jobs.claim_job(
            connection,
            worker_id,
            ["github.repository.reconcile"],
            300,
        )
        queued = connection.execute(
            text(
                """
                SELECT id, status, attempt, max_attempts, run_after, last_error
                FROM jobs
                WHERE kind = 'github.repository.reconcile'
                ORDER BY created_at
                """
            )
        ).mappings().all()
    assert lease is not None, [dict(item) for item in queued]
    return lease


def _execute(client, run: dict, lease: dict, worker_id: str = "reconcile-worker"):
    return client.post(
        f"/v1/github/reconciliation-runs/{run['id']}/execute",
        json={
            "project_id": PROJECT_ID,
            "job_id": lease["id"],
            "worker_id": worker_id,
            "lease_token": lease["lease_token"],
        },
    )


def _poll(client, binding_id: str, key: str, refs: list[str]) -> dict:
    response = client.post(
        "/v1/github/reconciliation-runs/poll",
        json={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding_id,
            "refs": refs,
            "dedupe_key": key,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["items"][0]


def _signed_headers(raw: bytes, delivery_id: str) -> dict[str, str]:
    digest = hmac.new(
        WEBHOOK_SECRET.encode(),
        raw,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": "push",
    }


def test_pull_request_signal_ignores_fork_ref_outside_bound_repository():
    refs = GitHubReconciliationService._refs_from_payload(
        "pull_request",
        {
            "pull_request": {
                "head": {
                    "ref": "contributor/work",
                    "sha": FEATURE_SHA,
                    "repo": {"full_name": "contributor/fork"},
                },
                "base": {
                    "ref": "main",
                    "sha": NEW_SHA,
                    "repo": {"full_name": "seanheiney/boss"},
                },
            }
        },
        default_branch="main",
        repository_full_name="seanheiney/boss",
    )

    assert refs == [
        {
            "ref_name": "main",
            "expected_commit_sha": NEW_SHA,
            "expected_deleted": False,
            "provider_occurred_at": None,
        }
    ]


def test_poll_reconciles_project_refs_with_read_receipts(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    run = _poll(
        client,
        binding["id"],
        "reconcile:poll:success",
        ["main", "ai/codex/work"],
    )
    assert run["status"] == "queued"

    response = _execute(client, run, _claim(engine))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["run"]["status"] == "succeeded"
    assert result["job"]["status"] == "succeeded"
    assert result["run"]["rate_limit_status"]["remaining"] == 4998
    assert len(result["run"]["observation_ids"]) == 2
    assert len(result["run"]["receipt_ids"]) == 3
    assert FakeReconciliationAdapter.rate_calls == 1
    assert sorted(FakeReconciliationAdapter.ref_calls) == [
        "ai/codex/work",
        "main",
    ]

    states = client.get(
        "/v1/github/repository-ref-states",
        params={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
        },
    )
    assert states.status_code == 200
    by_ref = {item["ref_name"]: item for item in states.json()["items"]}
    assert by_ref["main"]["commit_sha"] == NEW_SHA
    assert by_ref["main"]["protected"] is True
    assert by_ref["ai/codex/work"]["commit_sha"] == FEATURE_SHA

    with engine.begin() as connection:
        receipt_rows = connection.execute(
            text(
                """
                SELECT effect_kind, status
                FROM receipts
                WHERE id = ANY(CAST(:receipt_ids AS TEXT[]))
                ORDER BY effect_kind
                """
            ),
            {"receipt_ids": result["run"]["receipt_ids"]},
        ).mappings().all()
    assert {row["effect_kind"] for row in receipt_rows} == {
        "github.repository.rate_limit",
        "github.repository.observe_ref",
    }
    assert {row["status"] for row in receipt_rows} == {"succeeded"}


def test_bound_webhook_schedules_without_inline_provider_reads_and_dedupes(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    settings = api_main.inbound_webhooks.settings
    monkeypatch.setattr(settings, "github_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "webhook_inbound_secret", "")
    monkeypatch.setattr(settings, "webhook_inbound_secrets_json", "")
    monkeypatch.setattr(settings, "webhook_project_map_json", "")
    payload = {
        "ref": "refs/heads/main",
        "after": OLD_SHA,
        "deleted": False,
        "repository": {"full_name": "seanheiney/boss"},
        "sender": {"login": "github-user"},
        "head_commit": {"timestamp": "2026-07-26T18:00:00Z"},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    headers = _signed_headers(raw, "delivery-reconcile-1")

    first = client.post("/v1/webhooks/github", content=raw, headers=headers)
    duplicate = client.post("/v1/webhooks/github", content=raw, headers=headers)
    assert first.status_code == 202, first.text
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["duplicate"] is True
    assert FakeReconciliationAdapter.rate_calls == 0
    assert FakeReconciliationAdapter.ref_calls == []

    signals = client.get(
        "/v1/github/reconciliation-signals",
        params={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
        },
    ).json()["items"]
    assert len(signals) == 1
    assert signals[0]["expected_commit_sha"] == OLD_SHA
    assert signals[0]["status"] == "pending"
    runs = client.get(
        "/v1/github/reconciliation-runs",
        params={"project_id": PROJECT_ID},
    ).json()["items"]
    assert len(runs) == 1
    assert runs[0]["source"] == "webhook"

    result = _execute(client, runs[0], _claim(engine)).json()["run"]
    assert result["status"] == "succeeded"
    signal = client.get(
        "/v1/github/reconciliation-signals",
        params={"project_id": PROJECT_ID},
    ).json()["items"][0]
    assert signal["status"] == "stale"
    assert signal["observed_commit_sha"] == NEW_SHA


def test_rate_limit_retries_deadletters_and_preserves_failed_read_receipts(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    FakeReconciliationAdapter.rate = {
        "resource": "core",
        "limit": 5000,
        "remaining": 0,
        "used": 5000,
        "reset_epoch": 4102444800,
    }
    run = _poll(
        client,
        binding["id"],
        "reconcile:poll:rate-limited",
        ["main"],
    )

    for attempt in range(1, 4):
        lease = _claim(engine)
        response = _execute(client, run, lease)
        assert response.status_code == 200, response.text
        current = response.json()["run"]
        assert current["attempt_count"] == attempt
        assert current["rate_limit_status"]["remaining"] == 0
        assert len(current["receipt_ids"]) == attempt
        if attempt < 3:
            assert current["status"] == "failed"
            assert response.json()["job"]["status"] == "failed_retryable"
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE jobs
                        SET run_after = now() - interval '1 second'
                        WHERE id = :job_id
                        """
                    ),
                    {"job_id": run["job_id"]},
                )
        else:
            assert current["status"] == "deadlettered"
            assert current["deadletter_id"]
            assert response.json()["job"]["status"] == "deadlettered"
    assert FakeReconciliationAdapter.ref_calls == []

    repair = client.post(
        f"/v1/github/reconciliation-runs/{run['id']}/repair",
        json={
            "project_id": PROJECT_ID,
            "dedupe_key": "reconcile:repair:rate-limited",
            "actor": "ceo",
        },
    )
    assert repair.status_code == 201, repair.text
    repaired = repair.json()
    assert repaired["status"] == "queued"
    assert repaired["source"] == "repair"
    assert repaired["repair_of_run_id"] == run["id"]
    assert repaired["job_id"] != run["job_id"]


def test_provider_failures_are_redacted_and_retryable(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    _configure(monkeypatch)
    FakeReconciliationAdapter.fail_reads = True
    run = _poll(
        client,
        binding["id"],
        "reconcile:poll:provider-failure",
        ["main"],
    )
    result = _execute(client, run, _claim(engine)).json()["run"]
    assert result["status"] == "failed"
    assert result["error"]["message"] == (
        "GitHub reconciliation provider read failed."
    )
    assert "provider detail" not in json.dumps(result)
    assert len(result["receipt_ids"]) == 2
    with engine.begin() as connection:
        statuses = connection.execute(
            text(
                """
                SELECT effect_kind, status, error
                FROM receipts
                WHERE id = ANY(CAST(:receipt_ids AS TEXT[]))
                ORDER BY effect_kind
                """
            ),
            {"receipt_ids": result["receipt_ids"]},
        ).mappings().all()
    assert [row["status"] for row in statuses] == ["failed", "succeeded"]
    assert "provider detail" not in json.dumps([dict(row) for row in statuses])
