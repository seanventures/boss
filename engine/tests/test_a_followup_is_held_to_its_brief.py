"""The FXQuote banking sample (KAN-11, Go) surfaced three pipeline bugs (BOSSAPP-1330/1331/1332).

1330. The checks-failing follow-up crun_5daadf83 planned five named tests
(`TestP1_QuoteIDsAreNotPredictable`, …) and added none: its test author was sent an EMPTY gap —
a follow-up has no ITERATION FEEDBACK, its findings are its brief — and answered "Tests already
pass and no gap was specified. Nothing to change." The implementer may not touch the suite and
reverted the tests it wrote; every gate passed, and acceptance fell from the parent's 17 met to
13 while the merge card showed only the 13.

1331. Its workflow's More tests pass could not start (BranchOwned: the follow-up owned the
branch) and was recorded "succeeded".

1332. A UX sign-off card asked a person to "use the preview and sign off on how it looks" for an
API-only service: the build's UI quality pass wrote web/index.html into a Go service with no UI,
and the UX step asked whatever the app was.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import acceptance_gate as ag
from boss_engine.services import brief_tests
from boss_engine.services import factory_view
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows
from boss_engine.services.code_task_service import BranchOwned, refuse_second_owner

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

FIX = Path(__file__).parent / "fixtures" / "fxquote_1330"
BRIEF = json.loads((FIX / "followup_brief.json").read_text(encoding="utf-8"))
#: The five tests the follow-up's own plan named.
PLANNED = ["TestP1_QuoteIDsAreNotPredictable", "TestP1_ReadyzFailsOnBadConfig_InvalidSpread",
           "TestP1_QuoteStoreEvictsExpiredEntries", "TestP1_SingleRuntimeDependency",
           "TestP1_QuoteIDFailureIsRejectedAndMetered"]


# ── 1330: the test author is sent the brief, and the build is held to the tests it names ─────

def test_the_brief_names_the_five_tests_and_the_existing_ones_count_as_there():
    names = brief_tests.named(BRIEF["instructions"], BRIEF["spec"], BRIEF["plan"])
    assert set(PLANNED) <= set(names), names


def test_a_follow_ups_test_author_is_sent_its_brief_not_an_empty_gap():
    names = brief_tests.named(BRIEF["instructions"], BRIEF["spec"], BRIEF["plan"])
    gap = g.repair_tests_gap(None, BRIEF["instructions"], resumed=False, named_tests=names)
    assert "F-16" in gap and "F-17" in gap, "the findings are the gap"
    for name in PLANNED:
        assert name in gap
    # Iteration feedback still wins when there is some; a resumed run with none has no gap.
    fed = g.repair_tests_gap([{"fix_note": "cover app.js"}], BRIEF["instructions"],
                             resumed=False, named_tests=[])
    assert fed.startswith("cover app.js") and "F-16" not in fed
    assert g.repair_tests_gap(None, BRIEF["instructions"], resumed=True, named_tests=names) == ""


def test_the_named_tests_must_exist_in_the_suite(tmp_path):
    names = brief_tests.named(BRIEF["instructions"], BRIEF["spec"], BRIEF["plan"])
    suite = (FIX / "fxquote_test.go.txt").read_text(encoding="utf-8")
    (tmp_path / "fxquote_test.go").write_text(suite)
    # The shipped branch: "file untouched" — every planned test is missing.
    assert set(brief_tests.missing(names, tmp_path, ["fxquote_test.go"])) == set(PLANNED)
    added = suite + "".join(f"\nfunc {n}(t *testing.T) {{}}\n" for n in PLANNED)
    (tmp_path / "fxquote_test.go").write_text(added)
    assert brief_tests.missing(names, tmp_path, ["fxquote_test.go"]) == []
    # A suite whose output lists the tests it ran must have run each one.
    listed = "\n".join(f"--- PASS: {n} (0.00s)" for n in names if n != PLANNED[0])
    assert brief_tests.missing(names, tmp_path, ["fxquote_test.go"], listed) == [PLANNED[0]]


def test_the_gate_is_a_test_gate_with_words():
    assert "brief_tests_written" in g._TEST_GATES, "a failure re-runs write_tests"
    assert factory_view.GATE_LABELS["brief_tests_written"][0] == "Named tests written"


def _check(n_met: int, n_partial: int, keys: list[str]) -> dict:
    items = [{"n": i + 1, "text": f"AC-{i + 1}", "status": "met" if i < n_met else "partial",
              "evidence": "fxquote.go"} for i in range(n_met + n_partial)]
    return {"verdict": "not_met", "items": items, "source": {"root": True, "item_keys": keys},
            "counts": {"met": n_met, "partial": n_partial, "missing": 0, "unjudged": 0}}


def test_a_follow_up_worse_than_its_parent_says_so_and_keeps_both_verdicts():
    keys = [f"k{i}" for i in range(19)]
    parent, mine = _check(17, 2, keys), _check(13, 6, keys)
    reg = ag.regressed_vs_parent([("crun_parent", parent), ("crun_me", mine)],
                                 run_id="crun_me", parent_run_id="crun_parent")
    assert reg["words"] == "regressed vs parent: 17→13 met"
    shown = {**mine, "regressed_vs_parent": reg}
    assert ag.headline(shown).startswith("Must-haves: regressed vs parent: 17→13 met")
    card = ag.regression_card(reg)
    assert card["parent_counts"]["met"] == 17 and card["parent_table"], \
        "the parent's verdict and table ride on the card"
    # Better or equal, or judged on other items: no regression.
    assert ag.regressed_vs_parent([("crun_parent", mine), ("crun_me", parent)],
                                  run_id="crun_me", parent_run_id="crun_parent") is None
    assert ag.regressed_vs_parent([("crun_parent", parent), ("crun_me", _check(13, 6, ["x"]))],
                                  run_id="crun_me", parent_run_id="crun_parent") is None


def test_the_merge_card_of_a_regressed_follow_up_shows_both(eng, project):  # noqa: F811
    from .test_acceptance_gate_holds_the_build_to_its_must_haves import PR, _cleanup, _run

    keys = [f"k{i}" for i in range(19)]
    with eng.begin() as c:
        root = _run(c, project, pr=PR, fctx={})
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
        parent = _run(c, project, pr=PR, fctx={"root_run_id": root},
                      state={"acceptance": _check(17, 2, keys)})
        follow = _run(c, project, status="running", pr=PR,
                      fctx={"root_run_id": root, "is_followup": True, "parent_run_id": parent})
    try:
        with eng.begin() as c:
            _card, acc = ag.merge_card_checks(
                c, run_id=follow, evidence={ag.KEY: _check(13, 6, keys)},
                checks_failing=None, pr_url=PR)
        assert acc["counts"]["met"] == 13, "the latest check leads"
        assert acc["regressed_vs_parent"]["from_met"] == 17
        assert "regressed vs parent: 17→13 met" in ag.headline(acc)
    finally:
        _cleanup(eng, project)


# ── 1331: a step that could not start waits for the branch's owner, and is never "succeeded" ─

def test_branch_owned_says_whom_it_waits_for():
    with pytest.raises(BranchOwned) as caught:
        refuse_second_owner({"id": "crun_5daadf836f8543599260", "title": "Follow-up"})
    assert caught.value.tool_result == {"error_kind": "branch_owned",
                                        "wait_for_run": "crun_5daadf836f8543599260"}


def test_a_more_tests_pass_on_an_owned_branch_waits_for_the_owner(monkeypatch):
    monkeypatch.setattr(runner, "_invoke_tool", lambda *a, **k: {
        "ok": False, "error": "BranchOwned: crun_x is already working on this branch",
        "error_kind": "branch_owned", "wait_for_run": "crun_x"})
    monkeypatch.setattr(runner, "_step_authority",
                        lambda *a, **k: __import__("contextlib").nullcontext())

    class _Conn:
        def execute(self, *a, **k):
            class _R:
                def mappings(self):
                    return self

                def first(self):
                    return None
            return _R()

    with pytest.raises(runner.CapabilityWaitError) as caught:
        runner._start_on_the_branch(object(), _Conn(), project_id="p", actor="a", context={},
                                    step={}, capability_key="more_tests", base_run="crun_b",
                                    args={})
    assert caught.value.kind == "branch_owned" and "crun_x" in str(caught.value)


@pytest.mark.parametrize("output,not_run", [
    ({"status": "not_applicable", "advisory_failed": True, "note": "could not start"}, True),
    ({"status": "skipped", "skipped": "service", "reason": "a service"}, True),
    # A pass that ran and failed did run.
    ({"status": "succeeded", "advisory_failed": True, "awaited_status": "failed"}, False),
    ({"status": "succeeded"}, False),
])
def test_a_step_that_did_not_run_is_not_run(output, not_run):
    assert bool(workflows.did_not_run(output)) is not_run


# ── 1332: UI-only steps skip, on the record, for a service ────────────────────────────────────

def _build(eng, project, kind):  # noqa: F811
    crun = f"crun_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx) VALUES (:i, :p, 'completed', 'done', 'FXQuote', '{}'::jsonb, "
            "CAST(:st AS JSONB), '{}'::jsonb)"),
            {"i": crun, "p": project, "st": json.dumps(
                {"app_kind": {"kind": kind, "why": "go.mod and no page"}} if kind else {})})
    return crun


@pytest.mark.parametrize("kind,skipped", [("service", True), ("app", False), (None, False)])
def test_ux_sign_off_skips_for_a_service_and_records_it(eng, project, kind, skipped):  # noqa: F811
    crun = _build(eng, project, kind)
    try:
        with eng.begin() as c:
            _, version = _publish(c, project, f"ux_{uuid.uuid4().hex[:6]}", [
                {"id": "ux", "kind": "capability",
                 "binding": {"capability": "ux_review", "inputs": {}}, "next": ["after"]},
                {"id": "after", "kind": "capability",
                 "binding": {"capability": "notify", "inputs": {"message": "on"}}, "next": []},
            ], "ux")
            run = _start(c, project, version["id"], {"adopt_code_task_run_id": crun})
            runner.tick(c, Ctx(eng), project_id=project)
            steps = _steps(c, run["id"])
            cards = c.execute(text(
                "SELECT count(*) FROM decisions WHERE project_id = :p "
                "AND recommendation->>'workflow_run_id' = :r"),
                {"p": project, "r": run["id"]}).scalar()
            said = c.execute(text(
                "SELECT count(*) FROM events WHERE entity_id = :r "
                "AND event_type = 'workflow.step_skipped'"), {"r": run["id"]}).scalar()
        if skipped:
            assert steps["ux"]["status"] == "skipped", steps["ux"]
            assert "service" in steps["ux"]["output"]["reason"]
            assert cards == 0 and said >= 1
            assert steps["after"]["status"] != "pending", "the run went on"
        else:
            assert steps["ux"]["status"] == "waiting" and cards == 1, steps["ux"]
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": crun})


def test_the_ui_quality_pass_reads_the_apps_kind(tmp_path):
    (tmp_path / "go.mod").write_text("module fxquote\n\ngo 1.23\n")
    (tmp_path / "fxquote.go").write_text("package fxquote\n")
    assert g._app_kind_of(tmp_path, ".")["kind"] == "service"
    src = Path(g.__file__).read_text(encoding="utf-8")
    assert '_ui_pass_applies = _kind_now.get("kind") != "service"' in src
    assert "if _ui_pass_applies and should_run_quality_pass(" in src
