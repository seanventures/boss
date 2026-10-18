"""A deploy that promises a person's approval always asks for it (P0 safety class).

D2, 2026-10-18: a HelpDesk "＋ Add a feature" run drew its Deploy step as "Ships only when you
say yes" and its start form said nothing deploys without the person's approval — and the
staging policy (set 2026-10-12 to ship without asking) deployed it with nobody asked. The words
and the behaviour came from two places.

Now they come from one (deploy_policy.step_answer):
  * a Deploy step PROMISES A PERSON unless it says `approval.follows_policy` — no environment
    policy answers for it, at its gate or on the deployment it starts;
  * a step that follows the policy is shown as what the policy will do, everywhere.

These tests fail if any surface promises an approval the runtime would skip: the runner's own
gate decision is compared, step by step, with what the plan, the asks endpoint, the library
outline and the explained plan say — for every catalogue workflow, under a permissive policy.
"""
from __future__ import annotations

import inspect
import re

from sqlalchemy import text

from boss_engine.services import deploy_policy, workflow_catalogue, workflows
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.deploy_policy import (PERSON_SAYS, follows_policy, pre_approval,
                                                promises_a_person, set_approval_policy,
                                                step_answer)
from boss_engine.services.workflow_asks import asks_for_spec, project_asks
from boss_engine.services.workflow_capabilities import effective_approval

from .test_workflow_capability_runner import _publish, eng, project  # noqa: F401

#: Words that tell a person they approve the release. Matched on what the catalogue SHOWS a
#: person (description, objective, the start form's field texts) — a safety check on our own
#: copy, not an interpretation of anybody's request.
PROMISE = re.compile(
    r"\b(person|someone|you)\b[^.]{0,60}\b(approv\w*|says? yes|signs? off)\b[^.]{0,60}"
    r"\b(release|ships?|deploy\w*|production|migration runs)\b"
    r"|gated at the door to production|gates where[^.]*production", re.I)


def _effective(entry):
    spec = workflow_catalogue.entry_spec(entry)
    return workflows.validate_workflow_spec(
        {"entry_step": spec["entry_step"], "steps": [dict(s) for s in entry["steps"]]})["steps"]


def _deploys(steps):
    return [s for s in steps if (s.get("binding") or {}).get("capability") == "deploy"]


def _shown_words(entry) -> str:
    schema = workflow_catalogue.input_schema_for(entry)
    fields = " ".join(f"{p.get('title', '')}. {p.get('description', '')}."
                      for p in (schema.get("properties") or {}).values())
    return f"{entry['description']} {entry.get('objective') or ''} {fields}"


def _person_gate_before(steps, deploy) -> bool:
    """A release gate (release_approval) earlier in the chain answers for the release."""
    for step in steps:
        if step is deploy:
            return False
        if (step.get("binding") or {}).get("capability") == "release_approval":
            return True
    return False


def test_a_deploy_step_promises_a_person_unless_it_says_otherwise():
    deploy = {"binding": {"capability": "deploy"}, "approval": {"mode": "manual"}}
    assert promises_a_person(deploy) and not follows_policy(deploy)
    opted = {**deploy, "approval": {"mode": "manual", "follows_policy": True}}
    assert follows_policy(opted) and not promises_a_person(opted)
    # Only a Deploy step can opt in; the flag survives validation there and nowhere else.
    assert effective_approval("deploy", {"follows_policy": True}).get("follows_policy") is True
    assert "follows_policy" not in effective_approval("notify", {"follows_policy": True})
    assert not follows_policy({"binding": {"capability": "notify"},
                               "approval": {"follows_policy": True}})


def test_every_catalogue_workflow_that_promises_a_person_asks_one():
    """THE COPY AUDIT. A workflow whose words say a person approves the release may not have a
    deploy the environment's policy could ship unasked — unless a release gate precedes it."""
    for entry in workflow_catalogue.CATALOGUE:
        steps = _effective(entry)
        if not PROMISE.search(_shown_words(entry)):
            continue
        for deploy in _deploys(steps):
            assert promises_a_person(deploy) or _person_gate_before(steps, deploy), (
                f"{entry['key']} promises a person at the release, and its step "
                f"{deploy['id']!r} follows the environment's policy with no release gate first")


def test_the_d2_path_asks_a_person_whatever_the_staging_policy_says(eng, project):  # noqa: F811
    """Minor Feature (the "Add a feature" path D2 ran) under a staging policy that ships without
    asking: its Deploy step still opens a gate, and every surface says a person is asked."""
    entry = next(e for e in workflow_catalogue.CATALOGUE if e["key"] == "minor_feature")
    steps = _effective(entry)
    with eng.begin() as c:
        set_approval_policy(c, project_id=project, environment="staging",
                            requires_approval=False, actor="usr_owner")
        try:
            deploy = next(s for s in _deploys(steps) if not
                          (s["binding"].get("inputs") or {}).get("pass_2_of"))
            context = {"run_id": "wfr_promise_check", "input": {"title": "t"}}
            assert runner._gate_subject(c, project, deploy, context) is not None
            assert step_answer(c, project, deploy)["who"] == "you"
            # The deployment it starts may not run on the policy either.
            assert pre_approval(c, project, "staging", person_required=True) is None
            assert pre_approval(c, project, "staging")["source"] == "policy"
        finally:
            c.execute(text("DELETE FROM deployment_environment_policies WHERE project_id=:p"),
                      {"p": project})


def test_no_surface_promises_an_approval_the_runner_would_skip(eng, project):  # noqa: F811
    """THE CLASS TEST. Every catalogue workflow, every pass-1 Deploy step, with staging set to
    ship without asking and with no policy at all: what the runner does at the step (a gate, or
    none) is what the asks endpoint, the library outline and the plan's words say."""
    for permissive in (True, False):
        with eng.begin() as c:
            if permissive:
                set_approval_policy(c, project_id=project, environment="staging",
                                    requires_approval=False, actor="usr_owner")
            try:
                for entry in workflow_catalogue.CATALOGUE:
                    steps = _effective(entry)
                    spec = {"entry_step": steps[0]["id"], "steps": steps}
                    asks = {a["step_id"]: a for a in asks_for_spec(
                        spec, prd_auto_approve=True,
                        deploy_answer=lambda s: step_answer(c, project, s))
                        if a["kind"] != "merge"}
                    outline = {o["id"]: o for o in workflows._step_outline(
                        steps, {}, deploy_answer=lambda s: step_answer(c, project, s))}
                    for deploy in _deploys(steps):
                        if (deploy["binding"].get("inputs") or {}).get("pass_2_of"):
                            continue   # refused before it asks without a pass 1 (two_pass)
                        context = {"run_id": "wfr_promise_check", "input": {"title": "t"}}
                        gated = runner._gate_subject(c, project, deploy, context) is not None
                        where = f"{entry['key']}.{deploy['id']} (permissive={permissive})"
                        ask = asks[deploy["id"]]
                        assert (ask["who"] == "you") == gated, where
                        assert (ask["says"] == PERSON_SAYS) == gated, where
                        assert (outline[deploy["id"]]["approval"] == "manual") == gated, where
                        assert (outline[deploy["id"]]["says"] == PERSON_SAYS) == gated, where
                        if promises_a_person(deploy):
                            assert gated, f"{where} promised a person and was not asked"
                        if permissive and follows_policy(deploy):
                            assert not gated and "staging policy" in ask["says"], where
            finally:
                c.execute(text("DELETE FROM deployment_environment_policies "
                               "WHERE project_id=:p"), {"p": project})


def test_the_asks_endpoint_and_the_explained_plan_read_the_policy(eng, project):  # noqa: F811
    with eng.begin() as c:
        for key in ("minor_feature", "incremental_build"):
            entry = next(e for e in workflow_catalogue.CATALOGUE if e["key"] == key)
            steps = workflow_catalogue._staffed_for(entry, frozenset())["steps"]
            _publish(c, project, key, steps, steps[0]["id"])
        set_approval_policy(c, project_id=project, environment="staging",
                            requires_approval=False, actor="usr_owner")
        try:
            got = project_asks(c, None, project, prd_auto_approve=True)["workflows"]
            minor = {a["step_id"]: a for a in got["minor_feature"]}
            assert minor["deploy"]["who"] == "you" and minor["deploy"]["promised"] is True
            inc = {a["step_id"]: a for a in got["incremental_build"]}
            assert inc["inc1_deploy"]["who"] == "boss"
            assert inc["inc1_deploy"]["policy"] == "staging"
            assert inc["inc1_deploy"]["says"] == "Ships automatically under your staging policy"

            from boss_engine.services.workflow_explain import explain_version

            plan = {s["id"]: s for s in explain_version(
                c, project_id=project, workflow_key="minor_feature")["steps"]}
            assert plan["deploy"]["approval"] == "manual"
            assert "whatever the environment's deploy policy" in plan["deploy"]["approval_note"]
            plan = {s["id"]: s for s in explain_version(
                c, project_id=project, workflow_key="incremental_build")["steps"]}
            assert plan["inc1_deploy"]["approval"] == "policy"
            assert plan["inc1_deploy"]["approval_note"].startswith(
                "Ships automatically under your staging policy")
        finally:
            c.execute(text("DELETE FROM deployment_environment_policies WHERE project_id=:p"),
                      {"p": project})


def test_both_deploy_paths_carry_the_promise_to_the_deployment():
    """The runner hands a promised step's `person_required` to the deployment on both paths, and
    both paths hand it to pre_approval — so a gate skipped for any other reason still cannot
    let the policy ship a promised deploy."""
    from boss_engine.services import (delivery_pipeline, governed_host_deploy, resource_plane,
                                      stack_deploy)
    from boss_engine.tools import builtin

    src = inspect.getsource(runner)
    assert "person_required=promises_a_person(step)" in src          # stack path
    assert '"person_required": True' in src                          # deploy_app path
    assert "policy_answers_for_step(connection, project_id, spec_step" in src
    for fn in (stack_deploy.plan_stack_deploy, governed_host_deploy.plan_host_deployment):
        assert "person_required=person_required" in inspect.getsource(fn)
    for fn in (stack_deploy.plan_for_workflow_step, delivery_pipeline.deploy_build,
               resource_plane.ResourcePlaneService.plan_deployment_run):
        assert "person_required=person_required" in inspect.getsource(fn)
    assert 'person_required=args.get("person_required") is True' in inspect.getsource(builtin)
    assert "if person_required:" in inspect.getsource(deploy_policy.pre_approval)
