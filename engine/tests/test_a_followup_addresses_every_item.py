"""BOSSAPP-1355: a follow-up is done when EVERY item of its brief is addressed.

A re-check's "Fix these" build carried 26 open items. The test writer covered the one
unit-testable item, the implementer made the frozen tests pass and said "No further changes
needed", and the other 25 (README examples, a compose mount, a Makefile target, a Dockerfile
flag) were never touched. The frozen tests are a floor, not the definition of done.

  1. the implementer gets the items as a numbered checklist and reports each; an item its report
     skips sends it back ONCE with the skipped items named;
  2. the test writer covers each testable item or marks it "not unit-testable"; the gate
     `brief_tests_written` judges against that;
  3. the follow-up brief drops findings whose source check did not run or was blind, and
     findings a later check superseded.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import brief_items
from boss_engine.services.acceptance import checks as acc_checks
from boss_engine.services.acceptance import ledger as al

from .test_acceptance_findings_ledger import _run, ledger_project  # noqa: F401
from .test_the_pipeline_runs_end_to_end import pipeline  # noqa: F401 - fixture used by name
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

BRIEF = (
    "\n\nFAILING CHECKS AND OPEN FINDINGS (from run crun_parent):\n"
    "- [MINOR] F-1 src/slug.js:1: slug must lowercase its input\n"
    "- [MINOR] F-2 README.md:40: the refund example uses a read token for a write route\n"
    "- [MINOR] F-3 Dockerfile:3: the build lacks CGO_ENABLED=0\n"
    "- [MINOR] F-4: Failing gate 'acceptance_met' (Must-haves met) when the PR opened\n"
    "\nFix ONLY these.")
# (MINOR: a MAJOR item is held to by the plan-coverage gate first, AR-24 — not this file's job.)


# ── the checklist ────────────────────────────────────────────────────────────────────────────

def test_the_brief_is_read_as_a_checklist_without_the_gate_lines():
    items = brief_items.parse("Why.\n" + BRIEF)
    assert [i.key for i in items] == ["F-1", "F-2", "F-3"], "a gate's own failure is no item"
    assert items[1].severity == "minor"
    assert brief_items.parse("Build a slug service.") == []
    note = brief_items.checklist_note(items)
    assert "1. F-1:" in note and "3. F-3:" in note and "ITEM <key>: FIXED <file:line>" in note


def test_a_fixed_claim_counts_only_when_a_changed_file_backs_it():
    items = brief_items.parse(BRIEF)
    said = ("ITEM F-1: FIXED src/slug.js:1\nITEM F-2: FIXED README.md:41 — write token\n"
            "ITEM F-3: NOT-FIXED the service is pure node, there is no Go build")
    a = brief_items.assess(items, said, ["plain/src/slug.js", "README.md"])
    assert [i.key for i in a.fixed] == ["F-1", "F-2"] and a.send_back == []
    assert [(i.key, r[:8]) for i, r in a.not_fixed] == [("F-3", "the serv")]
    assert [i.key for i in a.not_done] == ["F-3"], "a NOT-FIXED goes to review, not back"
    # README.md was never changed: the claim is not backed, so it goes back and to review.
    b = brief_items.assess(items, said, ["plain/src/slug.js"])
    assert [i.key for i in b.unbacked] == ["F-2"] and [i.key for i in b.send_back] == ["F-2"]
    assert [i.key for i in b.not_done] == ["F-2", "F-3"]
    # "No further changes needed" answers nothing; bare FIXED / reasonless NOT-FIXED neither.
    assert len(brief_items.assess(items, "No further changes needed.", []).missing) == 3
    bare = "ITEM F-1: FIXED\nITEM F-2: NOT-FIXED\nITEM F-3: FIXED Dockerfile:3"
    c = brief_items.assess(items, bare, ["Dockerfile"])
    assert [i.key for i in c.missing] == ["F-1", "F-2"] and [i.key for i in c.fixed] == ["F-3"]
    assert len(brief_items.assess(items, "ITEM F-10: FIXED a.js:1", ["a.js"]).missing) == 3


def test_the_judge_block_and_the_pr_lines_name_what_was_not_fixed():
    items = brief_items.parse(BRIEF)
    a = brief_items.assess(items, "ITEM F-3: NOT-FIXED no Go build here", [])
    block = brief_items.judge_block(a.not_done, [items[1]])
    assert "DID NOT FIX" in block and "F-1" in block and "NO UNIT TEST" in block
    rec = {"items": 3, **a.record(), "excused": ["F-2"], "reworked": True}
    lines = "\n".join(brief_items.pr_lines(rec))
    assert "F-3 (not fixed: no Go build here)" in lines and "no report" in lines
    assert brief_items.pr_lines({}) == []


def test_a_harness_summary_keeps_its_tail_where_the_report_is():
    long = "x" * 20000 + "\nITEM F-2: FIXED README.md:1"
    clipped = brief_items.clip_summary(long)
    assert len(clipped) <= 8000 and clipped.endswith("README.md:1")
    assert brief_items.clip_summary("short") == "short"


def test_a_bare_number_key_needs_the_item_prefix():
    items = [brief_items.Item(key="1", text="the cache never expires")]
    assert len(brief_items.assess(items, "1. fixed the cache in cache.go:4", ["cache.go"]).missing) == 1
    assert brief_items.assess(items, "ITEM 1: FIXED cache.go:4", ["cache.go"]).fixed


def test_the_test_writer_covers_each_item_or_excuses_it():
    items = brief_items.parse(BRIEF)       # F-1 is a source file, F-2 and F-3 docs/config
    said = ("COVERAGE F-1: TestSlugLowercases\n"
            "COVERAGE F-2: not unit-testable — verified by acceptance/review")
    have = {"TestSlugLowercases"}
    assert [i.key for i in brief_items.uncovered(items, said, have)] == ["F-3"]
    assert [i.key for i in brief_items.excused(items, said, have)] == ["F-2"]
    # A test the suite does not define covers nothing; a word of the line is not a test name.
    assert [i.key for i in brief_items.uncovered(items, said, set())] == ["F-1", "F-3"]
    assert [i.key for i in brief_items.uncovered(
        items, "COVERAGE F-1: see TestSlugLowercases for details", have)][0] == "F-1"
    # A source-code finding cannot be excused: it needs a test.
    code_excuse = said.replace("COVERAGE F-1: TestSlugLowercases",
                               "COVERAGE F-1: not unit-testable — too hard")
    assert "F-1" in [i.key for i in brief_items.uncovered(items, code_excuse, have)]
    # An empty report covers nothing: the gate fails closed.
    assert [i.key for i in brief_items.uncovered(items, "", have)] == ["F-1", "F-2", "F-3"]


# ── the follow-up brief ──────────────────────────────────────────────────────────────────────

def test_a_blind_or_superseded_finding_is_not_in_the_brief(eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        blind = acc_checks.record_check(
            c, project_id=p, lineage_root_run_id=root, code_task_run_id=root, layer="build_gate",
            mode="static", record={"verdict": "not_run", "items": [], "why": "could not read"})

        def row(fp, title, body="", **kw):
            r, _ = al.upsert(c, project_id=p, lineage=root, kind=kw.pop("kind", "review"),
                             severity="major", fingerprint=fp, title=title, body=body,
                             stage="followup", run_id=root, **kw)
            return r

        real = row("rv:readme", "README refund example uses the wrong token", path="README.md",
                   line=40)
        gate = row("gate:acceptance_met", "Failing gate 'acceptance_met'",
                   "Failing gate 'acceptance_met' (Must-haves met): Must-haves: 0 met, "
                   "0 partial, 30 missing", kind="pipeline")
        unjudged = row("ac:AC-9", "AC-9 not judged: metrics", kind="acceptance_gap",
                       check_id=blind["id"])
        plan = al.followup_spec(c, project_id=p, lineage=root)
        assert {f["key"] for f in plan["findings"]} == {f"F-{real['number']}"}, plan["findings"]
        assert gate["id"] not in plan["ledger_ids"] and unjudged["id"] not in plan["ledger_ids"]
        # Dropped ONCE, to a terminal state with the reason: counts stop including them.
        for rid, why in ((gate["id"], "blind"), (unjudged["id"], "did not run")):
            got = al.get(c, rid)
            assert got["status"] == "superseded" and why in got["waive_reason"], got
        assert [r["id"] for r in al.open_for(c, project_id=p, lineage=root)] == [real["id"]]


def test_a_judged_check_that_found_everything_missing_is_not_dropped_by_its_counts(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        judged = acc_checks.record_check(
            c, project_id=p, lineage_root_run_id=root, code_task_run_id=root, layer="build_gate",
            mode="static", record={"verdict": "not_met", "items": []})
        gate, _ = al.upsert(
            c, project_id=p, lineage=root, kind="pipeline", severity="major",
            fingerprint="gate:acceptance_met", title="Failing gate 'acceptance_met'",
            body="Must-haves: 0 met, 0 partial, 30 missing", stage="followup", run_id=root)
        assert al.stale_rows(c, [gate], project_id=p, lineage=root) == {}, "a judgement stands"
    with eng.begin() as c:      # a LATER transaction: "later" is by time
        # A LATER judged check that recorded its own `ac:` rows supersedes the gate line; one
        # that recorded none does not.
        gate = al.get(c, gate["id"])
        later = acc_checks.record_check(
            c, project_id=p, lineage_root_run_id=root, layer="build_gate", mode="static",
            record={"verdict": "not_met", "items": []})
        assert al.stale_rows(c, [gate], project_id=p, lineage=root) == {}
        al.upsert(c, project_id=p, lineage=root, kind="acceptance_gap", severity="major",
                  fingerprint="ac:AC-1", title="AC-1 missing", stage="acceptance_check",
                  run_id=root, check_id=later["id"])
        assert "superseded" in al.stale_rows(c, [gate], project_id=p, lineage=root)[gate["id"]]
        assert judged["id"]


# ── the pipeline, with the scripted engine ───────────────────────────────────────────────────

class _NoModel:
    """The decision seat of these builds (plan coverage, risk tier): no model answers, so the
    deterministic verdicts stand. Without it the harness reaches the real Claude CLI."""

    def decide(self, *_a, **_k):
        raise RuntimeError("no model in this test")


@pytest.fixture(autouse=True)
def no_decision_model(monkeypatch):
    from boss_engine.services import typed_decisions

    monkeypatch.setattr(typed_decisions, "make_decider", lambda *_a, **_k: _NoModel())


def _events(pipeline, run_id, kind):  # noqa: F811
    with pipeline.engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT data FROM events WHERE entity_id = :i AND event_type = :k "
            "ORDER BY created_at, id"), {"i": run_id, "k": kind}).mappings()]


def _judged(pipeline, run_id):  # noqa: F811
    events = _events(pipeline, run_id, "code_task.brief_items_judged")
    assert len(events) == 1
    return events[0]["data"]


def test_an_implementer_that_fixes_only_the_tested_item_is_sent_back_with_the_rest_named(
        pipeline):  # noqa: F811
    run_id, repo = pipeline.build(faults="skip_items", tag="items-skip", brief_extra=BRIEF)
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", (out["run"]["status"], out["run"].get("error"))
    sent = _events(pipeline, run_id, "code_task.brief_items_sent_back")
    assert len(sent) == 1, "sent back once"
    assert sent[0]["data"]["unaddressed"] == ["F-2", "F-3"]
    implementers = [w for w in out["writes"] if w["stage"] in ("implement", "solo", "repair")]
    assert len(implementers) >= 2
    assert "F-2" in implementers[-1]["full_prompt"] and "F-3" in implementers[-1]["full_prompt"]
    rec = _judged(pipeline, run_id)
    assert rec["reworked"] and not rec["reverted"] and rec["missing"] == []
    assert sorted(rec["fixed"]) == ["F-1", "F-2", "F-3"], "the rework's files back every claim"
    state = out["run"]["state"]
    assert (state.get("evidence") or state)["brief_items"]["sent_back"] is True


def test_a_rework_that_breaks_the_tests_is_reverted_and_the_items_are_recorded(
        pipeline):  # noqa: F811
    run_id, repo = pipeline.build(faults="skip_items,break_on_rework", tag="items-revert",
                                  brief_extra=BRIEF, seed={"NOTES.md": "base notes\n"})
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", (out["run"]["status"], out["run"].get("error"))
    rec = _judged(pipeline, run_id)
    assert rec["sent_back"] and rec["reverted"] and not rec["reworked"]
    assert rec["missing"] == ["F-2", "F-3"], "recorded for review, never vanished"
    # No repair slot was spent on the send-back.
    assert not _events(pipeline, run_id, "code_task.step_escalating")
    assert out["writes"], "the passing tree was kept"
    # The reverted rework had edited README.md and deleted NOTES.md, base files the builder never
    # touched: the branch that reached the remote holds both as they were, and the builder's work.
    import subprocess

    heads = pipeline.branch_heads(repo)
    work = next(b for b in heads if b != "main")
    git = ["git", "--git-dir", str(pipeline.repos[repo])]

    def show(path):
        done = subprocess.run(git + ["show", f"{work}:{path}"], capture_output=True, text=True)
        return done.stdout if done.returncode == 0 else None

    assert show("README.md") == f"# {repo}\n"
    assert show("NOTES.md") == "base notes\n"
    assert any("slug.js" in f for f in subprocess.run(
        git + ["ls-tree", "-r", "--name-only", work], capture_output=True, text=True
    ).stdout.splitlines()), "the builder's own work is intact"


def test_an_implementer_that_addresses_every_item_is_not_sent_back(pipeline):  # noqa: F811
    run_id, repo = pipeline.build(faults="", tag="items-all", brief_extra=BRIEF)
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", (out["run"]["status"], out["run"].get("error"))
    assert not _events(pipeline, run_id, "code_task.brief_items_sent_back")
    assert not _events(pipeline, run_id, "code_task.brief_items_uncovered")
    rec = _judged(pipeline, run_id)
    assert sorted(rec["fixed"]) == ["F-1", "F-2", "F-3"] and rec["excused"] == ["F-2", "F-3"]
    first = [w for w in out["writes"] if w["stage"] == "write_tests"][0]
    assert "COVERAGE <key>:" in first["full_prompt"], "the test writer was handed the items"
