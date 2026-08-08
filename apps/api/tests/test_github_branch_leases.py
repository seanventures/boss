from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_github_branch_topology import _setup_repository


PROJECT_ID = "boss-dogfood"


def _acquire(client, binding_id: str, key: str, **overrides):
    payload = {
        "project_id": PROJECT_ID,
        "repository_binding_id": binding_id,
        "ref_name": "ai/codex/lease-work",
        "owner_kind": "code_task_run",
        "owner_id": "code-run-1",
        "run_id": "workflow-run-1",
        "worktree_path": "worktrees/code-run-1",
        "acquire_key": key,
        "ttl_seconds": 300,
        "actor": "ceo",
    }
    payload.update(overrides)
    return client.post("/v1/github/branch-leases", json=payload)


def test_branch_lease_token_fencing_heartbeat_release_and_replay(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    acquired = _acquire(client, binding["id"], "lease:one")
    assert acquired.status_code == 201, acquired.text
    payload = acquired.json()
    lease = payload["lease"]
    lease_token = payload["lease_token"]
    assert payload["duplicate"] is False
    assert lease_token
    assert lease["status"] == "active"
    assert lease["fencing_token"] > 0
    assert "lease_token_hash" not in acquired.text

    replayed = _acquire(client, binding["id"], "lease:one")
    assert replayed.status_code == 201
    assert replayed.json()["duplicate"] is True
    assert replayed.json()["lease_token"] is None
    assert replayed.json()["lease"]["id"] == lease["id"]

    overlap = _acquire(
        client,
        binding["id"],
        "lease:overlap",
        owner_id="code-run-2",
    )
    assert overlap.status_code == 409
    assert "active unexpired lease" in overlap.json()["detail"]

    wrong_token = client.post(
        f"/v1/github/branch-leases/{lease['id']}/heartbeat",
        json={
            "project_id": PROJECT_ID,
            "lease_token": "wrong-token",
            "fencing_token": lease["fencing_token"],
            "ttl_seconds": 600,
            "actor": "system",
        },
    )
    assert wrong_token.status_code == 409
    assert "token is invalid" in wrong_token.json()["detail"]

    heartbeat = client.post(
        f"/v1/github/branch-leases/{lease['id']}/heartbeat",
        json={
            "project_id": PROJECT_ID,
            "lease_token": lease_token,
            "fencing_token": lease["fencing_token"],
            "ttl_seconds": 600,
            "actor": "system",
        },
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["heartbeat_at"] > lease["heartbeat_at"]
    assert heartbeat.json()["lease_expires_at"] > lease["lease_expires_at"]

    stale_fence = client.post(
        f"/v1/github/branch-leases/{lease['id']}/release",
        json={
            "project_id": PROJECT_ID,
            "lease_token": lease_token,
            "fencing_token": lease["fencing_token"] + 1,
            "reason": "done",
            "actor": "ceo",
        },
    )
    assert stale_fence.status_code == 409
    assert "fencing token is stale" in stale_fence.json()["detail"]

    released = client.post(
        f"/v1/github/branch-leases/{lease['id']}/release",
        json={
            "project_id": PROJECT_ID,
            "lease_token": lease_token,
            "fencing_token": lease["fencing_token"],
            "reason": "work completed",
            "actor": "ceo",
        },
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert released.json()["release_reason"] == "work completed"

    reacquired = _acquire(
        client,
        binding["id"],
        "lease:two",
        owner_id="code-run-2",
    )
    assert reacquired.status_code == 201
    assert (
        reacquired.json()["lease"]["fencing_token"] > lease["fencing_token"]
    )

    with engine.begin() as connection:
        stored = connection.execute(
            text(
                """
                SELECT lease_token_hash
                FROM github_branch_leases
                WHERE id = :lease_id
                """
            ),
            {"lease_id": lease["id"]},
        ).scalar_one()
        events = connection.execute(
            text(
                """
                SELECT event_type, data
                FROM events
                WHERE project_id = :project_id
                  AND entity_id = :lease_id
                ORDER BY created_at
                """
            ),
            {"project_id": PROJECT_ID, "lease_id": lease["id"]},
        ).mappings().all()
    assert stored != lease_token
    assert lease_token not in str(events)
    assert {
        "github.branch_lease_acquired",
        "github.branch_lease_heartbeat",
        "github.branch_lease_released",
    }.issubset({event["event_type"] for event in events})


def test_expired_branch_lease_reclaims_with_new_fencing_lineage(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    first = _acquire(client, binding["id"], "lease:expired").json()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE github_branch_leases
                SET lease_expires_at = now() - interval '1 second'
                WHERE id = :lease_id
                """
            ),
            {"lease_id": first["lease"]["id"]},
        )

    second = _acquire(
        client,
        binding["id"],
        "lease:after-expiry",
        owner_id="code-run-after-expiry",
    )
    assert second.status_code == 201
    second_lease = second.json()["lease"]
    assert second_lease["reclaimed_from_lease_id"] == first["lease"]["id"]
    assert second_lease["fencing_token"] > first["lease"]["fencing_token"]

    history = client.get(
        "/v1/github/branch-leases",
        params={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "include_history": "true",
        },
    )
    assert history.status_code == 200
    statuses = {item["id"]: item["status"] for item in history.json()["items"]}
    assert statuses[first["lease"]["id"]] == "expired"
    assert statuses[second_lease["id"]] == "active"


def test_explicit_reclaim_requires_expiry_and_preserves_audit(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    lease = _acquire(client, binding["id"], "lease:reclaim").json()["lease"]
    premature = client.post(
        f"/v1/github/branch-leases/{lease['id']}/reclaim",
        json={
            "project_id": PROJECT_ID,
            "reason": "operator attempted reclaim",
            "actor": "ceo",
        },
    )
    assert premature.status_code == 409
    assert "unexpired" in premature.json()["detail"]

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE github_branch_leases
                SET lease_expires_at = now() - interval '1 second'
                WHERE id = :lease_id
                """
            ),
            {"lease_id": lease["id"]},
        )
    reclaimed = client.post(
        f"/v1/github/branch-leases/{lease['id']}/reclaim",
        json={
            "project_id": PROJECT_ID,
            "reason": "owner heartbeat expired",
            "actor": "ceo",
        },
    )
    assert reclaimed.status_code == 200
    assert reclaimed.json()["status"] == "reclaimed"
    assert reclaimed.json()["release_reason"] == "owner heartbeat expired"


def test_branch_lease_is_project_scoped_permissioned_and_dedupe_scoped(
    client, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    lease = _acquire(client, binding["id"], "lease:scope").json()["lease"]

    cross_project = client.get(
        f"/v1/github/branch-leases/{lease['id']}",
        params={"project_id": "not-this-project"},
    )
    assert cross_project.status_code == 404

    collision = _acquire(
        client,
        binding["id"],
        "lease:scope",
        ref_name="ai/codex/different",
    )
    assert collision.status_code == 409
    assert "different branch lease" in collision.json()["detail"]

    unauthorized = _acquire(
        client,
        binding["id"],
        "lease:unauthorized",
        ref_name="ai/codex/other",
        actor="coo",
    )
    assert unauthorized.status_code == 403


def test_concurrent_branch_lease_acquire_has_exactly_one_winner(
    client, engine, monkeypatch
):
    _, binding = _setup_repository(client, monkeypatch)
    barrier = Barrier(2)

    def acquire(index: int) -> str:
        barrier.wait(timeout=5)
        try:
            with engine.begin() as connection:
                result = api_main.github_branch_leases.acquire(
                    connection,
                    project_id=PROJECT_ID,
                    repository_binding_id=binding["id"],
                    ref_name="ai/codex/concurrent",
                    owner_kind="agent",
                    owner_id=f"agent-{index}",
                    run_id=f"run-{index}",
                    worktree_path=f"worktrees/run-{index}",
                    acquire_key=f"lease:concurrent:{index}",
                    ttl_seconds=300,
                    actor="ceo",
                )
                return f"won:{result['lease']['id']}"
        except Exception as error:
            return f"blocked:{error}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(acquire, [1, 2]))

    assert sum(outcome.startswith("won:") for outcome in outcomes) == 1
    assert sum("active unexpired lease" in outcome for outcome in outcomes) == 1
    listed = client.get(
        "/v1/github/branch-leases",
        params={
            "project_id": PROJECT_ID,
            "repository_binding_id": binding["id"],
            "ref_name": "ai/codex/concurrent",
        },
    )
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1
