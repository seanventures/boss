"""BOSSAPP-1360: what a blind review seat (not_run / hollow / tools_blocked) said, and any
finding that only describes BOSS's own tooling failing, is never ledgered, never carried by a
follow-up brief and never sent to an app builder; the planner may declare an item NOT APPLICABLE
with a reason, and that is an item to verify, not a reason to loop the plan back to a person.

THE CASE: crun_9f190ed1ede845c09cdc's seat could not create a namespace; its F-42 "every command
failed before execution ... bwrap: ..." stayed in the brief of the next run, which looped the
planner 3x and escalated to a person."""
from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.services import plan_coverage as pc
from boss_engine.services import review_tools
from boss_engine.services.acceptance import ledger as al
from boss_engine.services.review_comments import ReviewComment

from .test_acceptance_findings_ledger import _run, ledger_project  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

F42 = ("The required re-check is incomplete. Every command failed before execution because the "
       "BOSS command sandbox could not create a namespace (`bwrap: No permissions to create a "
       "new namespace`).")


def test_a_tooling_signature_is_only_a_signature():
    assert review_tools.is_tooling_failure(F42)
    # Anchored to a file: a real review of code that uses bwrap.
    assert not review_tools.is_tooling_failure(F42, "internal/sandbox/run.go")
    assert not review_tools.is_tooling_failure("The handler leaks the token in the log line.")


def test_findings_of_a_blind_seat_are_dropped_and_a_seat_that_ran_is_kept():
    real = {"stage": "review_code", "body": "router.go does not clean dot segments",
            "path": "internal/proxy/router.go"}
    ran = {"stage": "review_code", "body": F42}          # a seat that RAN: its prose is its own
    comments = [real, {"stage": "security_check", "body": "from a seat that did not run"}, ran]
    kept = review_tools.drop_blind(comments, {"security_check": "NOT RUN: tools"})
    assert kept == [real, ran]


def test_a_blind_seats_findings_are_not_ledgered_and_it_re_reviewed_nothing(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        mk = lambda stage, body, path=None: ReviewComment(  # noqa: E731
            stage=stage, severity="major", body=body, path=path, line=None)
        old, _ = al.upsert(c, project_id=p, lineage=root, kind="review", severity="major",
                           fingerprint="rv:old", title="old finding in a.go", body="old",
                           path="a.go", line=3, stage="security_check", run_id=root)
        al.observe_review(
            c, project_id=p, lineage=root, run_id=root,
            comments=[mk("security_check", "blind seat said this"),
                      mk("consistency_check", "README says 8080, the code binds 9090",
                         "README.md")],
            blind_seats=("security_check",), seats=("security_check",),
            reviewed_paths=("a.go",))
        titles = [r["title"] for r in al.open_for(c, project_id=p, lineage=root)]
        assert any(t.startswith("README says") for t in titles), titles
        assert not any("blind seat" in t for t in titles)
        assert al.get(c, old["id"])["status"] == "open", "a blind seat closes nothing"


def _blind_row(c, p, root, *, kind, stage, hollow):
    c.execute(text("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                   "|| CAST(:f AS JSONB) WHERE id = :i"),
              {"i": root, "f": json.dumps({"review_rounds": [
                  {"round": 1, "seats": {stage: {"verdict": "not_run", "hollow": hollow}}}]})})
    row, _ = al.upsert(c, project_id=p, lineage=root, kind=kind, severity="major",
                       fingerprint=f"rv:{kind}{stage}", title=F42[:80], body=F42, stage=stage,
                       run_id=root)
    return row


def test_an_existing_row_a_blind_seat_raised_is_superseded_and_not_in_the_brief(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        bad = _blind_row(c, p, root, kind="review", stage="review_code",
                         hollow="NOT RUN: the reviewer's tools could not run")
        real, _ = al.upsert(c, project_id=p, lineage=root, kind="review", severity="major",
                            fingerprint="rv:real", title="README refund example is wrong",
                            body="README refund example is wrong", path="README.md", line=40,
                            stage="review_code", run_id=root)
        assert "tooling failure" in al.stale_rows(c, [bad, real], project_id=p,
                                                  lineage=root)[bad["id"]]
        plan = al.followup_spec(c, project_id=p, lineage=root)
        assert [f["ledger_id"] for f in plan["findings"]] == [real["id"]]
        assert al.get(c, bad["id"])["status"] == "superseded"


def test_the_prose_rule_never_touches_a_seat_that_ran_or_a_security_row(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        ran = _blind_row(c, p, root, kind="review", stage="review_code", hollow=None)
        assert al.stale_rows(c, [ran], project_id=p, lineage=root) == {}
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        sec = _blind_row(c, p, root, kind="security", stage="security_check", hollow="NOT RUN: x")
        assert al.stale_rows(c, [sec], project_id=p, lineage=root) == {}


def test_a_not_applicable_declaration_is_a_pending_check_a_review_confirms_or_refutes(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        rows = [{"key": "F-42", "id": "bi_x", "severity": "HIGH", "why": "stale", "item": "F-42 x"},
                {"key": "F-43", "id": "bi_y", "severity": "HIGH", "why": "done", "item": "F-43 y"}]
        ids = al.declare_not_applicable(c, project_id=p, lineage=root, run_id=root, rows=rows)
        assert len(ids) == 2 and all(al.get(c, i)["status"] == "open" for i in ids)
        refuting = {"stage": "review_code", "body": "F-43 is NOT done: router.go still joins"}
        assert al.confirm_not_applicable(c, project_id=p, lineage=root, run_id=root,
                                         comments=[refuting], seats=[]) == []
        closed = al.confirm_not_applicable(c, project_id=p, lineage=root, run_id=root,
                                           comments=[refuting], seats=["review_code"])
        assert closed == [ids[0]]
        assert al.get(c, ids[1])["status"] == "open", "a refuted declaration stays a finding"


# ── plan coverage: NOT APPLICABLE ────────────────────────────────────────────────────────────

BRIEF = ("FAILING CHECKS AND OPEN FINDINGS:\n"
         "- [HIGH] F-7 internal/proxy/router.go (`Match`) — dot segments are not cleaned\n"
         "- [HIGH] F-42: stale finding about the sandbox, nothing to change in the app\n"
         "\nFix ONLY these.")


def test_an_explicit_not_applicable_with_a_reason_is_skipped_not_missing():
    plan = ("Change internal/proxy/router.go: Match cleans dot segments.\n"
            "ITEM F-42: NOT APPLICABLE — stale: the finding describes the reviewer's sandbox "
            "failing, not the app\n")
    cov = pc.check({"plan": plan}, instructions=BRIEF)
    assert cov.ok and not cov.missing, cov.missing
    assert [s["not_applicable"] for s in cov.skipped] == [True]
    assert "sandbox" in cov.skipped[0]["why"]
    block = pc.verify_block({"skipped": cov.skipped})
    assert "NOT APPLICABLE" in block and "verify" in block


class _Says:
    def __init__(self, answer):
        self.answer = answer

    def decide(self, context, questions):
        from boss_engine.services.typed_decisions import Decision, DecisionBatch

        return DecisionBatch(decisions={q.key: Decision(self.answer, 0.9, reason="scripted")
                                        for q in questions},
                             backend="scripted", model="scripted", latency_ms=0)


def test_not_applicable_needs_a_reason_and_only_covers_the_item_it_names():
    plan = ("Change internal/proxy/router.go: Match cleans dot segments.\n"
            "ITEM F-42: NOT APPLICABLE\n")
    cov = pc.check({"plan": plan}, instructions=BRIEF, decider=_Says("missing"))
    assert not cov.ok and any("F-42" in m["item"] for m in cov.missing)
    other = ("Change internal/proxy/router.go: Match cleans dot segments.\n"
             "ITEM F-99: NOT APPLICABLE — stale, already done\n")
    assert not pc.check({"plan": other}, instructions=BRIEF, decider=_Says("missing")).ok
    assert "NOT APPLICABLE" in pc.replan_note(cov)


def test_a_security_finding_or_blocker_is_never_not_applicable():
    brief = ("- [HIGH] F-8 internal/proxy/router.go (`Match`) — dot segments are not cleaned\n"
             "- [SECURITY] F-9: the admin route has no auth\n- [BLOCKER] F-10: build is broken\n")
    plan = ("Change internal/proxy/router.go: Match.\n"
            "ITEM F-9: NOT APPLICABLE — stale, already fixed upstream\n"
            "ITEM F-10: NOT APPLICABLE — stale, already fixed upstream\n")
    cov = pc.check({"plan": plan}, instructions=brief, decider=_Says("missing"))
    assert not cov.ok and not [r for r in cov.skipped if r.get("not_applicable")], cov.skipped


def test_a_declaration_in_the_plan_review_does_not_count():
    review = "ITEM F-42: NOT APPLICABLE — stale: the sandbox failed, not the app"
    cov = pc.check({"plan": "Change internal/proxy/router.go: Match.", "plan_review": review},
                   instructions=BRIEF, decider=_Says("missing"))
    assert not cov.ok and not cov.skipped
