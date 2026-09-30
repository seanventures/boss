from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.workflows import seed_specpack_workflows


PROJECT_ID = "boss-dogfood"


def publish(client, key, spec):
    created = client.post(
        "/v1/workflows",
        json={
            "project_id": PROJECT_ID,
            "workflow_key": key,
            "name": key.replace("-", " ").title(),
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    definition = created.json()
    version = client.post(
        f"/v1/workflows/{definition['id']}/versions",
        json={"project_id": PROJECT_ID, "spec": spec, "actor": "ceo"},
    )
    assert version.status_code == 201, version.text
    return definition, version.json()


def start(client, version_id, dedupe_key, input_data=None):
    response = client.post(
        "/v1/workflow-runs",
        json={
            "project_id": PROJECT_ID,
            "workflow_version_id": version_id,
            "dedupe_key": dedupe_key,
            "input": input_data or {},
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def claim(client, run_id, worker_id="system"):
    response = client.post(
        f"/v1/workflow-runs/{run_id}/steps/claim",
        json={"project_id": PROJECT_ID, "worker_id": worker_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def complete(client, step_run_id, output=None, worker_id="system"):
    response = client.post(
        f"/v1/workflow-step-runs/{step_run_id}/complete",
        json={
            "project_id": PROJECT_ID,
            "worker_id": worker_id,
            "output": output or {},
            "evidence": [{"kind": "test", "ref": "pytest"}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_run(client, run_id):
    response = client.get(
        f"/v1/workflow-runs/{run_id}", params={"project_id": PROJECT_ID}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_shipped_specpack_workflows_are_registered_idempotently(engine):
    settings = get_settings()
    with engine.begin() as connection:
        first = seed_specpack_workflows(
            connection,
            project_id=PROJECT_ID,
            workflow_root=settings.specpack_root / "workflows",
            actor="test",
        )
        second = seed_specpack_workflows(
            connection,
            project_id=PROJECT_ID,
            workflow_root=settings.specpack_root / "workflows",
            actor="test",
        )

    assert len(first) == 33
    assert {item["workflow_key"] for item in first} >= {
        "ceo_command_to_pr",
        "governed_coding_pipeline",
        "self_improvement",
        "ground_truth_metric_evaluation",
        "ground_truth_baseline_change",
        "regression_signal_lifecycle",
        "closed_question_publication_guard",
        "settled_conclusion_reopen",
        "claim_authority_gate",
        "red_to_green_verification",
        "causal_isolation_verification",
        "surface_ownership_acquire",
        "surface_ownership_transfer",
        "ownership_effect_preflight",
        "verification_schedule",
        "adversarial_verification_execute",
        "assurance_case_evaluate",
        "agent_group_panel",
        "agent_group_producer_reviewer",
        "agent_group_bounded_debate",
        "agent_group_fan_out_join",
        "integration_connection_lifecycle",
        "capability_resolution_gate",
    }
    assert all(item["duplicate"] for item in second)


def test_registry_validation_mapping_pinning_and_idempotent_replay(client):
    spec = {
        "entry_step": "prepare",
        "steps": [
            {"id": "prepare", "kind": "task", "next": ["ship"]},
            {"id": "ship", "kind": "task"},
        ],
    }
    definition, version = publish(client, "release-train", spec)

    duplicate = client.post(
        f"/v1/workflows/{definition['id']}/versions",
        json={"project_id": PROJECT_ID, "spec": spec, "actor": "ceo"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == version["id"]
    assert duplicate.json()["duplicate"] is True

    invalid = client.post(
        f"/v1/workflows/{definition['id']}/versions",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "spec": {
                "entry_step": "a",
                "steps": [
                    {"id": "a", "kind": "task", "next": ["b"]},
                    {"id": "b", "kind": "task", "next": ["a"]},
                ],
            },
        },
    )
    assert invalid.status_code == 422
    assert "unbounded cycle" in invalid.json()["detail"]

    mapping = client.post(
        "/v1/workflow-mappings",
        json={
            "project_id": PROJECT_ID,
            "workflow_version_id": version["id"],
            "selector_kind": "work_class",
            "selector_value": "release",
            "scope_type": "project",
            "scope_id": PROJECT_ID,
            "priority": 50,
            "actor": "ceo",
        },
    )
    assert mapping.status_code == 201, mapping.text
    resolved = client.post(
        "/v1/workflow-mappings/resolve",
        json={
            "project_id": PROJECT_ID,
            "selector_kind": "work_class",
            "selector_value": "release",
            "actor": "ceo",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["selected"]["id"] == mapping.json()["id"]

    started = client.post(
        "/v1/workflow-runs",
        json={
            "project_id": PROJECT_ID,
            "selector_kind": "work_class",
            "selector_value": "release",
            "dedupe_key": "release:42",
            "actor": "ceo",
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    assert run["workflow_version_id"] == version["id"]
    assert run["workflow_mapping_id"] == mapping.json()["id"]

    replay = client.post(
        "/v1/workflow-runs",
        json={
            "project_id": PROJECT_ID,
            "workflow_version_id": version["id"],
            "dedupe_key": "release:42",
            "actor": "ceo",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == run["id"]
    assert replay.json()["duplicate"] is True

    first = claim(client, run["id"])
    assert first["step_id"] == "prepare"
    heartbeat = client.post(
        f"/v1/workflow-step-runs/{first['id']}/heartbeat",
        json={"project_id": PROJECT_ID, "worker_id": "system"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["status"] == "running"
    complete(client, first["id"], {"artifact": "candidate"})
    complete(client, claim(client, run["id"])["id"], {"released": True})
    finished = get_run(client, run["id"])
    assert finished["status"] == "completed"
    assert finished["output"] == {"artifact": "candidate", "released": True}
    assert all(step["evidence"] for step in finished["steps"])


def test_expired_leases_are_reclaimed_and_exhausted_retries_deadletter(client, engine):
    _, version = publish(
        client,
        "retry-work",
        {
            "entry_step": "work",
            "steps": [{"id": "work", "kind": "task", "max_attempts": 2}],
        },
    )
    reclaimed_run = start(client, version["id"], "retry:reclaim")
    original = claim(client, reclaimed_run["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_step_runs SET lease_expires_at=now() - interval '1 second' WHERE id=:id"
            ),
            {"id": original["id"]},
        )
    reclaimed = claim(client, reclaimed_run["id"])
    assert reclaimed["id"] == original["id"]
    assert reclaimed["attempt"] == 2
    complete(client, reclaimed["id"])

    failed_run = start(client, version["id"], "retry:deadletter")
    attempt_one = claim(client, failed_run["id"])
    retry = client.post(
        f"/v1/workflow-step-runs/{attempt_one['id']}/fail",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "system",
            "error": "transient",
            "retryable": True,
        },
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "ready"
    attempt_two = claim(client, failed_run["id"])
    terminal = client.post(
        f"/v1/workflow-step-runs/{attempt_two['id']}/fail",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "system",
            "error": "still broken",
            "retryable": True,
        },
    )
    assert terminal.status_code == 200
    assert terminal.json()["status"] == "deadlettered"
    assert get_run(client, failed_run["id"])["status"] == "deadlettered"
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM deadletters WHERE source_kind='workflow_step' AND source_id=:id"
            ),
            {"id": attempt_two["id"]},
        ).scalar_one() == 1


def test_condition_parallel_join_and_bounded_loop_are_deterministic(client):
    _, version = publish(
        client,
        "controlled-flow",
        {
            "entry_step": "route",
            "steps": [
                {
                    "id": "route",
                    "kind": "condition",
                    "expression": {"path": "input.fast", "op": "eq", "value": True},
                    "on_true": "fanout",
                    "on_false": "slow",
                },
                {
                    "id": "fanout",
                    "kind": "parallel",
                    "branches": ["left", "right"],
                    "join": "merge",
                },
                {"id": "left", "kind": "task", "next": ["merge"]},
                {"id": "right", "kind": "task", "next": ["merge"]},
                {
                    "id": "merge",
                    "kind": "join",
                    "wait_for": ["left", "right"],
                    "next": ["finish"],
                },
                {"id": "slow", "kind": "task", "next": ["finish"]},
                {"id": "finish", "kind": "task"},
            ],
        },
    )
    run = start(client, version["id"], "flow:parallel", {"fast": True})
    claimed = [claim(client, run["id"]), claim(client, run["id"])]
    assert {item["step_id"] for item in claimed} == {"left", "right"}
    for item in claimed:
        complete(client, item["id"], {item["step_id"]: True})
    last = claim(client, run["id"])
    assert last["step_id"] == "finish"
    complete(client, last["id"])
    detail = get_run(client, run["id"])
    statuses = {item["step_id"]: item["status"] for item in detail["steps"]}
    assert detail["status"] == "completed"
    assert statuses["slow"] == "skipped"
    assert statuses["merge"] == "succeeded"

    _, loop_version = publish(
        client,
        "bounded-loop",
        {
            "entry_step": "loop",
            "steps": [
                {
                    "id": "loop",
                    "kind": "bounded_loop",
                    "body": "body",
                    "max_iterations": 3,
                    "expression": {"path": "context.keep_going", "op": "ne", "value": False},
                    "next": ["done"],
                },
                {"id": "body", "kind": "task"},
                {"id": "done", "kind": "task"},
            ],
        },
    )
    loop_run = start(client, loop_version["id"], "flow:loop")
    body = claim(client, loop_run["id"])
    assert body["step_id"] == "body"
    complete(client, body["id"], {"keep_going": False})
    done = claim(client, loop_run["id"])
    assert done["step_id"] == "done"
    complete(client, done["id"])
    loop_detail = get_run(client, loop_run["id"])
    controllers = [step for step in loop_detail["steps"] if step["step_id"] == "loop"]
    assert loop_detail["status"] == "completed"
    assert [step["iteration"] for step in controllers] == [0, 1]


def test_timer_decision_pause_resume_and_binding_fail_closed(client):
    _, version = publish(
        client,
        "governed-wait",
        {
            "entry_step": "timer",
            "steps": [
                {"id": "timer", "kind": "timer", "wait_seconds": 0, "next": ["approve"]},
                {
                    "id": "approve",
                    "kind": "decision",
                    "question": "May this workflow continue?",
                    "next": ["execute"],
                },
                {"id": "execute", "kind": "task"},
            ],
        },
    )
    run = start(client, version["id"], "wait:decision")
    advanced = client.post(
        f"/v1/workflow-runs/{run['id']}/advance",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert advanced.status_code == 200
    waiting = advanced.json()
    decision_step = next(step for step in waiting["steps"] if step["step_id"] == "approve")
    assert waiting["status"] == "waiting"
    assert decision_step["decision_id"]

    paused = client.post(
        f"/v1/workflow-runs/{run['id']}/pause",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    resumed = client.post(
        f"/v1/workflow-runs/{run['id']}/resume",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "waiting"

    approved = client.post(
        f"/v1/decisions/{decision_step['decision_id']}/approve",
        json={"actor": "ceo", "note": "The evidence is sufficient."},
    )
    assert approved.status_code == 200, approved.text
    client.post(
        f"/v1/workflow-runs/{run['id']}/advance",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    complete(client, claim(client, run["id"])["id"])
    assert get_run(client, run["id"])["status"] == "completed"

    _, blocked_version = publish(
        client,
        "missing-integration",
        {
            "entry_step": "call",
            "steps": [
                {
                    "id": "call",
                    "kind": "integration",
                    "binding": {"integration_connection_id": "missing"},
                }
            ],
        },
    )
    blocked = client.post(
        "/v1/workflow-runs",
        json={
            "project_id": PROJECT_ID,
            "workflow_version_id": blocked_version["id"],
            "dedupe_key": "binding:missing",
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 409
    assert "fails closed" in blocked.json()["detail"]


def test_compensation_runs_in_reverse_order_and_migration_can_rollback(client):
    definition, version_one = publish(
        client,
        "change-with-rollback",
        {
            "entry_step": "create",
            "steps": [
                {
                    "id": "create",
                    "kind": "task",
                    "next": ["publish"],
                    "compensation": {"action": "delete-created-resource"},
                },
                {
                    "id": "publish",
                    "kind": "task",
                    "compensation": {"action": "unpublish-resource"},
                },
            ],
        },
    )
    run = start(client, version_one["id"], "compensation:1")
    first = claim(client, run["id"])
    complete(client, first["id"])
    second = claim(client, run["id"])
    complete(client, second["id"])

    begun = client.post(
        f"/v1/workflow-runs/{run['id']}/compensate",
        json={"project_id": PROJECT_ID, "actor": "ceo", "reason": "rollback requested"},
    )
    assert begun.status_code == 200, begun.text
    assert begun.json()["status"] == "compensating"
    claimed_second = client.post(
        f"/v1/workflow-runs/{run['id']}/compensations/claim",
        json={"project_id": PROJECT_ID, "worker_id": "system"},
    ).json()
    assert claimed_second["workflow_step_run_id"] == second["id"]
    assert client.post(
        f"/v1/workflow-compensation-runs/{claimed_second['id']}/complete",
        json={"project_id": PROJECT_ID, "worker_id": "system", "output": {"ok": True}},
    ).status_code == 200
    claimed_first = client.post(
        f"/v1/workflow-runs/{run['id']}/compensations/claim",
        json={"project_id": PROJECT_ID, "worker_id": "system"},
    ).json()
    assert claimed_first["workflow_step_run_id"] == first["id"]
    assert client.post(
        f"/v1/workflow-compensation-runs/{claimed_first['id']}/complete",
        json={"project_id": PROJECT_ID, "worker_id": "system", "output": {"ok": True}},
    ).status_code == 200
    assert get_run(client, run["id"])["status"] == "compensated"

    version_two_response = client.post(
        f"/v1/workflows/{definition['id']}/versions",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "spec": {
                "entry_step": "create",
                "steps": [
                    {"id": "create", "kind": "task", "next": ["publish"]},
                    {"id": "publish", "kind": "task", "next": ["verify"]},
                    {"id": "verify", "kind": "task"},
                ],
            },
        },
    )
    assert version_two_response.status_code == 201
    version_two = version_two_response.json()
    migration_run = start(client, version_one["id"], "migration:1")
    complete(client, claim(client, migration_run["id"])["id"])
    paused = client.post(
        f"/v1/workflow-runs/{migration_run['id']}/pause",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert paused.status_code == 200
    migrated = client.post(
        f"/v1/workflow-runs/{migration_run['id']}/migrate",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "target_version_id": version_two["id"],
            "reason": "add verification",
        },
    )
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["workflow_version_id"] == version_two["id"]
    migration_id = migrated.json()["migrations"][0]["id"]
    rolled_back = client.post(
        f"/v1/workflow-runs/{migration_run['id']}/migrations/rollback",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "migration_id": migration_id,
        },
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["workflow_version_id"] == version_one["id"]
    assert all(step["step_id"] != "verify" for step in rolled_back.json()["steps"])
