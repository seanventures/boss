"""Every workflow that builds code gets its More tests pass, and the app never waits for it.

Operator, 2026-10-13: pass 1 builds the app with a lean P0/P1 suite; pass 2 ("More tests", P2)
extends the same branch and PR. Then: "for the user to see the app running, does it need to wait
for P2?" — it must not. NO DEPLOY WAITS FOR PASS 2, IN ANY ENVIRONMENT: right after pass 1 the
app is previewed and the deploy ships pass 1 under its environment's policy; pass 2 runs
alongside; when it lands commits, "Deploy again" ships it as a new deploy under the same policy.
The rule lives once in services/two_pass.py, applied where a version becomes a run's steps
(workflows._published_spec) and to the catalogue at import.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import build_scope, two_pass, workflows
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_catalogue import CATALOGUE, entry_spec

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures by name


def _cap(step):
    return (step.get("binding") or {}).get("capability")


def _by_id(spec):
    return {s["id"]: s for s in spec["steps"]}


def _reach(spec, start, *, stop_at_builds=True):
    by_id = _by_id(spec)
    seen, stack = set(), [start]
    while stack:
        for t in two_pass._targets(by_id[stack.pop()]):
            if t in by_id and t not in seen:
                seen.add(t)
                if not (stop_at_builds and _cap(by_id[t]) == "build"):
                    stack.append(t)
    return seen


def _first_deploys(spec, build_id):
    """The deploys a build reaches before another build: the ones that ship pass 1."""
    by_id = _by_id(spec)
    return [i for i in _reach(spec, build_id) if _cap(by_id[i]) == "deploy"
            and not by_id[i]["binding"]["inputs"].get("pass_2_of")]


def _its_more_tests(spec, build_id):
    by_id = _by_id(spec)
    return next(i for i in _reach(spec, build_id) if _cap(by_id[i]) == "more_tests")


BUILDERS = [e for e in CATALOGUE if any(_cap(s) == "build" for s in e["steps"])]

#: The catalogue workflows that build with NO pass 2, each with its reason. Opting out is a
#: decision a template states (`"more_tests": False`), never an accident.
OPTED_OUT = {
    # Users are failing now: the fix's own criteria demand the regression test and its gates run
    # the whole existing suite; a second pass and its second deploy would hold the recovery.
    "incident_response",
}


def test_the_catalogue_workflows_that_build_are_the_ones_expected():
    assert {e["key"] for e in BUILDERS} == {
        "standard_product_delivery", "minor_feature", "major_feature", "fast_fix",
        "backend_service_setup", "ux_certification", "security_hardening", "data_migration",
        "compliance_release", "third_party_integration", "deprecation_sunset",
        "incremental_build", "incident_response"}


def _the_rule_holds(spec, key):
    by_id = _by_id(spec)
    assert two_pass.builds_without_more_tests(spec) == [], key
    for build in [s for s in spec["steps"] if _cap(s) == "build"]:
        more = _its_more_tests(spec, build["id"])
        # SOMETHING TO LOOK AT RIGHT AFTER PASS 1: a preview, before pass 2 is anywhere near.
        first = by_id[build["next"][0]]
        chain = [first]
        while _cap(chain[-1]) != "preview":
            chain.append(by_id[chain[-1]["next"][0]])
        assert more not in [s["id"] for s in chain], key
        # NO DEPLOY WAITS FOR PASS 2: the deploys that ship pass 1 are not behind More tests.
        firsts = _first_deploys(spec, build["id"])
        for deploy in firsts:
            assert deploy not in _reach(spec, more), (key, deploy)
            assert by_id[deploy]["binding"]["inputs"]["run_id"]["from_step"] != more
            # …and each gets a Deploy again, from pass 2, after a join that waits for it.
            again = [s for s in spec["steps"]
                     if (s.get("binding") or {}).get("inputs", {}).get("pass_2_of") == deploy]
            assert len(again) == 1, (key, deploy)
            assert again[0]["binding"]["inputs"]["run_id"] == {
                "from_step": more, "field": "code_task_run_id"}
            join = next(s for s in spec["steps"] if s["kind"] == "join"
                        and more in s["wait_for"] and again[0]["id"] in _reach(spec, s["id"]))
            assert join


def test_only_the_named_templates_opt_out_and_the_opt_out_holds_at_run_time():
    assert {e["key"] for e in CATALOGUE if e.get("more_tests") is False} == OPTED_OUT
    for entry in CATALOGUE:
        if entry["key"] in OPTED_OUT:
            run_spec = workflows._published_spec(entry_spec(entry))
            assert not [s for s in run_spec["steps"] if _cap(s) == "more_tests"], entry["key"]


@pytest.mark.parametrize("entry", [e for e in BUILDERS if e["key"] not in OPTED_OUT],
                         ids=lambda e: e["key"])
def test_every_catalogue_build_gets_pass_2_and_nothing_waits_for_it(entry):
    for spec in (entry_spec(entry), workflows._published_spec(entry_spec(entry))):
        _the_rule_holds(spec, entry["key"])
        assert two_pass.with_more_tests(spec) == spec  # applying the rule again adds nothing


def test_no_catalogue_entry_wires_more_tests_by_hand():
    import inspect

    from boss_engine.services import workflow_catalogue

    assert '_step("more_tests"' not in inspect.getsource(workflow_catalogue)


def test_readiness_stays_in_front_because_it_writes_the_same_branch():
    spd = _by_id(next(e for e in CATALOGUE if e["key"] == "standard_product_delivery"))
    assert spd["preview"]["next"] == ["plrr"] and spd["plrr"]["next"] == ["pass_2"]
    assert spd["deploy"]["binding"]["inputs"]["run_id"]["from_step"] == "plrr"


def test_a_hotfix_gets_a_lighter_pass_2_and_a_preview_that_never_blocks():
    fast = _by_id(next(e for e in CATALOGUE if e["key"] == "fast_fix"))
    assert fast["more_tests"]["binding"]["inputs"]["depth"] == two_pass.LIGHT
    assert fast["build"]["next"] == ["preview"]
    assert fast["preview"]["binding"]["inputs"]["advisory"] is True
    assert "MORE TESTS PASS (LIGHT)" in runner._more_tests_brief(light=True)
    scope = build_scope.decide("fix", "Playwright\n" + runner._more_tests_brief(light=True), [])
    assert scope["more_tests"] and scope["more_tests_light"] and not scope["e2e"]
    token = build_scope.set_scope(scope)
    try:
        assert g.test_budget(["c"], 20) == 20  # pass 1's budget, not twice it
    finally:
        build_scope._SCOPE.reset(token)


def test_pass_1_writes_p0_p1_and_pass_2_writes_p2():
    clause = g._test_scope_clause(["a", "b"])
    assert "P0" in clause and "P1" in clause and "sign-in" in clause
    token = build_scope.set_scope(build_scope.decide("x", runner._more_tests_brief(), []))
    try:
        assert "P2" in g._test_scope_clause(["a", "b"])
        assert "At most 40 NEW test cases" in g._test_scope_clause(["a"], 20)
    finally:
        build_scope._SCOPE.reset(token)
    assert "P2" in runner._more_tests_brief()


# ── A custom workflow ──────────────────────────────────────────────────────────────────────────

def _custom(**top):
    return {
        "entry_step": "build", **top,
        "steps": [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {
                 "title": {"from_input": "title"}, "instructions": {"from_input": "directive"}}},
             "next": ["ship"]},
            {"id": "ship", "kind": "capability",
             "binding": {"capability": "deploy", "inputs": {
                 "run_id": {"from_step": "build", "field": "code_task_run_id"}}},
             "next": []},
        ],
    }


CUSTOM_IDS = ["build", "preview", "pass_2", "more_tests", "ship", "pass_2_joined", "ship_again"]


def test_a_custom_workflow_gets_pass_2_alongside_when_it_is_resolved_for_a_run():
    spec = workflows._published_spec(_custom())
    assert [s["id"] for s in spec["steps"]] == CUSTOM_IDS
    by_id = _by_id(spec)
    assert by_id["preview"]["binding"]["inputs"]["advisory"] is True
    assert by_id["pass_2"]["branches"] == ["more_tests", "ship"]
    assert by_id["ship"]["binding"]["inputs"]["run_id"]["from_step"] == "build"
    assert by_id["ship_again"]["binding"]["inputs"]["pass_2_of"] == "ship"
    _the_rule_holds(spec, "custom")


def test_a_custom_workflow_can_opt_out_or_ask_for_a_light_pass():
    assert [s["id"] for s in workflows._published_spec(_custom(more_tests=False))["steps"]] == [
        "build", "ship"]
    one_off = _custom()
    one_off["steps"][0]["more_tests"] = False
    assert len(workflows._published_spec(one_off)["steps"]) == 2
    light = workflows._published_spec(_custom(more_tests="light"))
    assert _by_id(light)["more_tests"]["binding"]["inputs"]["depth"] == "light"


def test_each_of_two_builds_gets_its_own_pass_2_and_the_second_builds_after_the_first():
    spec = _custom()
    spec["steps"][0]["next"] = ["build_2"]
    spec["steps"].insert(1, {"id": "build_2", "kind": "capability",
                             "binding": {"capability": "build",
                                         "inputs": dict(spec["steps"][0]["binding"]["inputs"])},
                             "next": ["ship"]})
    out = workflows._published_spec(spec)
    ids = [s["id"] for s in out["steps"]]
    assert "more_tests" in ids and "build_2_more_tests" in ids
    # Pass 2 of the first build writes the branch, so the second build comes after it.
    assert ids.index("more_tests") < ids.index("build_2")
    _the_rule_holds(out, "two builds")


def test_contains_reads_a_document_a_step_wrote():
    payload = {"steps": {"meeting": {"output": {"prd_markdown": "…\n## Increment 2: votes"}}}}
    yes = {"path": "steps.meeting.output.prd_markdown", "op": "contains",
           "value": "## increment 2"}
    assert workflows.evaluate_expression(yes, payload)
    assert not workflows.evaluate_expression({**yes, "value": "## Increment 3"}, payload)


# ── At run time ────────────────────────────────────────────────────────────────────────────────

def _run(c, project, steps=None):
    from .test_a_failed_step_stops_the_run import _publish, _start

    _, version = _publish(c, project, f"two_pass_{uuid.uuid4().hex[:6]}",
                          steps or _custom()["steps"], "build")
    return _start(c, project, version["id"], {"title": "t", "directive": "d"})


def _set(c, run_id, step_id, status, output):
    import json

    c.execute(text("UPDATE workflow_step_runs SET status=:s, output=CAST(:o AS JSONB), "
                   "completed_at=now() WHERE workflow_run_id=:r AND step_id=:i"),
              {"s": status, "o": json.dumps(output), "r": run_id, "i": step_id})


def test_a_run_walks_the_new_shape_and_older_runs_keep_theirs(eng, project):  # noqa: F811
    with eng.begin() as c:
        run = _run(c, project)
        rows = set(c.execute(text("SELECT step_id FROM workflow_step_runs "
                                  "WHERE workflow_run_id=:r"), {"r": run["id"]}).scalars())
        assert rows == set(CUSTOM_IDS)
        assert [s["id"] for s in workflows._run_spec(c, run["id"])["steps"]] == CUSTOM_IDS
        # A run started under the rc10.2 inline shape keeps it: deploy reads More tests.
        c.execute(text("DELETE FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id "
                       "IN ('preview','pass_2','pass_2_joined','ship_again')"), {"r": run["id"]})
        inline = workflows._run_spec(c, run["id"])
        assert [s["id"] for s in inline["steps"]] == ["build", "more_tests", "ship"]
        # A run started before any pass 2 keeps its version as it was.
        c.execute(text("DELETE FROM workflow_step_runs WHERE workflow_run_id=:r "
                       "AND step_id='more_tests'"), {"r": run["id"]})
        old = workflows._run_spec(c, run["id"])
        assert [s["id"] for s in old["steps"]] == ["build", "ship"]


def test_deploy_again_ships_only_what_pass_2_landed_and_says_why_not(eng, project):  # noqa: F811
    with eng.begin() as c:
        run = _run(c, project)
        spec = _by_id(workflows._run_spec(c, run["id"]))
        again = spec["ship_again"]

        def why():
            return runner.pass_two_redeploy_refusal(c, run["id"], again, "ship")

        assert "first deploy did not ship" in why()
        _set(c, run["id"], "ship", "succeeded", {"capability": "deploy", "deployment_id": "d1"})
        _set(c, run["id"], "more_tests", "succeeded", {
            "capability": "more_tests", "advisory_failed": True, "base_run_id": "crun_p1",
            "code_task_run_id": "crun_p1"})
        assert "did not finish" in why()
        p2 = f"crun_p2_{uuid.uuid4().hex[:6]}"
        _set(c, run["id"], "more_tests", "succeeded", {
            "capability": "more_tests", "base_run_id": "crun_p1", "code_task_run_id": p2})
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, title, state) "
                       "VALUES (:i, :p, 'completed', 'More tests', CAST(:s AS JSONB))"),
                  {"i": p2, "p": project, "s": '{"files_changed": 0, "test_files": [], '
                                                '"impl_files": []}'})
        assert "changed nothing" in why()
        c.execute(text("UPDATE code_task_runs SET state = CAST(:s AS JSONB) WHERE id = :i"),
                  {"i": p2, "s": '{"files_changed": 3, "test_files": ["a.test.ts", "b.test.ts"],'
                                 ' "impl_files": ["x.ts"], "summary": "done\\nBUGS FIXED:\\n'
                                 '- empty title accepted"}'})
        assert why() is None
        found = runner.pass_two_summary(c, run["id"], "more_tests")
        assert found["changed"] and found["bugs_fixed"] == ["empty title accepted"]
        assert "fixed 1 bug" in runner.pass_two_sentence(found)
        c.execute(text("DELETE FROM code_task_runs WHERE id=:i"), {"i": p2})


def test_a_deploy_asks_as_its_environment_says_and_never_waits_for_pass_2(eng, project):  # noqa: F811
    from boss_engine.services.deploy_policy import set_approval_policy

    with eng.begin() as c:
        run = _run(c, project)
        context = runner._run_context(c, run["id"])
        spec = _by_id(workflows._run_spec(c, run["id"]))
        # No policy for the environment: a person is asked, for pass 1's deploy, now.
        assert runner._gate_subject(c, project, spec["ship"], context) is not None
        set_approval_policy(c, project_id=project, environment="staging",
                            requires_approval=False, actor="usr_owner")
        # A Deploy step PROMISES A PERSON: the staging policy does not answer for it (D2,
        # test_a_promised_approval_is_always_asked)…
        assert runner._gate_subject(c, project, spec["ship"], context) is not None
        # …unless the step lets the policy decide: then nobody is asked, at the step either.
        opted = {**spec["ship"], "approval": {**(spec["ship"].get("approval") or {}),
                                              "follows_policy": True}}
        assert runner._gate_subject(c, project, opted, context) is None
        # A Deploy again that will ship nothing asks nobody and says why when it runs.
        assert runner._gate_subject(c, project, spec["ship_again"], context) is None
        c.execute(text("DELETE FROM deployment_environment_policies WHERE project_id=:p"),
                  {"p": project})


def test_an_automatic_preview_that_cannot_run_says_so_and_does_not_block(eng, project,  # noqa: F811
                                                                         monkeypatch):
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    def broken(*a, **k):
        raise runner.CapabilityExecutionError("no preview host is configured")

    monkeypatch.setattr(runner, "_perform_capability", broken)
    with eng.begin() as c:
        run = _run(c, project)
        out = runner._advisory_preview(
            c, None, project_id=project, actor="test", step={"id": "preview"},
            context=runner._run_context(c, run["id"]), capability=CAPABILITIES["preview"],
            inputs={"run_id": "crun_x"})
        assert out["status"] == "not_applicable" and "could not be previewed" in out["note"]
        assert "no preview host" in out["note"]
        # …and the deploy is not held for a preview BOSS added.
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded' "
                       "WHERE workflow_run_id=:r AND step_id='preview'"), {"r": run["id"]})
        assert runner._preview_missing_for_run(c, {"run_id": run["id"]}) is None


def test_a_later_increment_builds_on_the_newest_pass(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    called = {}

    def tool(ctx, name, project_id, actor, args):
        called.update(name=name, **args)
        return {"status": "extending", "result": {"run_id": "crun_inc2"}}

    monkeypatch.setattr(runner, "_invoke_tool", tool)
    with eng.begin() as c:
        run = _run(c, project)
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded', completed_at=now(), "
                       "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
                  {"r": run["id"], "o": '{"code_task_run_id": "crun_inc1"}'})
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded', "
                       "completed_at=now() + interval '1 second', output=CAST(:o AS JSONB) "
                       "WHERE workflow_run_id=:r AND step_id='more_tests'"),
                  {"r": run["id"], "o": '{"code_task_run_id": "crun_inc1_p2"}'})
        out = runner._perform_capability(
            c, None, project_id=project, actor="test", step={"id": "inc2_build"},
            context={"run_id": run["id"]}, key="build", capability=CAPABILITIES["build"],
            inputs={"title": "t", "instructions": "INCREMENT 2", "extend": True})
    assert called["name"] == "extend_code_task" and called["run_id"] == "crun_inc1_p2"
    assert "extend" not in called and out["status"] == "awaiting"
    assert out["code_task_run_id"] == "crun_inc2"


# ── Incremental Build ──────────────────────────────────────────────────────────────────────────

def test_incremental_build_ships_the_basics_first_then_improves_in_steps():
    entry = next(e for e in CATALOGUE if e["key"] == "incremental_build")
    assert entry["description"] == "Builds the basics first, deploys them, then improves in steps."
    spec = entry_spec(entry)
    by_id = _by_id(spec)
    # Plan → approve → increment 1 → (increment 2..4 only if the plan has them) → report.
    assert spec["entry_step"] == "meeting" and "## Increment 1" in entry["objective"]
    for k in range(1, 5):
        group = [s for s in spec["steps"] if s["id"].startswith(f"inc{k}_")]
        # EVERY INCREMENT IS ITS OWN GROUP: one prefix, one title.
        assert all(str(s.get("title", "")).startswith(f"Increment {k} · ") for s in group)
        assert {_cap(s) for s in group} >= {"build", "preview", "more_tests", "deploy"}
        if k > 1:
            planned = by_id[f"inc{k}_planned"]
            assert planned["expression"]["value"] == f"## Increment {k}"
            assert planned["on_true"] == f"inc{k}_build"
            assert by_id[planned["on_false"]]["next"] == ["report"]
            # Later increments build on this run's branch, as more commits on the same PR.
            assert by_id[f"inc{k}_build"]["binding"]["inputs"]["extend"] is True
    # The first increment is live before any pass 2 has finished, and every increment after it
    # starts once the one before has its pass 2 (they write the same branch).
    assert "inc1_deploy" not in _reach(spec, "inc1_more_tests")
    assert "inc2_build" in _reach(spec, "inc1_pass_2_joined")
    assert workflows.validate_workflow_spec(spec)


# ── Pass 1's deploy ships pass 1 ───────────────────────────────────────────────────────────────

PASS_ONE = "a" * 40


def test_more_tests_keeps_the_head_pass_one_left_for_the_deploy(eng, project, monkeypatch):  # noqa: F811
    """Pass 2 pushes beside the deploy: the first deploy plans the head pass 1 left (read when
    pass 2 started), never `pull/N`, which by then may hold pass 2's commits."""
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    monkeypatch.setattr(runner, "_invoke_tool", lambda ctx, name, pid, actor, args: {
        "status": "extending", "result": {"run_id": "crun_p2"}})
    monkeypatch.setattr(runner, "pass_one_head", lambda c, settings, pid, base: PASS_ONE)
    with eng.begin() as c:
        run = _run(c, project)
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded', completed_at=now(), "
                       "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
                  {"r": run["id"], "o": '{"code_task_run_id": "crun_p1"}'})
        out = runner._perform_capability(
            c, None, project_id=project, actor="test", step={"id": "more_tests"},
            context={"run_id": run["id"]}, key="more_tests",
            capability=CAPABILITIES["more_tests"], inputs={})
        assert out["status"] == "awaiting" and out[runner.PASS_ONE_HEAD] == PASS_ONE, out
        c.execute(text("UPDATE workflow_step_runs SET status='waiting', output=CAST(:o AS JSONB) "
                       "WHERE workflow_run_id=:r AND step_id='more_tests'"),
                  {"r": run["id"], "o": __import__("json").dumps(out)})
        assert runner.pinned_pass_one_commit(c, run["id"]) == PASS_ONE
        # A newer pass 1 (a retried build) than the pin: the pull request's head is right again.
        c.execute(text("UPDATE workflow_step_runs SET output=CAST(:o AS JSONB), "
                       "completed_at=now() + interval '1 minute' "
                       "WHERE workflow_run_id=:r AND step_id='build'"),
                  {"r": run["id"], "o": '{"code_task_run_id": "crun_p1_retry"}'})
        assert runner.pinned_pass_one_commit(c, run["id"]) is None


def test_a_stack_deploy_plans_the_pinned_commit_not_the_pull_request(monkeypatch):
    from boss_engine.services import stack_deploy

    seen = {}

    class Engine:
        def begin(self):
            class Tx:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

                def execute(self_inner, *a, **k):
                    class R:
                        def mappings(self):
                            return self

                        def first(self):
                            return {"spec": {"owner": "acme", "repo": "web"},
                                    "pull_request_number": 7, "status": "completed"}
                    return R()
            return Tx()

    def plan(engine, settings, **kw):
        seen.update(kw)
        return {"id": "sdep_1", "commit": kw["ref"], "repository": "acme/web"}

    monkeypatch.setattr(stack_deploy, "plan_stack_deploy", plan)
    target = {"id": "deptgt_1", "repo": "acme/web", "app_name": "Notes", "target_kind": "ssh"}
    stack_deploy.plan_for_workflow_step(Engine(), None, project_id="p", actor="a", target=target,
                                        inputs={"run_id": "crun_p1"}, workflow_run_id="wfr",
                                        commit=PASS_ONE)
    assert seen["ref"] == PASS_ONE
    stack_deploy.plan_for_workflow_step(Engine(), None, project_id="p", actor="a", target=target,
                                        inputs={"run_id": "crun_p1"}, workflow_run_id="wfr")
    assert seen["ref"] == "pull/7"
