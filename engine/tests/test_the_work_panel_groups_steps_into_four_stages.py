"""Work panel v2: the four stages group the workflow's OWN steps by their kind.

Every capability in the catalogue has a stage (or explicitly takes its neighbours'), so a new
capability cannot quietly land in the wrong stage. A person's decision joins the stage before
it, a production deploy is Live, routing steps are never drawn, and each step's record names
the screen it opens (its meeting, its build, its deploy).
"""
from __future__ import annotations

from boss_engine.services.project_experience import work_stages
from boss_engine.services.project_experience.work_stages import STAGE_OF_CAPABILITY, STAGES, step_list
from boss_engine.services.workflow_capabilities import CAPABILITY_KEYS


def test_every_known_capability_has_a_stage_or_takes_its_neighbours():
    missing = sorted(CAPABILITY_KEYS - set(STAGE_OF_CAPABILITY))
    assert not missing, f"capabilities with no stage: {missing}"
    stale = sorted(set(STAGE_OF_CAPABILITY) - CAPABILITY_KEYS)
    assert not stale, f"stages for capabilities that no longer exist: {stale}"
    for cap, stage in STAGE_OF_CAPABILITY.items():
        assert stage is None or stage in STAGES, (cap, stage)


def test_the_operator_named_kinds_land_where_the_wireframe_says():
    want = {"meeting": "plan", "spec_review": "plan", "research": "plan",
            "build": "build", "fix_pr": "build", "test": "build", "review": "build", "merge": "build",
            "preview": "ship", "deploy": "ship", "contract_test": "ship", "smoke_test": "ship",
            "config_deploy": "ship", "monitor": "live", "monitoring_pack": "live"}
    for cap, stage in want.items():
        assert work_stages.stage_of({"kind": "capability", "capability": cap}) == stage, cap


def test_a_production_deploy_is_live():
    assert work_stages.stage_of({"id": "deploy", "capability": "deploy",
                                 "inputs": {"environment": "production"}}) == "live"
    assert work_stages.stage_of({"id": "deploy_prod", "capability": "deploy"}) == "live"
    assert work_stages.stage_of({"id": "deploy", "capability": "deploy",
                                 "inputs": {"environment": "staging"}}) == "ship"


SPEC = {"steps": [
    {"id": "plan", "kind": "capability", "capability": "meeting"},
    {"id": "approve_prd", "kind": "decision"},
    {"id": "branch", "kind": "condition"},
    {"id": "build", "kind": "capability", "binding": {"capability": "build"}},
    {"id": "merge", "kind": "capability", "capability": "merge"},
    {"id": "tell", "kind": "capability", "capability": "notify"},
    {"id": "staging", "kind": "capability", "capability": "config_deploy"},
    {"id": "contract_tests", "kind": "capability", "capability": "contract_test"},
]}


def test_steps_carry_their_stage_and_decisions_join_the_step_before():
    steps = {
        "plan": {"status": "succeeded", "output": {"meeting_run_id": "mrun_1", "summary": "Panel agreed the PRD"}},
        "approve_prd": {"status": "succeeded", "output": {}},
        "build": {"status": "running", "output": {"await": {"id": "crun_9", "kind": "code_task"}}},
    }
    rows = step_list(SPEC, steps, project_id="p1")
    by = {r["id"]: r for r in rows}
    assert "branch" not in by, "routing steps are never drawn"
    assert [by[k]["stage"] for k in ("plan", "approve_prd", "build", "merge", "tell", "staging", "contract_tests")] == [
        "plan", "plan", "build", "build", "build", "ship", "ship"]
    assert by["plan"]["state"] == "done" and by["build"]["state"] == "now" and by["merge"]["state"] == "todo"
    assert by["plan"]["refs"]["meeting"] == "?project_id=p1&area=meetings&meeting=mrun_1"
    assert by["build"]["refs"]["build"] == "?project_id=p1&area=code_tasks&view=apps&build=crun_9"
    assert by["plan"]["sub"] == "Panel agreed the PRD"


def test_a_leading_step_with_no_stage_takes_the_next_one_and_an_unstarted_step_stays_todo():
    spec = {"steps": [{"id": "tell", "capability": "notify"}, {"id": "build", "capability": "build"},
                      {"id": "deploy", "capability": "deploy"}]}
    rows = step_list(spec, {"tell": {"status": "succeeded", "output": {}},
                            "build": {"status": "failed", "output": {}}}, project_id="p")
    assert [(r["id"], r["stage"], r["state"]) for r in rows] == [
        ("tell", "build", "done"), ("build", "build", "failed"), ("deploy", "ship", "todo")]


def test_a_merge_waiting_names_no_build_and_a_deploy_names_its_record():
    spec = {"steps": [{"id": "merge", "capability": "merge"}, {"id": "deploy", "capability": "deploy"}]}
    rows = step_list(spec, {
        "merge": {"status": "waiting", "output": {"await": {"id": "crun_1", "kind": "merge"}}},
        "deploy": {"status": "running", "output": {"await": {"id": "sdep_7", "kind": "stack_deployment"}}},
    }, project_id="p")
    assert rows[0]["refs"] == {}
    assert rows[1]["refs"] == {"deploy": "?project_id=p&area=infra&view=deploys&deploy=sdep_7"}
