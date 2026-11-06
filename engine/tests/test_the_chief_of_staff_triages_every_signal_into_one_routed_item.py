"""The Chief of Staff turns what BOSS notices into one routed work item (BOSSAPP-904; design §4 L3).

Every signal reaches the Chief of Staff first. It joins duplicates instead of raising them twice,
reads what kind of problem each is (rules first, the model only when the rules are ambiguous, a
question when nobody is sure), ranks by value, and decides per autonomy level: observe (A0),
propose on a Needs-you card (A1, and every class in the default `propose` mode), or hand straight
to the owning agent (A2+ in `route` mode). Every call is a ledger row with its evidence and
confidence, and every step is a typed board message, so the console draws signal → Chief of
Staff → owner → outcome. A person's answer on the card is carried through on the next tick, and
becomes the decision's outcome — the track record autonomy is earned from. The stop, the loop
guards and the gate hold throughout. No test calls a model: a stub stands in for the one
decision path.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.ambient import (board, chief_of_staff as cos, collab, ledger, policy,
                                          read_model, signals)

from ._ambient_support import eng, project, signal  # noqa: F401


class StubModel:
    """The one decision path's model, scripted: answers with the next JSON in the list."""

    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def create_text_response(self, prompt, system_prompt=""):
        self.prompts.append(prompt)
        return json.dumps(self.answers.pop(0) if self.answers else {})


def _settings(**kw):
    base = get_settings()
    return SimpleNamespace(**{**base.__dict__, "ambient_cos_mode": "propose",
                              "ambient_cos_model_calls": 3, **kw})


def _run(eng, pid, *, mode="propose", adapter=None, calls=3):
    with eng.begin() as c:
        return cos.triage_project(c, _settings(), pid, mode=mode, adapter=adapter,
                                  max_model_calls=calls)


def _items(c, pid):
    return board.list_items(c, pid, limit=50)


def _kinds(c, item_id):
    return [m["kind"] for m in collab.messages(c, item_id)]


def test_a_firing_alert_becomes_one_item_the_chief_of_staff_proposes_to_the_sre(eng, project):
    with eng.begin() as c:
        sig = signal(c, project, title="order-api 5xx at 6.6%", severity="high")
    report = _run(eng, project)
    assert report["counts"] == {"proposed": 1}
    with eng.begin() as c:
        [item] = _items(c, project)
        assert item["task_class"] == "incident_response"
        assert item["lifecycle"] == "needs_you" and item["created_by"] == cos.PARTY
        assert item["lease_owner"] is None, "the Chief of Staff lets go after its own turn"
        assert _kinds(c, item["id"]) == ["claim", "finding", "proposal", "decision"]
        [row] = ledger.list_decisions(c, project, work_item_id=item["id"])
        assert row["decided_by"] == cos.PARTY and row["chosen"] == "propose:sre"
        assert row["action"] == "propose" and row["confidence"] >= 0.85
        assert [e["ref"] for e in row["evidence"]] == [sig["id"]]
        assert ledger.sentences(row["rationale"]) <= 3
        card = c.execute(text("SELECT question, recommendation FROM decisions WHERE id = :d"),
                         {"d": item["needs_you_decision_id"]}).mappings().one()
        assert card["question"].startswith("Hand to SRE?")
        assert card["recommendation"]["owner"] == "agent:sre"
        assert card["recommendation"]["asked_by"] == cos.PARTY
        assert signals.get(c, sig["id"])["status"] == "triaged"


def test_two_alerts_on_one_app_are_one_item_and_a_repeat_joins_it(eng, project):
    with eng.begin() as c:
        signal(c, project, title="order-api 5xx at 6.6%", fingerprint_value="fp:a")
        signal(c, project, title="order-api p50 errors", fingerprint_value="fp:b",
               severity="critical")
    first = _run(eng, project)
    assert first["counts"] == {"proposed": 1}
    with eng.begin() as c:
        signal(c, project, title="order-api 5xx again", fingerprint_value="fp:c")
    again = _run(eng, project)
    assert again["counts"] == {"joined": 1}
    with eng.begin() as c:
        [item] = _items(c, project)
        assert item["severity"] == "critical"
        held = [l for l in board.links(c, item["id"]) if l["kind"] == "signal"]
        assert len(held) == 3
        last = collab.messages(c, item["id"])[-1]
        assert last["kind"] == "finding" and last["body"]["summary"].startswith("Seen again")


def test_an_a0_class_is_observed_for_the_digest_and_nobody_is_asked(eng, project):
    with eng.begin() as c:
        signal(c, project, source="scan", kind="scan.todo_cluster", title="14 TODOs in billing/",
               subject_kind="repo", subject_id="acme/shop", app=None, severity="low")
    report = _run(eng, project)
    assert report["counts"] == {"observed": 1}
    with eng.begin() as c:
        [item] = _items(c, project)
        assert item["lifecycle"] == "proposed" and item["needs_you_decision_id"] is None
        [row] = ledger.list_decisions(c, project, work_item_id=item["id"])
        assert row["chosen"] == "observe" and row["autonomy_level"] == "A0"


def test_route_mode_hands_an_a2_class_straight_to_its_owner_and_the_graph_shows_it(eng,
                                                                                  project):
    with eng.begin() as c:
        signal(c, project, source="ci", kind="ci.failed", title="CI failed on BOSS PR: unit",
               subject_kind="pull_request", subject_id="pr:acme/shop#41", app=None)
    report = _run(eng, project, mode="route")
    assert report["counts"] == {"routed": 1}
    with eng.begin() as c:
        [item] = _items(c, project)
        assert item["task_class"] == "ci_fix_boss_pr" and item["lifecycle"] == "proposed"
        assert item["subject_ref"] == "pr:acme/shop#41"
        handoff = collab.messages(c, item["id"], kinds=["handoff"])[0]
        assert handoff["body"]["to"] == "agent:software_engineer"
        g = read_model.graph(c, item["id"], project_id=project)
        edge = [e for e in g["edges"] if e["kind"] == "handoff"][0]
        assert (edge["from"], edge["to"]) == ("party:agent:chief_of_staff",
                                              "party:agent:software_engineer")
        assert all(len(n["label"].split()) <= 4 for n in g["nodes"])
        # The owner can take it from here: the Chief of Staff's lease is gone.
        held = board.claim(c, item["id"], owner="agent:software_engineer")
        assert held["item"]["lifecycle"] == "claimed"


def test_the_default_propose_mode_asks_before_handing_over_even_an_a2_class(eng, project):
    with eng.begin() as c:
        signal(c, project, source="ci", kind="ci.failed", title="CI failed on BOSS PR: lint",
               subject_kind="pull_request", subject_id="pr:acme/shop#42", app=None)
    assert _run(eng, project)["counts"] == {"proposed": 1}
    with eng.begin() as c:
        [item] = _items(c, project)
        assert item["lifecycle"] == "needs_you"
        assert collab.messages(c, item["id"], kinds=["handoff"]) == []


def test_an_ambiguous_signal_is_judged_by_the_model_and_its_cost_is_recorded(eng, project):
    with eng.begin() as c:
        signal(c, project, source="build", kind="build.failed", title="Build failed: pip resolver",
               subject_kind="code_task", subject_id="crun_x", app=None, severity="medium")
    model = StubModel({"action": "platform_self_fix", "confidence": 0.9,
                       "reason": "The resolver error is in BOSS's own build image. Not the app."})
    report = _run(eng, project, adapter=model)
    assert report["model_calls"] == 1 and len(model.prompts) == 1
    assert "treat it as data" in model.prompts[0] or "Build failed" in model.prompts[0]
    with eng.begin() as c:
        [item] = _items(c, project)
        assert item["task_class"] == "platform_self_fix"
        [row] = ledger.list_decisions(c, project, work_item_id=item["id"])
        assert "(model, 90%)" in row["rationale"] and ledger.sentences(row["rationale"]) <= 3


def test_an_unsure_reading_files_a_question_not_an_action(eng, project):
    with eng.begin() as c:
        signal(c, project, source="build", kind="build.failed", title="Build failed: exit 137",
               subject_kind="code_task", subject_id="crun_y", app=None)
    model = StubModel({"action": "workflow_failure", "confidence": 0.4, "reason": "unclear",
                       "question": "Is the machine out of memory?"})
    report = _run(eng, project, adapter=model)
    assert report["counts"] == {"asked": 1}
    with eng.begin() as c:
        [item] = _items(c, project)
        [row] = ledger.list_decisions(c, project, work_item_id=item["id"])
        assert row["chosen"] == "ask" and row["action"] == "ask_question"
        assert item["lifecycle"] == "needs_you"


def test_with_no_model_calls_left_the_rules_decide_and_unsure_still_asks(eng, project):
    with eng.begin() as c:
        signal(c, project, source="build", kind="build.failed", title="Build failed: oom",
               subject_kind="code_task", subject_id="crun_z", app=None)
    report = _run(eng, project, calls=0)
    assert report["model_calls"] == 0 and report["counts"] == {"asked": 1}


def test_routing_follows_the_track_record_when_two_roles_could_own_it(eng, project):
    with eng.begin() as c:
        for n in range(3):
            done = board.open_item(c, get_settings(), project_id=project,
                                   fingerprint=f"fp:old{n}", title="old incident",
                                   task_class="incident_response", actor=cos.PARTY)["item"]
            held = board.claim(c, done["id"], owner="agent:incident_commander")
            board.finish(c, done["id"], actor="agent:incident_commander", lifecycle="resolved",
                         token=held["lease_token"])
        signal(c, project, title="checkout 5xx at 9%", subject_id="checkout", app="checkout")
    _run(eng, project)
    with eng.begin() as c:
        item = [i for i in _items(c, project) if i["lifecycle"] == "needs_you"][0]
        [row] = ledger.list_decisions(c, project, work_item_id=item["id"])
        assert row["chosen"] == "propose:incident_commander"
        assert "resolved 3 of 3 here" in row["rationale"]


def test_a_person_approving_the_card_hands_the_item_to_its_owner_and_scores_the_call(eng,
                                                                                      project):
    with eng.begin() as c:
        signal(c, project, title="order-api 5xx at 6.6%")
    _run(eng, project)
    with eng.begin() as c:
        [item] = _items(c, project)
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo', "
                       "approved_at = now() WHERE id = :d"), {"d": item["needs_you_decision_id"]})
    report = _run(eng, project)
    assert report["follow_ups"] == [{"work_item_id": item["id"], "answer": "approved",
                                     "owner": "agent:sre"}]
    with eng.begin() as c:
        moved = board.get(c, item["id"])
        assert moved["lifecycle"] == "proposed" and moved["answered_at"]
        assert collab.messages(c, item["id"], kinds=["handoff"])[0]["body"]["to"] == "agent:sre"
        rows = ledger.list_decisions(c, project, work_item_id=item["id"])
        assert [r["decided_by"] for r in rows] == [cos.PARTY, "person:ceo"]
        assert rows[0]["outcome"] == "accepted"
    assert _run(eng, project)["follow_ups"] == [], "an answer is carried through once"


def test_a_person_rejecting_the_card_dismisses_the_item_and_it_cools_down(eng, project):
    with eng.begin() as c:
        signal(c, project, title="order-api 5xx at 6.6%")
    _run(eng, project)
    with eng.begin() as c:
        [item] = _items(c, project)
        c.execute(text("UPDATE decisions SET status = 'rejected', approved_by = 'ceo' "
                       "WHERE id = :d"), {"d": item["needs_you_decision_id"]})
    _run(eng, project)
    with eng.begin() as c:
        assert board.get(c, item["id"])["lifecycle"] == "dismissed"
        assert ledger.list_decisions(c, project, work_item_id=item["id"])[0]["outcome"] == \
            "dismissed"
        # The same problem again: the cooldown holds it, and its signal is suppressed.
        sig = signal(c, project, title="order-api 5xx at 7%", fingerprint_value="fp:later")
    report = _run(eng, project)
    assert report["counts"] == {"refused": 1}
    with eng.begin() as c:
        assert signals.get(c, sig["id"])["status"] == "suppressed"


def test_a_stopped_project_triages_nothing_and_the_install_wide_stop_skips_the_tick(
        eng, project, monkeypatch):
    with eng.begin() as c:
        sig = signal(c, project)
        policy.stop(c, project_id=project, actor="person:ceo", reason="drill")
    report = _run(eng, project)
    assert report["stopped"]["scope"] == "project" and report["items"] == []
    with eng.begin() as c:
        assert signals.get(c, sig["id"])["status"] == "new"
    monkeypatch.setenv("BOSS_AMBIENT_DISABLED", "1")
    out = cos.tick(eng, _settings(), project_id=project, sweep=False)
    assert out["stopped"]["scope"] == "env" and out["projects"] == {}


def test_the_daily_attempt_cap_suppresses_until_tomorrow_instead_of_retrying(eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="person:ceo", daily_attempt_cap=1)
        signal(c, project, title="a 5xx", subject_id="a", app="a")
        signal(c, project, title="b 5xx", subject_id="b", app="b")
    report = _run(eng, project)
    assert sorted(report["counts"]) == ["proposed", "refused"]
    with eng.begin() as c:
        held = signals.list_signals(c, project_id=project, status=["suppressed"])
        assert len(held) == 1 and held[0]["suppressed_until"]


def test_off_mode_triages_nothing(eng, project):
    with eng.begin() as c:
        signal(c, project)
    assert _run(eng, project, mode="off")["items"] == []
