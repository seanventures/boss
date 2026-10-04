"""A workflow's deploy step that ships to a stack target is approved ONCE: by its deployment.

boss-1, wfr_79c63a78b34546a3b927 (template minor_feature, repo paramg/boss-sandbox, which has an
active stack target): the deploy step was held at its release_manager gate BEFORE it ran, so the
release needed two approvals — the gate, through the paused per-app flow, and then the stack
deployment's own decision. Now the step plans the deployment at once and that deployment's
decision, raised for the step's approver_role, is the gate. A deploy that does not ship to a
stack target keeps its gate exactly as before.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (  # noqa: F401 - fixture
    HOME,
    _approve_and_execute,
    _build,
    stack,
)


def _deploy_run(engine, build_run_id: str, **extra) -> str:
    """A published product workflow whose one step deploys the build, gated on release_manager
    exactly as the catalogue's deploy steps are."""
    key = f"ship_{uuid4().hex[:8]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=HOME, workflow_key=key, name=key, description=None, actor="test",
            namespace="product")
        version = workflows.publish_version(
            c, project_id=HOME, definition_id=definition["id"], actor="test",
            spec={"entry_step": "deploy", "steps": [{
                "id": "deploy", "kind": "capability", "next": [],
                "binding": {"capability": "deploy",
                            "inputs": {"run_id": {"from_input": "build_run_id"}}},
                "approval": {"approver_role": "release_manager"},
            }]})
        run = workflows.start_run(
            c, project_id=HOME, actor="ceo", input_data={"build_run_id": build_run_id, **extra},
            dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
    return run["id"]


def _tick(engine):
    with engine.begin() as c:
        return runner.tick(c, SimpleNamespace(engine=engine, settings=api_main.settings,
                                              begin=engine.begin), project_id=HOME)


def _step(engine, run_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(
            text("SELECT status, decision_id, output, last_error FROM workflow_step_runs "
                 "WHERE workflow_run_id = :r AND step_id = 'deploy'"),
            {"r": run_id}).mappings().one())


def _decisions_for(engine, run_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(
            text("SELECT id, status, recommendation FROM decisions "
                 "WHERE recommendation->>'workflow_run_id' = :r"),
            {"r": run_id}).mappings().all()]


def test_a_stack_deploy_step_plans_at_once_and_waits_on_the_deployments_decision(
        client, engine, stack):
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "waiting", step
    assert step["decision_id"] is None, "the step was gated before it ran"
    assert step["output"]["await"]["kind"] == "stack_deployment"
    deployment_id = step["output"]["stack_deployment_id"]

    decisions = _decisions_for(engine, run_id)
    assert len(decisions) == 1, f"one approval, not {len(decisions)}"
    rec = decisions[0]["recommendation"]
    assert rec["action"] == "deploy_stack" and rec["deployment_id"] == deployment_id
    assert rec["approver_role"] == "release_manager" and rec["step_id"] == "deploy"

    # The console's deploy step carries the deployment, with the server's own actions.
    runs = client.get("/v1/delivery-workflows/runs", params={"project_id": HOME}).json()["runs"]
    served = next(s for r in runs if r["id"] == run_id for s in r["steps"]
                  if s["step_id"] == "deploy")
    assert served["stack_deploy"]["stack_deployment_id"] == deployment_id
    assert served["stack_deploy"]["target_id"] == stack["target"]["id"]
    assert served["stack_deploy"]["deployment"]["actions"] == ["approve", "reject"]


def test_approving_the_deployment_is_the_gate_and_completes_the_step(client, engine, stack):
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    deployment_id = _step(engine, run_id)["output"]["stack_deployment_id"]
    deployment = client.get(f"/v1/deployments/stack/{deployment_id}").json()["deployment"]

    done, _ = _approve_and_execute(client, engine, deployment)
    assert done["deployment"]["status"] == "healthy", done
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "succeeded", step
    assert step["output"]["approval"]["approved_by"] == "ceo"
    assert step["output"]["approval"]["approver_role"] == "release_manager"
    assert step["output"]["deployed_url"] == "https://shop.example.test"
    assert len(_decisions_for(engine, run_id)) == 1


def test_rejecting_the_deployment_rejects_the_step(client, engine, stack):
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    decision_id = _step(engine, run_id)["output"]["decision_id"]
    rejected = client.post(f"/v1/decisions/{decision_id}/reject",
                           json={"actor": "ceo", "note": "not this one"})
    assert rejected.status_code == 200, rejected.text
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "failed", step
    assert "not approved" in step["last_error"]
    assert stack["host"].writes() == []


def test_a_deploy_step_without_a_stack_target_still_holds_at_its_gate(client, engine, stack):
    # other/shop has no stack target: the per-app path, gate first, exactly as before.
    run_id = _deploy_run(engine, _build(engine, '{"owner": "other", "repo": "shop"}'))
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "waiting", step
    assert step["decision_id"], "the release_manager gate did not open"
    assert not (step["output"] or {}).get("await")
    with engine.begin() as c:
        planned = c.execute(text("SELECT count(*) FROM stack_deployments WHERE project_id = :p"),
                            {"p": HOME}).scalar_one()
    assert planned == 0
    runs = client.get("/v1/delivery-workflows/runs", params={"project_id": HOME}).json()["runs"]
    served = next(s for r in runs if r["id"] == run_id for s in r["steps"]
                  if s["step_id"] == "deploy")
    assert served["stack_deploy"] is None


def test_an_active_stack_target_is_somewhere_to_deploy(engine, stack):
    from boss_engine.services import workflow_readiness

    # Even with the host not (yet) verified: the stack target is the destination, and the
    # deployment's own preflight says what the host still needs.
    with engine.begin() as c:
        c.execute(text("UPDATE deployment_hosts SET status = 'registered' WHERE id = :h"),
                  {"h": stack["target"]["host_id"]})
        ok, detail = workflow_readiness.check(c, api_main.settings, HOME, "deployment_target")
    assert ok, detail
    assert "stack target" in detail


def _skip_and_retry(engine, run_id: str) -> dict:
    from boss_engine.services import workflow_run_actions

    with engine.begin() as c:
        actions = workflow_run_actions.actions_for(c, HOME, [run_id])[run_id]
    return {a["key"]: a for a in actions if a.get("step_id") == "deploy"}


def test_a_rejected_release_offers_retry_and_skip_and_skip_never_ships(client, engine, stack):
    """Agents home D3 (the operator's decision): a refused release stops the run at a failed
    step offering Retry (ask again) and Skip (carry on WITHOUT the release). A skip is never
    an approval: the step is `skipped`, not `succeeded`, and nothing is applied."""
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    decision_id = _step(engine, run_id)["output"]["decision_id"]
    assert client.post(f"/v1/decisions/{decision_id}/reject",
                       json={"actor": "ceo", "note": "no"}).status_code == 200
    _tick(engine)
    assert _step(engine, run_id)["status"] == "failed"

    offered = _skip_and_retry(engine, run_id)
    assert offered["skip_step"]["enabled"], offered
    assert offered["retry_step"]["enabled"], offered
    skipped = client.post(f"/v1/delivery-workflows/runs/{run_id}/skip-step", json={
        "project_id": HOME, "actor": "ceo", "step_id": "deploy", "reason": "not this release"})
    assert skipped.status_code == 200, skipped.text
    assert _step(engine, run_id)["status"] == "skipped"
    with engine.begin() as c:
        approved = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                             {"d": decision_id}).scalar()
    assert approved == "rejected"


def test_a_waived_gate_ships_only_to_the_stack_target_it_was_waived_for(
        client, engine, stack, monkeypatch):
    from boss_engine.services import stack_deploy

    real = stack_deploy.stack_target_for_deploy
    calls = {"n": 0}

    def flips(*args, **kwargs):
        # The gate sees the stack target; by execution it is gone (deactivated, a second target
        # added, the host changed). The step must not fall through to an ungated deploy.
        calls["n"] += 1
        return real(*args, **kwargs) if calls["n"] == 1 else None

    monkeypatch.setattr(stack_deploy, "stack_target_for_deploy", flips)
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)

    step = _step(engine, run_id)
    # Stopped for the operator, settled (not retried into the same answer).
    assert step["status"] in ("failed", "deadlettered"), step
    assert "no longer resolves" in step["last_error"]
    with engine.begin() as c:
        snapshot = c.execute(text("SELECT binding_snapshot FROM workflow_step_runs "
                                  "WHERE workflow_run_id = :r AND step_id = 'deploy'"),
                             {"r": run_id}).scalar_one()
        planned = c.execute(text("SELECT count(*) FROM stack_deployments WHERE project_id = :p"),
                            {"p": HOME}).scalar_one()
        legacy = c.execute(text("SELECT count(*) FROM deployment_runs WHERE project_id = :p"),
                           {"p": HOME}).scalar_one()
    assert snapshot["stack_gate_target_id"] == stack["target"]["id"]
    assert planned == 0 and legacy == 0


def test_an_approver_not_on_the_app_cannot_release_a_stack_deploy(client, engine, stack):
    from boss_engine.services import agent_assignment

    with engine.begin() as c:
        agent_assignment.assign(c, project_id=HOME, app_name="Shop", role_keys=["pm"],
                                actor="ceo")
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'),
                         title="Shop")
    _tick(engine)

    step = _step(engine, run_id)
    # Stopped for the operator, settled (not retried into the same answer).
    assert step["status"] in ("failed", "deadlettered"), step
    assert "release_manager" in step["last_error"] and "not assigned" in step["last_error"]
    with engine.begin() as c:
        planned = c.execute(text("SELECT count(*) FROM stack_deployments WHERE project_id = :p"),
                            {"p": HOME}).scalar_one()
    assert planned == 0
