"""The Recommended roadmap ranks, dedupes, and learns from a person's review (BOSSAPP-911; design
§14.2-§14.4; spec §9).

* A run records each finding as a signal, opens ONE roadmap item per subject with the Chief of
  Staff's proposal on the ledger (evidence verified), and ranks by value density.
* Re-running is idempotent: the same evidence opens nothing new; more evidence joins the live
  item and re-scores it; evidence that vanished closes it ("resolved by change").
* Accept makes real work through the normal paths (a ticket on the work board, a PRD panel) and
  settles the proposal as accepted. Reject needs a reason, dismisses the item, suppresses the
  fingerprint until the evidence grows, and lowers the detector's weight; "too small" raises its
  thresholds; a detector under 15 % after 20 decided is muted and reported. Not now hides it
  until the date.
* An accepted problem whose evidence keeps coming RECURRED. The stop and the kill switch hold;
  a run opens at most its cap; an optional phraser is bounded and changes only the words.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import board, ledger, policy, roadmap, signals
from boss_engine.services.ambient import roadmap_demo as demo
from boss_engine.services.ambient._common import now

from ._learning_support import eng, project  # noqa: F401

DAY = timedelta(days=1)


def _incidents(c, pid, n, cause="payment.timeout", start=1, title="Webhook timeout"):
    return [demo.put_incident(c, pid, title=title, cause_key=cause, at=now() - d * DAY)
            for d in range(start, start + n)]


def _open(c, pid):
    return roadmap.list_roadmap(c, pid, status="open")["items"]


def test_a_run_opens_one_item_per_subject_with_a_verified_proposal_on_the_ledger(eng, project):
    with eng.begin() as c:
        incs = _incidents(c, project, 3)
        report = roadmap.run(c, project, only=["incident_cause"])
        items = _open(c, project)
        rec = roadmap.get(c, items[0]["id"], project_id=project)
        decision = ledger.get_decision(c, rec["detail"]["proposal_act_id"])
        item = board.get(c, items[0]["id"])
        sig = signals.list_signals(c, project_id=project, source="kg")
    assert len(report["opened"]) == 1 and len(items) == 1
    assert item["task_class"] == "roadmap_item" and item["lifecycle"] == "proposed"
    assert decision["decided_by"] == "agent:chief_of_staff" and decision["action"] == "propose"
    assert decision["chosen"] == "recommend" and ledger.sentences(decision["rationale"]) <= 3
    cited = {(e["kind"], e["ref"]) for e in decision["evidence"]}
    assert {("incident", i) for i in incs} <= cited
    assert all(e["checked"] == "exists" for e in decision["evidence"])
    assert sig[0]["kind"] == "learn.incident_cause" and sig[0]["status"] == "triaged"
    assert rec["badges"]["single_source"] is True
    assert [n["type"] for n in rec["nodes"]] == ["signal", "agent", "action"]
    assert all(len(n["label"].split()) <= 4 for n in rec["nodes"])


def test_running_again_opens_nothing_new_and_more_evidence_joins_and_rescores(eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2)
        first = roadmap.run(c, project, only=["incident_cause"])
        again = roadmap.run(c, project, only=["incident_cause"])
        before = roadmap.get(c, first["opened"][0], project_id=project)
        _incidents(c, project, 2, start=5)
        grown = roadmap.run(c, project, only=["incident_cause"])
        after = roadmap.get(c, first["opened"][0], project_id=project)
        occurrences = c.execute(text(
            "SELECT occurrences FROM signals WHERE project_id = :p AND kind = "
            "'learn.incident_cause'"), {"p": project}).scalar()
    assert again["opened"] == [] and again["joined"] == first["opened"]
    assert grown["opened"] == [] and grown["joined"] == first["opened"]
    assert before["badges"]["evidence"] == 2 and after["badges"]["evidence"] == 4
    assert after["detail"]["value"] > before["detail"]["value"]
    assert occurrences == 2           # the same evidence re-read was a duplicate, not a repeat


def test_ranking_puts_strong_multi_source_evidence_above_a_single_source(eng, project):
    with eng.begin() as c:
        f = demo.put_file(c, project, "shop/checkout.py", lines=1500)
        for n in range(8):
            demo.put_pr(c, project, 10 + n, [f["path"]], at=now() - (10 - n) * DAY)
        for d in (2, 6, 9):
            demo.put_incident(c, project, title="Double charge", suspect_file=f["path"],
                              cause_key="checkout.charge", at=now() - d * DAY)
        for n in range(5):
            run = demo.put_build(c, project)
            demo.put_profile(c, project, run, {"design": 8.0})
        roadmap.run(c, project)
        items = _open(c, project)
    assert items[0]["subject"] == "file:shop/checkout.py"
    assert set(items[0]["detectors"]) == {"hotspot", "incident_cause", "missing_tests"}
    assert items[0]["badges"]["sources"] == 2 and not items[0]["badges"]["single_source"]
    assert [i["rank"] for i in items] == list(range(1, len(items) + 1))
    assert items[-1]["badges"]["score"] <= items[0]["badges"]["score"]


def test_scoring_reuses_value_density_and_caps_a_single_source():
    assert roadmap.evidence_strength(20, 1) == roadmap.SINGLE_SOURCE_CAP
    assert roadmap.evidence_strength(8, 3) > roadmap.evidence_strength(8, 1)
    fresh = roadmap.freshness(now() - 30 * DAY, 30)
    assert fresh == pytest.approx(0.5, abs=0.01)
    assert roadmap.risk_of(0) == 1.0 and roadmap.risk_of(400) == 1.5


def test_accept_as_ticket_makes_a_board_work_item_and_settles_the_proposal(eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2)
        item_id = roadmap.run(c, project, only=["incident_cause"])["opened"][0]
        out = roadmap.accept(c, item_id, project_id=project, actor="ceo", as_="ticket",
                             note="Next sprint.")
        ticket = c.execute(text("SELECT * FROM work_items WHERE id = :w"),
                           {"w": out["artifact"]["work_item_id"]}).mappings().one()
        rec = roadmap.get(c, item_id, project_id=project)
        proposal = ledger.get_decision(c, rec["detail"]["proposal_act_id"])
        held = board.get(c, item_id)
        example = c.execute(text(
            "SELECT label, outcome FROM learning_examples WHERE project_id = :p AND agent = "
            "'roadmap'"), {"p": project}).one()
        with pytest.raises(board.Conflict):
            roadmap.accept(c, item_id, project_id=project, actor="ceo")
    assert not ticket["ambient"] and ticket["source"] == "roadmap"
    assert ticket["subject_type"] == "recommendation" and ticket["subject_id"] == item_id
    assert ticket["workload_class"] == "roadmap_ticket"
    assert out["decision"]["decided_by"] == "person:ceo"
    assert out["decision"]["chosen"] == "accept_as_ticket"
    assert out["decision"]["parent_act_id"] == proposal["act_id"]
    assert proposal["outcome"] == "accepted" and held["lifecycle"] == "resolved"
    assert rec["status"] == "accepted" and rec["accepted_as"] == "ticket"
    assert [n["type"] for n in rec["nodes"]][-2:] == ["decision", "run"]
    assert tuple(example) == ("incident_cause", "accepted")


def test_accept_as_prd_convenes_a_prd_panel(eng, project):
    with eng.begin() as c:
        for _ in range(2):
            demo.put_build(c, project, consistency="drift", decision_id="dec_x")
        item_id = roadmap.run(c, project, only=["design_drift"])["opened"][0]
        out = roadmap.accept(c, item_id, project_id=project, actor="ceo")
        meeting = c.execute(text("SELECT project_id, constraints FROM meeting_runs WHERE id = :m"),
                            {"m": out["artifact"]["meeting_run_id"]}).mappings().one()
        links = board.links(c, item_id)
    assert meeting["project_id"] == project
    constraints = meeting["constraints"] if isinstance(meeting["constraints"], dict) \
        else json.loads(meeting["constraints"])
    assert constraints["source"] == "roadmap" and constraints["recommendation_id"] == item_id
    assert ("meeting", out["artifact"]["meeting_run_id"]) in {(l["kind"], l["ref"])
                                                             for l in links}


def test_reject_needs_a_reason_suppresses_until_the_evidence_grows_then_resurfaces(
        eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2)
        item_id = roadmap.run(c, project, only=["incident_cause"])["opened"][0]
        with pytest.raises(ValueError, match="say why"):
            roadmap.reject(c, item_id, project_id=project, actor="ceo", reason=" ")
        roadmap.reject(c, item_id, project_id=project, actor="ceo", reason="Vendor's bug.",
                       category="wrong")
        proposal = ledger.get_decision(
            c, roadmap.get(c, item_id, project_id=project)["detail"]["proposal_act_id"])
        quiet = roadmap.run(c, project, only=["incident_cause"])
        sig_status = c.execute(text(
            "SELECT status FROM signals WHERE project_id = :p AND kind = 'learn.incident_cause'"),
            {"p": project}).scalar()
        stats = roadmap.detector_stats(c, project)["incident_cause"]
        _incidents(c, project, 2, start=10)
        policy.update_settings(c, project, actor="boss", cooldown_hours=0)
        back = roadmap.run(c, project, only=["incident_cause"])
        new = roadmap.get(c, back["opened"][0], project_id=project)
    assert proposal["outcome"] == "dismissed"
    assert quiet["opened"] == [] and quiet["suppressed"] == [item_id]
    assert sig_status == "suppressed"
    assert stats["rejected"] == 1 and stats["weight"] < 1.0
    assert back["resurfaced"] == back["opened"] and new["resurfaced_from"] == item_id
    assert new["badges"]["resurfaced"] is True


def test_too_small_rejections_raise_a_detectors_thresholds(eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2)
        item_id = roadmap.run(c, project, only=["incident_cause"])["opened"][0]
        roadmap.reject(c, item_id, project_id=project, actor="ceo", reason="Two is noise.",
                       category="too_small")
        for _ in range(3):   # four "too small" in all: thresholds × 2
            from boss_engine.services.learning_loop import LearningLoopService

            LearningLoopService().record(c, project, "roadmap", "recommendation_feedback",
                                         label="incident_cause", outcome="rejected",
                                         payload={"category": "too_small"})
        tuning = roadmap.detector_tuning(c, project)
        policy.update_settings(c, project, actor="boss", cooldown_hours=0)
        _incidents(c, project, 2, start=20, cause="payment.other")
        report = roadmap.run(c, project, only=["incident_cause"])
    assert tuning == {"incident_cause": 2.0}
    assert report["opened"] == []          # 2 incidents no longer clear a bar of 4


def test_a_detector_people_keep_rejecting_is_muted_and_reported(eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="boss", daily_attempt_cap=50)
        for n in range(20):
            _incidents(c, project, 2, cause=f"cause.{n}", start=1 + n)
        report = roadmap.run(c, project, only=["incident_cause"], max_new=20)
        for item_id in report["opened"][:20]:
            roadmap.reject(c, item_id, project_id=project, actor="ceo", reason="Noise.",
                           category="not_valuable")
        stats = roadmap.detector_stats(c, project)["incident_cause"]
        muted_event = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND event_type = "
            "'ambient.detector_muted'"), {"p": project}).scalar()
        again = roadmap.run(c, project, only=["incident_cause"])
    assert stats["decided"] == 20 and stats["muted"] is True
    assert muted_event["detector"] == "incident_cause"
    assert again["detectors"]["incident_cause"].get("muted") is True


def test_not_now_hides_an_item_until_its_date(eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2)
        item_id = roadmap.run(c, project, only=["incident_cause"])["opened"][0]
        with pytest.raises(ValueError):
            roadmap.snooze(c, item_id, project_id=project, actor="ceo", until=now() - DAY)
        roadmap.snooze(c, item_id, project_id=project, actor="ceo", until=now() + 3 * DAY)
        hidden = _open(c, project)
        c.execute(text("UPDATE ambient_recommendations SET snoozed_until = now() - interval "
                       "'1 minute' WHERE work_item_id = :w"), {"w": item_id})
        shown = _open(c, project)
    assert hidden == [] and [i["id"] for i in shown] == [item_id]


def test_an_item_whose_evidence_vanished_closes_itself(eng, project):
    with eng.begin() as c:
        for n in range(5):
            run = demo.put_build(c, project)
            demo.put_profile(c, project, run, {"design": 9.0})
        item_id = roadmap.run(c, project, only=["slow_stage"])["opened"][0]
        c.execute(text("DELETE FROM events WHERE project_id = :p AND event_type = "
                       "'code_task.build_profile'"), {"p": project})
        report = roadmap.run(c, project, only=["slow_stage"])
        rec = roadmap.get(c, item_id, project_id=project)
        item = board.get(c, item_id)
    assert report["closed"] == [item_id]
    assert rec["status"] == "resolved_by_change" and item["lifecycle"] == "resolved"
    assert rec["nodes"][-1]["label"] == "Resolved by change"


def test_an_accepted_problem_that_keeps_coming_back_recurred(eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2, start=30)
        item_id = roadmap.run(c, project, only=["incident_cause"])["opened"][0]
        out = roadmap.accept(c, item_id, project_id=project, actor="ceo")
        c.execute(text("UPDATE ambient_recommendations SET decided_at = now() - interval "
                       "'20 days' WHERE work_item_id = :w"), {"w": item_id})
        _incidents(c, project, 1, start=1)
        report = roadmap.run(c, project, only=["incident_cause"])
        accept_row = ledger.get_decision(c, out["decision"]["act_id"])
    assert report["recurred"] == [item_id] and report["opened"] == []
    assert accept_row["outcome"] == "recurred"


def test_the_stop_and_the_kill_switch_hold_and_a_run_opens_at_most_its_cap(
        eng, project, monkeypatch):
    with eng.begin() as c:
        for n in range(4):
            _incidents(c, project, 2, cause=f"c.{n}", start=1 + n * 2)
        policy.stop(c, project_id=project, actor="ceo", reason="pause")
        stopped = roadmap.run(c, project)
        policy.resume(c, project_id=project, actor="ceo")
        monkeypatch.setenv("BOSS_AMBIENT_DISABLED", "1")
        disabled = roadmap.run(c, project)
        monkeypatch.delenv("BOSS_AMBIENT_DISABLED")
        capped = roadmap.run(c, project, only=["incident_cause"], max_new=2)
    assert stopped["skipped"] == "stopped" and disabled["skipped"] == "disabled"
    assert len(capped["opened"]) == 2
    assert [r["guard"] for r in capped["refused"]] == ["run_cap", "run_cap"]


def test_a_phraser_only_rewords_and_is_bounded(eng, project):
    calls = []

    def phraser(payload):
        calls.append(payload)
        return {"title": "Make webhooks retry-safe", "why": "Two pages, one cause.",
                "evidence": [], "cost_usd_micros": 1500}

    roadmap.set_phraser(phraser)
    try:
        with eng.begin() as c:
            policy.update_settings(c, project, actor="boss", daily_attempt_cap=50)
            for n in range(7):
                _incidents(c, project, 2, cause=f"p.{n}", start=1 + 2 * n)
            report = roadmap.run(c, project, only=["incident_cause"], max_new=7)
            recs = [roadmap.get(c, i, project_id=project) for i in report["opened"]]
    finally:
        roadmap.set_phraser(None)
    assert len(calls) == roadmap.MAX_PHRASE_CALLS
    worded = [r for r in recs if r["title"] == "Make webhooks retry-safe"]
    assert len(worded) == roadmap.MAX_PHRASE_CALLS
    assert all(r["badges"]["evidence"] == 2 for r in worded)


def test_the_tick_runs_due_projects_once_and_sweeps_outcomes(eng, project):
    with eng.begin() as c:
        _incidents(c, project, 2)
    first = roadmap.tick(eng, only_projects=[project])
    second = roadmap.tick(eng, only_projects=[project])
    assert first["projects"].get(project, {}).get("opened") == 1
    assert project not in second["projects"]
    assert set(first["outcomes"]) == {"ignored", "recurred", "merged", "reverted"}


def test_demo_data_goes_only_into_a_demo_project(eng, project, monkeypatch):
    monkeypatch.delenv("BOSS_DEMO_DATA", raising=False)
    with eng.begin() as c:
        with pytest.raises(PermissionError):
            demo.seed(c, project)
        monkeypatch.setenv("BOSS_DEMO_DATA", "1")
        out = demo.seed(c, project)
        items = roadmap.list_roadmap(c, project, status="all")
    assert len(out["report"]["opened"]) >= 8
    assert set(out["decided"]) == {"accepted", "rejected"}
    assert items["items"][0]["status"] in ("open", "accepted")
    assert {i["badges"]["kind"] for i in items["items"]} >= {"Hardening", "Cost", "Process",
                                                              "Test gap"}
