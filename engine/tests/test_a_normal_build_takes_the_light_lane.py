"""THE LIGHT LANE FOR NORMAL BUILDS (operator, 2026-11-18; quality_bar.py, BOSSAPP-1362).

"Make lighter and faster lane for normal builds, please dont introduce strict rules.. that's only
for production ... there is no need to fail, what user would do if the review fails?"

Easy Mode's Tile swap and Recipe box both ended on "Checks failing — decide the merge" because
MAJOR review findings were still open after the repair rounds, and the person was asked to merge
with checks failing and say why. At the default bar (standard, what Easy Mode, templates, Try a
sample and New work run at) a MAJOR finding is a follow-up:

  * the PR opens ready, the merge card is a normal one — "<name> is ready — N follow-ups queued,
    BOSS is fixing them" — never "Checks failing", never a reason to type;
  * BOSS starts the follow-up build itself and the PR lists the findings as P1/P2 follow-ups;
  * a BLOCKER still blocks, security blocks at every bar, and production stays strict.

Pure tests first, then the pipeline end to end on the scripted engine (the harness of
test_the_pipeline_runs_end_to_end.py: no model, no real CLI).
"""
from __future__ import annotations

import json
import os

import pytest

from boss_engine.services import pr_description
from boss_engine.services import quality_bar as qb
from boss_engine.services.governed_code_task_service import _review_gate_from_findings

REVIEW = ("REVIEWED: styles/globals.css\n"
          "FINDING: MAJOR styles/globals.css:161 — The serving control overflows on phones.\n"
          "FINDING: MINOR pages/new.js:40 — An empty amount is saved as 0.\n"
          "VERDICT: REFUTED")


# ── the default bar for a normal build ───────────────────────────────────────────────────────

def test_a_normal_build_with_no_setting_runs_at_standard_where_a_major_never_blocks():
    # Easy Mode, templates, Try a sample and New work set no bar: the default is standard.
    assert qb.resolve() == qb.STANDARD == qb.DEFAULT
    ok, blocking, deferred = _review_gate_from_findings(
        REVIEW, "refuted", qb.review_block_severity(qb.resolve()))
    assert ok and not blocking and [f["severity"] for f in deferred] == ["major", "minor"]


def test_a_blocker_still_blocks_at_standard_and_a_major_blocks_at_production():
    blocker = REVIEW.replace("MAJOR", "BLOCKER")
    assert not _review_gate_from_findings(blocker, "refuted",
                                          qb.review_block_severity(qb.STANDARD))[0]
    assert not _review_gate_from_findings(REVIEW, "refuted",
                                          qb.review_block_severity(qb.PRODUCTION))[0]


@pytest.mark.parametrize("bar", qb.PRESETS)
def test_security_blocks_at_every_bar(bar):
    g = qb.BarGates(bar, initial={"security_clean": False, "no_committed_secrets": False})
    assert g["security_clean"] is False and g["no_committed_secrets"] is False
    assert not g.followups


def test_the_card_and_the_pr_list_the_follow_ups_p1_first():
    evidence = {"deferred_findings": [
        {"severity": "minor", "path": "pages/new.js", "line": 40, "text": "Empty amount saves 0."},
        {"severity": "major", "path": "styles/globals.css", "line": 161,
         "text": "The serving control overflows on phones."}],
        qb.QUEUED_KEY: True, qb.KEY: {"bar": "standard", "followups": {}, "flaky": []}}
    card = qb.merge_followups(evidence)
    assert card["count"] == 2 and card["p1"] == 1 and card["queued"] is True
    assert [i["tier"] for i in card["items"]] == ["P1", "P2"]
    assert card["items"][0]["text"].startswith("styles/globals.css:161 — ")
    assert qb.ready_words("Recipe box", {**card, "count": 1}) == \
        "Recipe box is ready — 1 follow-up queued, BOSS is fixing it"
    assert qb.ready_words("Recipe box", {"count": 2, "queued": False}) == \
        "Recipe box is ready — 2 follow-ups listed for later"
    body = qb.pr_section(evidence)
    assert body.startswith("## Follow-ups (quality bar: standard)")
    assert "BOSS has queued a follow-up build" in body
    assert "- P1 [MAJOR] styles/globals.css:161 — The serving control overflows" in body
    assert "- P2 [MINOR] pages/new.js:40" in body
    assert qb.merge_followups({"deferred_findings": []}) is None


def test_a_review_refuted_only_below_the_bar_is_a_follow_up_in_the_pr_never_failed():
    evidence = {"deferred_findings": [{"severity": "major", "text": "overflows on phones"}],
                qb.KEY: {"bar": "standard", "followups": {}, "flaky": []}}
    gates = {"code_written": True, "code_review_passed": True, "security_clean": True}
    rounds = [{"seats": {"review_code": {"verdict": "refuted"},
                         "security_check": {"verdict": "clean"}}}]
    body = pr_description.render(title="Recipe box", evidence=evidence, gates=gates,
                                 rounds=rounds, deferred=evidence["deferred_findings"])
    checks = body.split("### Checks", 1)[1].split("###", 1)[0]
    assert "❌" not in checks and "0 failed" in checks
    assert "🔸 Review passed — follow-up" in checks
    # a review that did not pass its gate is still a failure
    body = pr_description.render(title="T", evidence={}, gates={**gates,
                                 "code_review_passed": False}, rounds=rounds)
    assert "❌ Review passed" in body


def test_a_failed_follow_up_of_a_ready_build_is_not_the_projects_failure():
    from boss_engine.services.build_failure import _followups_only

    parent = {"id": "crun_p", "pull_request_url": "http://x/pull/1", "merged_sha": None,
              "decision_status": "pending"}
    follow = {"id": "crun_f", "deferred_from": "crun_p"}
    assert _followups_only(follow, [parent])
    # a follow-up a person asked for, a merged parent, or one no card waits on is a failure
    assert not _followups_only({"id": "crun_f", "deferred_from": None}, [parent])
    assert not _followups_only(follow, [{**parent, "merged_sha": "abc"}])
    assert not _followups_only(follow, [{**parent, "decision_status": "executed"}])


def test_the_merge_card_reads_follow_ups_as_ready_on_every_surface():
    here = os.path.dirname(__file__)
    root = os.path.join(here, "..", "..")
    sf = open(os.path.join(root, "engine", "boss_engine", "services", "software_factory.py"),
              encoding="utf-8").read()
    assert "_quality_bar.ready_words(" in sf and "_quality_bar.CARD_KEY: _followups" in sf
    assert "None if card_checks else" in sf      # a failing card never also says "ready"
    web = os.path.join(root, "apps", "web")
    drawer = open(os.path.join(web, "app", "projects", "drawers.tsx"), encoding="utf-8").read()
    assert "`Ready to merge · ${mp.followups.words}`" in drawer
    inbox = open(os.path.join(web, "app", "decision-inbox-ui.tsx"), encoding="utf-8").read()
    assert "<FollowUps record={followUpsOf(rec)!} />" in inbox


# ── the pipeline, end to end ─────────────────────────────────────────────────────────────────

pytestmark_db = pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"),
                                   reason="needs the bench database (BOSS_TEST_DATABASE_URL)")

if os.environ.get("BOSS_TEST_DATABASE_URL"):
    from .test_the_pipeline_runs_end_to_end import pipeline  # noqa: F401


def _card(pipeline, run_id):
    with pipeline.engine.begin() as c:
        row = c.execute(pipeline.text(
            "SELECT d.id, d.status, d.question, d.recommendation FROM decisions d "
            "JOIN code_task_runs r ON r.decision_id = d.id WHERE r.id = :r"),
            {"r": run_id}).mappings().first()
    assert row is not None, "no merge card"
    rec = row["recommendation"]
    return dict(row), (rec if isinstance(rec, dict) else json.loads(rec or "{}"))


def _gate(pipeline, run_id):
    with pipeline.engine.begin() as c:
        row = c.execute(pipeline.text(
            "SELECT status, gates_failed, evidence FROM quality_gates WHERE code_task_run_id = :r"),
            {"r": run_id}).mappings().one()
    ev = row["evidence"] if isinstance(row["evidence"], dict) else json.loads(row["evidence"])
    return dict(row), ev


def _pr_body(pipeline, repo, number):
    status, pr = pipeline.hub.api.handle("GET", f"/repos/pipe/{repo}/pulls/{number}", "", None)
    assert status == 200, pr
    return str(pr.get("body") or "")


@pytestmark_db
def test_a_build_with_only_major_findings_opens_ready_and_boss_queues_the_follow_up(pipeline):
    run_id, repo = pipeline.build(faults="review_major", tag="light-major")
    assert pipeline.run(run_id, repo) is None
    out = pipeline.result(run_id)
    run = out["run"]
    assert run["status"] == "completed", run.get("error")
    assert run["pull_request_url"]
    gate, ev = _gate(pipeline, run_id)
    assert gate["status"] == "pass" and not gate["gates_failed"], gate
    assert ev["quality_bar"]["bar"] == "standard"
    assert "code_task.pr_opened_with_checks_failing" not in out["events"]
    assert not (run.get("finalize_ctx") or {}).get("checks_failing")
    # the merge card: a normal ready card naming its follow-ups, no reason to type
    card, rec = _card(pipeline, run_id)
    assert card["status"] == "pending"
    assert not card["question"].startswith("Checks failing"), card["question"]
    assert " is ready — 2 follow-ups queued, BOSS is fixing them" in card["question"]
    assert "checks_failing" not in rec and rec["action"] == "review_pull_request"
    assert rec["followups"]["count"] == 2 and rec["followups"]["queued"] is True
    assert [i["tier"] for i in rec["followups"]["items"]] == ["P1", "P2"]
    assert rec["summary"].startswith("Ready to merge — 2 follow-ups queued")
    # BOSS started the follow-up itself
    with pipeline.engine.begin() as c:
        skipped = [r[0] for r in c.execute(pipeline.text(
            "SELECT data::text FROM events WHERE entity_id = :r AND event_type = "
            "'code_task.followup_skipped'"), {"r": run_id})]
    assert "code_task.deferred_findings_task" in out["events"], skipped
    assert out["children"], "no follow-up build was queued"
    # the PR lists them as P1/P2 follow-ups and never calls the review failed
    body = _pr_body(pipeline, repo, run["pull_request_number"])
    assert "## Follow-ups (quality bar: standard)" in body
    assert "P1 [MAJOR]" in body and "phone-width" in body and "P2 [MINOR]" in body
    checks = body.split("### Checks", 1)[1].split("###", 1)[0]
    assert "❌" not in checks, checks


@pytestmark_db
def test_a_blocker_that_stays_still_ends_on_checks_failing_at_standard(pipeline):
    run_id, repo = pipeline.build(faults="review_blocker_stays", tag="light-blocker")
    assert pipeline.run(run_id, repo) is None
    gate, _ = _gate(pipeline, run_id)
    assert "code_review_passed" in (gate["gates_failed"] or []), gate
    card, rec = _card(pipeline, run_id)
    assert card["question"].startswith("Checks failing"), card["question"]
    assert "followups" not in rec


@pytestmark_db
@pytest.mark.parametrize("bar", ["prototype", "standard"])
def test_a_security_finding_blocks_at_every_bar(pipeline, bar):
    run_id, repo = pipeline.build(faults="security_finding", tag=f"light-sec-{bar}")
    with pipeline.engine.begin() as c:
        c.execute(pipeline.text(
            "UPDATE code_task_runs SET spec = COALESCE(spec,'{}'::jsonb) || CAST(:s AS jsonb) "
            "WHERE id = :r"), {"s": json.dumps({"quality_bar": bar}), "r": run_id})
    assert pipeline.run(run_id, repo) is None
    gate, ev = _gate(pipeline, run_id)
    assert ev["quality_bar"]["bar"] == bar
    assert "security_clean" in (gate["gates_failed"] or []), gate
    with pipeline.engine.begin() as c:
        q = c.execute(pipeline.text(
            "SELECT d.question FROM decisions d JOIN code_task_runs r ON r.decision_id = d.id "
            "WHERE r.id = :r"), {"r": run_id}).scalar()
    assert q is None or q.startswith("Checks failing"), q


@pytestmark_db
def test_the_same_major_findings_block_at_the_production_bar(pipeline):
    run_id, repo = pipeline.build(faults="review_major", tag="light-prod")
    with pipeline.engine.begin() as c:
        c.execute(pipeline.text(
            "UPDATE code_task_runs SET spec = COALESCE(spec,'{}'::jsonb) || CAST(:s AS jsonb) "
            "WHERE id = :r"), {"s": json.dumps({"quality_bar": "production"}), "r": run_id})
    assert pipeline.run(run_id, repo) is None
    gate, ev = _gate(pipeline, run_id)
    assert ev["quality_bar"]["bar"] == "production"
    assert "code_review_passed" in (gate["gates_failed"] or []), gate
    card, rec = _card(pipeline, run_id)
    assert card["question"].startswith("Checks failing") and "followups" not in rec
