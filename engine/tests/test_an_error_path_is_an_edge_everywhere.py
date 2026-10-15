"""An error path is an edge wherever BOSS walks a workflow's graph (#512 follow-up).

`workflows._targets` counts a step's `on_error` as an edge: an error-only step is reachable, an
error path back into the graph is a cycle. The two other walks over the same graph read the same
edges from there, so they cannot drift:

* two_pass — a step an error path also leads to is where the workflow meets again, so it is
  never pulled into pass 2's parallel branch; and a step after the join that only an error path
  reaches still builds on pass 2's branch. Pass 2 is still what follows a build that SUCCEEDED,
  so the straight walks (what runs alongside, what ships) follow success edges.
* workflow_precheck — reachability (a parallel branch's steps, the deploys a verify step
  protects, the order steps are checked in) includes the error paths.
"""
from __future__ import annotations

from boss_engine.services import two_pass, workflows
from boss_engine.services import workflow_precheck as wp


def _cap(cap, sid, nxt=(), **extra):
    return {"id": sid, "kind": "capability", "binding": {"capability": cap, "inputs": {}},
            "next": list(nxt), **extra}


def _by_id(spec):
    return {s["id"]: s for s in spec["steps"]}


BUILT = {"from_step": "build", "field": "code_task_run_id"}


# ── one set of edges ──────────────────────────────────────────────────────────────────────────

def test_two_pass_and_the_precheck_read_the_runtimes_edges():
    step = _cap("build", "build", ["deploy"], on_error="notify")
    assert two_pass._targets(step) == workflows._targets(step) == ["deploy", "notify"]
    assert wp._targets(step) == workflows._targets(step)
    assert two_pass._success_targets(step) == workflows._success_targets(step) == ["deploy"]


# ── two_pass ──────────────────────────────────────────────────────────────────────────────────

def test_a_step_an_error_path_also_reaches_is_not_pulled_into_pass_2():
    # check fails into `report`; deploy also ends at `report`. `report` is where the workflow
    # meets again: if it ran inside pass 2's branch, a failed check would enter that branch
    # midway and its join would wait for a More tests that never started.
    spec = {"entry_step": "check", "steps": [
        _cap("review", "check", ["build"], on_error="report"),
        _cap("build", "build", ["deploy"]),
        _cap("deploy", "deploy", ["report"]),
        _cap("report", "report"),
    ]}
    out = _by_id(two_pass.with_more_tests(spec))
    fork = next(s for s in out.values() if s.get("kind") == "parallel")
    join = out[fork["join"]]
    assert out["deploy"]["next"] == [join["id"]]  # pass 1 ships alongside pass 2, as before
    assert out["report"]["next"] == []
    again = out["deploy_again"]
    assert again["next"] == ["report"]  # report waits for both, after the join


def test_a_step_after_pass_2_that_only_an_error_path_reaches_reads_pass_2():
    # After the join: PLRR writes the branch, and when it fails its error path rebuilds from
    # the build's run. That run must be pass 2's, the newest branch, like any step after the join.
    spec = {"entry_step": "build", "steps": [
        _cap("build", "build", ["deploy"]),
        _cap("deploy", "deploy", ["plrr"]),
        _cap("plrr", "plrr", [], on_error="fixup",
             binding={"capability": "plrr", "inputs": {"run_id": BUILT}}),
        _cap("smoke_test", "fixup", [], binding={"capability": "smoke_test",
                                                 "inputs": {"run_id": BUILT}}),
    ]}
    out = _by_id(two_pass.with_more_tests(spec))
    more = next(i for i, s in out.items() if two_pass._capability(s) == "more_tests")
    assert out["plrr"]["binding"]["inputs"]["run_id"]["from_step"] == more
    assert out["fixup"]["binding"]["inputs"]["run_id"]["from_step"] == more


def test_a_build_with_an_error_path_still_ships_pass_1_without_waiting():
    spec = {"entry_step": "build", "steps": [
        _cap("build", "build", ["deploy"], on_error="notify"),
        _cap("deploy", "deploy"),
        _cap("notify", "notify"),
    ]}
    out = _by_id(two_pass.with_more_tests(spec))
    fork = next(s for s in out.values() if s.get("kind") == "parallel")
    assert "deploy" in fork["branches"]
    assert out["build"]["on_error"] == "notify"


def test_a_more_tests_only_an_error_path_reaches_is_not_the_builds_pass_2():
    spec = {"entry_step": "build", "steps": [
        _cap("build", "build", ["deploy"], on_error="more_tests"),
        _cap("deploy", "deploy"),
        _cap("more_tests", "more_tests"),
    ]}
    assert two_pass.builds_without_more_tests(spec) == ["build"]


# ── workflow_precheck ─────────────────────────────────────────────────────────────────────────

def test_a_parallel_branch_includes_its_error_path():
    spec = {"entry_step": "fork", "steps": [
        {"id": "fork", "kind": "parallel", "branches": ["a", "b"], "join": "done", "next": []},
        _cap("test", "a", ["done"], on_error="a_failed"),
        _cap("notify", "a_failed", ["done"]),
        _cap("review", "b", ["done"]),
        {"id": "done", "kind": "join", "wait_for": ["a", "b"], "next": []},
    ]}
    (group,) = wp._parallel_groups(spec)
    assert group["branches"]["a"] == ["a", "a_failed"]


def test_a_verify_step_a_deploy_follows_on_an_error_path_cannot_be_skipped():
    spec = {"entry_step": "tests", "steps": [
        _cap("test", "tests", ["build2"]),
        _cap("build", "build2", [], on_error="hotfix"),
        _cap("deploy", "hotfix"),
    ]}
    refusal = wp.skip_refusal(spec, _by_id(spec)["tests"])
    assert refusal and "hotfix" in refusal


def test_steps_are_checked_in_the_order_a_run_reaches_them_error_paths_included():
    spec = {"entry_step": "a", "steps": [
        _cap("review", "a", ["b"], on_error="c"),
        _cap("notify", "z"),
        _cap("notify", "c"),
        _cap("notify", "b"),
    ]}
    assert [s["id"] for s in wp._ordered(spec)] == ["a", "b", "c", "z"]


def test_a_deploy_with_an_error_path_is_not_put_in_the_branch_the_join_waits_for():
    # A join waits for its branch's last step to succeed; a deploy that fails into its error
    # path never would, so the join would never release. It runs after the join instead.
    spec = {"entry_step": "build", "steps": [
        _cap("build", "build", ["preview"]),
        _cap("preview", "preview", ["deploy"]),
        _cap("deploy", "deploy", [], on_error="rollback"),
        _cap("notify", "rollback"),
    ]}
    out = _by_id(two_pass.with_more_tests(spec))
    assert not any(s.get("kind") == "parallel" for s in out.values())
    more = next(i for i, s in out.items() if two_pass._capability(s) == "more_tests")
    assert out[more]["next"] == ["deploy"]
    assert out["deploy"]["on_error"] == "rollback"
