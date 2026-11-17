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


def test_the_implementers_report_must_answer_every_item():
    items = brief_items.parse(BRIEF)
    only_tested = "Changed src/slug.js.\nITEM F-1: FIXED src/slug.js:1"
    assert [i.key for i in brief_items.unaddressed(items, only_tested)] == ["F-2", "F-3"]
    all_three = ("ITEM F-1: FIXED src/slug.js:1\nITEM F-2: FIXED README.md:41 — write token\n"
                 "ITEM F-3: NOT-FIXED the service is pure node, there is no Go build")
    assert brief_items.unaddressed(items, all_three) == []
    # "No further changes needed" answers nothing; a FIXED with no place or a NOT-FIXED with no
    # reason answers nothing either.
    assert len(brief_items.unaddressed(items, "No further changes needed.")) == 3
    bare = "ITEM F-1: FIXED\nITEM F-2: NOT-FIXED\nITEM F-3: FIXED Dockerfile:3"
    assert [i.key for i in brief_items.unaddressed(items, bare)] == ["F-1", "F-2"]
    # F-1 is not F-10.
    assert len(brief_items.unaddressed(items, "ITEM F-10: FIXED a.js:1")) == 3


def test_the_test_writer_covers_each_item_or_excuses_it():
    items = brief_items.parse(BRIEF)
    said = ("COVERAGE F-1: TestSlugLowercases\n"
            "COVERAGE F-2: not unit-testable — verified by acceptance/review")
    assert [i.key for i in brief_items.uncovered(items, said, {"TestSlugLowercases"})] == ["F-3"]
    # A test the suite does not define covers nothing.
    assert [i.key for i in brief_items.uncovered(items, said, set())] == ["F-1", "F-3"]
    full = said + "\nCOVERAGE F-3: not unit-testable — verified by acceptance/review"
    assert brief_items.uncovered(items, full, {"TestSlugLowercases"}) == []


# ── the follow-up brief ──────────────────────────────────────────────────────────────────────

def test_a_blind_or_superseded_finding_is_not_in_the_brief(eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        blind = acc_checks.record_check(
            c, project_id=p, lineage_root_run_id=root, layer="build_gate", mode="static",
            record={"verdict": "not_run", "items": [], "why": "the reviewer could not read it"})

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
        keys = {f["key"] for f in plan["findings"]}
        assert keys == {f"F-{real['number']}"}, plan["findings"]
        assert gate["id"] not in plan["ledger_ids"] and unjudged["id"] not in plan["ledger_ids"]
        why = al.stale_rows(c, [gate, unjudged, real], project_id=p, lineage=root)
        assert "blind" in why[gate["id"]] and "did not run" in why[unjudged["id"]]
        assert real["id"] not in why
        # The same gate line, not blind-looking, once a later check JUDGED: superseded.
        gate2 = dict(gate, body="Must-haves: 12 met, 3 partial, 5 missing")
        assert al.stale_rows(c, [gate2], project_id=p, lineage=root) == {}
        acc_checks.record_check(c, project_id=p, lineage_root_run_id=root, layer="build_gate",
                                mode="static", record={"verdict": "not_met", "items": []})
        assert "superseded" in al.stale_rows(c, [gate2], project_id=p, lineage=root)[gate["id"]]


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
    assert "F-2" in implementers[1]["full_prompt"] and "F-3" in implementers[1]["full_prompt"]


def test_an_implementer_that_addresses_every_item_is_not_sent_back(pipeline):  # noqa: F811
    run_id, repo = pipeline.build(faults="", tag="items-all", brief_extra=BRIEF)
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    assert out["run"]["status"] == "completed", (out["run"]["status"], out["run"].get("error"))
    assert not _events(pipeline, run_id, "code_task.brief_items_sent_back")
    assert not _events(pipeline, run_id, "code_task.brief_items_uncovered")
    first = [w for w in out["writes"] if w["stage"] == "write_tests"][0]
    assert "COVERAGE <key>:" in first["full_prompt"], "the test writer was handed the items"


