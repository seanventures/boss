"""ONE RUN OWNS A PULL REQUEST, AND "DEPLOY AGAIN" WAITS FOR ITS MERGE.

The case, replayed (helpdesk-ai-v3, workflow wfr_3ceaad94, 2026-11-07). The build
crun_da4498ff opened PR #1 with review findings graded below the blocking threshold. Then:

  * 12:51:46 its finaliser started "Follow-up: deferred review findings — Route new tickets to a
    queue" (crun_c45a6960) on the build's branch;
  * 12:52:11 the workflow's More tests pass extended the same branch (crun_d2316785). The
    `title` the runner gave it ("More tests") was dropped by extend_code_task, so it was named
    "Route new tickets to a queue · retry 1" — a retry of a build that had succeeded;
  * 12:54 PR #1 merged; both runs pushed on and both claimed PR #2: two merge questions for one
    PR, "Merge PR #2" twice in Needs you;
  * 13:09 "Deploy again" planned PR #2's open head 99b569f — its deploy step skipped the merge
    wait every other deploy has — on a card titled "Deploy PR #1 to Staging".
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

from .test_every_build_gets_a_more_tests_pass import _by_id, _run
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures by name

FINDINGS = [{"severity": "minor", "text": "the fail-open test never forces an exception"},
            {"severity": "nit", "text": "queueMeta does not normalise its argument"}]


def _crun(c, project, *, status="completed", title="Route new tickets to a queue",
          root=None, pr=None, fctx=None):
    rid = f"crun_{uuid.uuid4().hex[:20]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, "
        "pull_request_number) VALUES (:i, :p, :s, 'done', :t, CAST(:f AS JSONB), :n)"),
        {"i": rid, "p": project, "s": status, "t": title, "n": pr,
         "f": json.dumps({**({"root_run_id": root} if root else {}), **(fctx or {})})})
    return rid


# ── 1. a pass is named for what it is; one run on a branch at a time ─────────────────────────

def test_more_tests_is_named_for_what_it_is_never_a_retry(eng, project):  # noqa: F811
    with eng.begin() as c:
        build = _crun(c, project, pr=1)
        retried = _crun(c, project, title="Route new tickets to a queue · retry 1", root=build)
    assert cts.extend_title(eng, build, "More tests") == (
        "Follow-up: More tests — Route new tickets to a queue")
    assert cts.extend_title(eng, retried, "More tests") == (
        "Follow-up: More tests — Route new tickets to a queue")
    assert cts.extend_title(eng, build, "") is None      # no word: retry_code_task names it
    from boss_engine.services.project_experience.work import clean_title

    assert clean_title(cts.extend_title(eng, build, "More tests")) == "Route new tickets to a queue"


def test_the_extend_tool_passes_its_title_on(monkeypatch):
    from boss_engine.tools import builtin

    src = open(builtin.__file__, encoding="utf-8").read()
    body = src[src.index("    def extend_code_task(ctx"):src.index('    @reg.tool(\n        "retry_code_task"')]
    assert "title=cts.extend_title(" in body, "extend_code_task drops the pass's title again"


def test_a_second_run_never_starts_on_a_branch_another_run_owns(eng, project):  # noqa: F811
    with eng.begin() as c:
        build = _crun(c, project, pr=1)
        followup = _crun(c, project, status="running", root=build, pr=2,
                         title="Follow-up: deferred review findings — Route new tickets to a queue")
    with pytest.raises(cts.BranchOwned, match=f"{followup}.*PR #2.*One run owns a pull request"):
        cts.retry_code_task(eng, None, None, build, fix_note="MORE TESTS PASS: add P2 tests")
    with eng.begin() as c:
        # The attempt being continued is not a second owner (a review-repair round's parent).
        assert cts.lineage_branch_owner(c, project, build, exclude=(followup,)) is None
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"),
                  {"i": followup})
        assert cts.lineage_branch_owner(c, project, build) is None


# ── 2. the findings go to the More tests pass that runs on the branch next ───────────────────

def test_deferred_findings_ride_with_the_pending_more_tests_pass(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    with eng.begin() as c:
        run = _run(c, project)
        build = _crun(c, project, pr=1, fctx={"workflow_run_id": run["id"]})
        assert runner.pass_two_pending(c, run["id"]) is True
    keep, why = g._findings_to_pass_two(eng, project, build, run["id"], FINDINGS)
    assert keep is False and "More tests" in why
    with eng.begin() as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :i"),
                         {"i": build}).scalar()
        assert fctx[runner.PASS_TWO_FINDINGS] == FINDINGS
        assert c.execute(text("SELECT count(*) FROM events WHERE entity_id = :i AND event_type = "
                              "'code_task.deferred_findings_to_pass_two'"), {"i": build}).scalar() == 1
    # The pass's brief carries them: one run on the branch fixes them.
    told = {}
    monkeypatch.setattr(runner, "_invoke_tool", lambda ctx, name, pid, actor, args: (
        told.update(args) or {"status": "extending", "result": {"run_id": "crun_p2"}}))
    monkeypatch.setattr(runner, "pass_one_head", lambda *a, **k: None)
    with eng.begin() as c:
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded', completed_at=now(), "
                       "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
                  {"r": run["id"], "o": json.dumps({"code_task_run_id": build})})
        runner._perform_capability(c, None, project_id=project, actor="test",
                                   step={"id": "more_tests"}, context={"run_id": run["id"]},
                                   key="more_tests", capability=CAPABILITIES["more_tests"],
                                   inputs={})
    assert told["run_id"] == build and told["title"] == "More tests"
    assert "DEFERRED REVIEW FINDINGS" in told["instructions"]
    assert "[MINOR] the fail-open test never forces an exception" in told["instructions"]


def test_once_more_tests_has_started_a_finding_goes_to_its_own_follow_up(eng, project):  # noqa: F811
    with eng.begin() as c:
        run = _run(c, project)
        c.execute(text("UPDATE workflow_step_runs SET status='running', started_at=now() "
                       "WHERE workflow_run_id=:r AND step_id='more_tests'"), {"r": run["id"]})
        assert runner.pass_two_pending(c, run["id"]) is False
        build = _crun(c, project, pr=1)
    assert g._findings_to_pass_two(eng, project, build, run["id"], FINDINGS) == (True, "")
    with eng.begin() as c:
        assert runner.pass_two_pending(c, "") is False
        assert runner.handed_findings_brief(c, build) == ""


# ── 3. Deploy again waits for its pull request's merge ───────────────────────────────────────

def test_deploy_again_waits_for_the_more_tests_pull_request_to_merge(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    monkeypatch.setattr(runner, "pass_two_redeploy_refusal", lambda *a, **k: None)
    asked = []

    def merge_wait(c, pid, build_run_id):
        asked.append(build_run_id)
        return {"capability": "deploy", "tool": None, "status": "awaiting",
                "await": {"kind": runner.MERGE_AWAIT_KIND, "id": build_run_id},
                "note": "Waiting for the merge: approve PR #2 to merge it."}

    monkeypatch.setattr(runner, "_merge_wait", merge_wait)
    with eng.begin() as c:
        run = _run(c, project)
        spec = _by_id(workflows._run_spec(c, run["id"]))
        out = runner._perform_capability(
            c, None, project_id=project, actor="test", step=spec["ship_again"],
            context={"run_id": run["id"]}, key="deploy", capability=CAPABILITIES["deploy"],
            inputs={"run_id": "crun_more_tests", "pass_2_of": "ship"})
    assert asked == ["crun_more_tests"]
    assert out["status"] == "awaiting" and out["await"]["kind"] == runner.MERGE_AWAIT_KIND


def test_deploy_again_of_an_unmerged_pull_request_asks_nobody_yet(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "pass_two_redeploy_refusal", lambda *a, **k: None)
    monkeypatch.setattr(runner, "deploy_step_inputs", lambda *a, **k: {"run_id": "crun_more_tests"})
    unmerged = {"v": True}
    monkeypatch.setattr(runner, "_unmerged_on_record", lambda c, rid: unmerged["v"])
    with eng.begin() as c:
        run = _run(c, project)
        context = runner._run_context(c, run["id"])
        spec = _by_id(workflows._run_spec(c, run["id"]))
        assert runner._gate_subject(c, project, spec["ship_again"], context) is None
        unmerged["v"] = False
        assert runner._gate_subject(c, project, spec["ship_again"], context) is not None
