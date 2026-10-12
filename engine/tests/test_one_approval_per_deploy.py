"""A deploy asks a person once, or not at all when its environment's policy says so.

HelpDesk's workflow asked twice — the Deploy step's gate, then the deployment's own card — and
the second card sat in Approvals while the step looked approved. And the per-environment policy
(deployment_environment_policies.requires_approval) existed and was read by nothing.

deploy_policy.pre_approval is the one answer both deploy paths read:
  * a released Deploy step's approved gate, passed explicitly, approves ONE deployment;
  * otherwise the environment's policy; no row means ask.
"""
from __future__ import annotations

import inspect
import json

from sqlalchemy import text

from boss_engine.services import deploy_policy, governed_host_deploy, stack_deploy
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.deploy_policy import deploy_policy as policy_of
from boss_engine.services.deploy_policy import pre_approval, set_approval_policy

from .test_workflow_capability_runner import _publish, _start, eng, project  # noqa: F401


def test_no_policy_means_ask(eng, project):  # noqa: F811
    with eng.begin() as c:
        assert policy_of(c, project, "staging")["requires_approval"] is True
        assert pre_approval(c, project, "staging") is None
        assert pre_approval(c, project, "production") is None


def test_a_staging_policy_set_by_a_person_runs_deploys_without_asking(eng, project):  # noqa: F811
    with eng.begin() as c:
        policy = set_approval_policy(c, project_id=project, environment="Staging",
                                     requires_approval=False, actor="usr_owner",
                                     note="set at the operator's request")
        assert policy["requires_approval"] is False and policy["set_by"] == "usr_owner"
        found = pre_approval(c, project, "staging")
        assert found["source"] == "policy" and found["decision_id"] is None
        assert found["approved_by"] == f"policy:{policy['policy_id']}"
        # Production keeps asking: nobody set it otherwise.
        assert pre_approval(c, project, "production") is None
        events = c.execute(text("SELECT actor, data FROM events WHERE project_id = :p AND "
                                "event_type = 'deployment_environment_policy.approval_set'"),
                           {"p": project}).mappings().all()
        assert [e["actor"] for e in events] == ["usr_owner"]
        set_approval_policy(c, project_id=project, environment="staging",
                            requires_approval=True, actor="usr_owner")
        assert pre_approval(c, project, "staging") is None
        c.execute(text("DELETE FROM deployment_environment_policies WHERE project_id = :p"),
                  {"p": project})


def _gated_deploy_step(c, project, *, released=True, capability="deploy"):  # noqa: F811
    _, version = _publish(c, project, f"gated_{capability}_{released}", [
        {"id": "deploy", "kind": "capability",
         "binding": {"capability": capability,
                     "inputs": {"run_id": "crun_x"} if capability == "deploy"
                     else {"message": "hi"}}, "next": []},
    ], "deploy")
    run = _start(c, project, version["id"], {})
    decision = f"dec_gate_{run['id'][-8:]}"
    c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_by) "
                   "VALUES (:d, :p, 'Deploy?', 'approved', 'usr_person')"),
              {"d": decision, "p": project})
    snapshot = {"capability": capability, **({"released_for": decision} if released else {})}
    c.execute(text("UPDATE workflow_step_runs SET decision_id = :d, binding_snapshot = "
                   "COALESCE(binding_snapshot, '{}'::jsonb) || CAST(:s AS JSONB) "
                   "WHERE workflow_run_id = :r"),
              {"d": decision, "s": json.dumps(snapshot), "r": run["id"]})
    return decision


def test_a_released_deploy_gate_approves_its_deploy_once(eng, project):  # noqa: F811
    with eng.begin() as c:
        decision = _gated_deploy_step(c, project)
        found = pre_approval(c, project, "staging", gate_decision_id=decision)
        assert found == {"source": "gate", "decision_id": decision, "approved_by": "usr_person"}
        # Used: a second deployment naming the same gate asks again.
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, created_by) VALUES "
                       "(:t, :p, 'A', 'r', 'staging', 'ssh', 'u@h:22', 't')"),
                  {"t": f"deptgt_{decision}", "p": project})
        c.execute(text("INSERT INTO deployment_runs (id, project_id, deployment_target_id, "
                       "decision_id, app_name, environment, repo, target_kind, target_ref, "
                       "created_by) VALUES (:i, :p, :t, :d, 'A', 'staging', 'r', 'ssh', "
                       "'u@h:22', 't')"),
                  {"i": f"deprun_{decision}", "p": project, "t": f"deptgt_{decision}",
                   "d": decision})
        assert pre_approval(c, project, "staging", gate_decision_id=decision) is None
        c.execute(text("DELETE FROM deployment_runs WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM deployment_targets WHERE project_id = :p"), {"p": project})


def test_any_other_decision_named_is_not_an_approval(eng, project):  # noqa: F811
    with eng.begin() as c:
        unreleased = _gated_deploy_step(c, project, released=False)
        assert pre_approval(c, project, "staging", gate_decision_id=unreleased) is None
        not_a_deploy = _gated_deploy_step(c, project, capability="notify")
        assert pre_approval(c, project, "staging", gate_decision_id=not_a_deploy) is None
        c.execute(text("INSERT INTO decisions (id, project_id, question, status) "
                       "VALUES ('dec_pending_x', :p, 'Deploy?', 'pending')"), {"p": project})
        assert pre_approval(c, project, "staging", gate_decision_id="dec_pending_x") is None


def test_both_paths_and_the_runner_use_the_one_answer():
    host = inspect.getsource(governed_host_deploy.plan_host_deployment)
    stack = inspect.getsource(stack_deploy.plan_stack_deploy)
    for src in (host, stack):
        assert "pre_approval(" in src and "approval_decision_id" in src
        assert "record_pre_approval(" in src
    # The runner passes the step's released gate explicitly on both paths.
    dispatch = inspect.getsource(runner)
    assert 'approval_decision_id=str(context.get("approval_decision_id") or "") or None' in dispatch
    assert '"approval_decision_id": str(context["approval_decision_id"])' in dispatch
    assert "requires_approval" in inspect.getsource(deploy_policy.deploy_policy)
