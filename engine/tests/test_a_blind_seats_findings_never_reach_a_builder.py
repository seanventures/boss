"""BOSSAPP-1360: what a blind review seat (not_run / hollow / tools_blocked) said, and any
finding that only describes BOSS's own tooling failing, is never ledgered, never carried by a
follow-up brief and never sent to an app builder; the planner may declare an item NOT APPLICABLE
with a reason, and that is an item to verify, not a reason to loop the plan back to a person.

THE CASE: crun_9f190ed1ede845c09cdc's seat could not create a namespace; its F-42 "every command
failed before execution ... bwrap: ..." stayed in the brief of the next run, which looped the
planner 3x and escalated to a person."""
from __future__ import annotations

from boss_engine.services import plan_coverage as pc
from boss_engine.services import review_tools
from boss_engine.services.acceptance import ledger as al
from boss_engine.services.review_comments import ReviewComment

from .test_acceptance_findings_ledger import _run, ledger_project  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

F42 = ("The required re-check is incomplete. Every command failed before execution because the "
       "BOSS command sandbox could not create a namespace (`bwrap: No permissions to create a "
       "new namespace`).")


def test_a_tooling_failure_is_told_from_a_real_finding_about_a_sandbox_feature():
    assert review_tools.is_tooling_failure(F42)
    # Anchored to a file: a real review of code that uses bwrap.
    assert not review_tools.is_tooling_failure(F42, "internal/sandbox/run.go")
    assert not review_tools.is_tooling_failure("The handler leaks the token in the log line.")


def test_findings_of_a_blind_seat_and_tooling_failures_are_dropped():
    real = {"stage": "review_code", "body": "router.go does not clean dot segments",
            "path": "internal/proxy/router.go"}
    comments = [real,
                {"stage": "security_check", "body": "a finding from a seat that did not run"},
                {"stage": "review_code", "body": F42}]
    kept = review_tools.drop_tooling_failures(comments, {"security_check": "NOT RUN: tools"})
    assert kept == [real]


def test_a_blind_seats_findings_are_not_ledgered(eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        mk = lambda stage, body, path=None: ReviewComment(  # noqa: E731
            stage=stage, severity="major", body=body, path=path, line=None)
        delta = al.observe_review(
            c, project_id=p, lineage=root, run_id=root,
            comments=[mk("review_code", F42), mk("security_check", "blind seat said this"),
                      mk("consistency_check", "README says 8080, the code binds 9090",
                         "README.md")],
            blind_seats=("security_check",))
        keys = [r["title"] for r in al.open_for(c, project_id=p, lineage=root)]
        assert len(keys) == 1 and keys[0].startswith("README says"), keys
        assert delta is not None


def test_an_existing_tooling_failure_row_is_superseded_and_not_in_the_brief(
        eng, ledger_project):  # noqa: F811
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, fctx={})
        bad, _ = al.upsert(c, project_id=p, lineage=root, kind="review", severity="major",
                           fingerprint="rv:f42", title=F42[:80], body=F42, stage="review_code",
                           run_id=root)
        real, _ = al.upsert(c, project_id=p, lineage=root, kind="review", severity="major",
                            fingerprint="rv:real", title="README refund example is wrong",
                            body="README refund example is wrong", path="README.md", line=40,
                            stage="review_code", run_id=root)
        assert "tooling failure" in al.stale_rows(c, [bad, real], project_id=p,
                                                  lineage=root)[bad["id"]]
        plan = al.followup_spec(c, project_id=p, lineage=root)
        assert [f["ledger_id"] for f in plan["findings"]] == [real["id"]]
        got = al.get(c, bad["id"])
        assert got["status"] == "superseded" and "tooling failure" in got["waive_reason"]


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
