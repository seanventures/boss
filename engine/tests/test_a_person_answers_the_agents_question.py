"""When the on-call agent asks, a person answers — and BOSS acts as if the agent had decided.

Order API release 1.2, 2026-10-19: the SEV1 error-rate alert fired; Beacon wanted to open its own
incident but was 62% sure, so it asked. Nothing in the console could answer it, the question read
"do you want me to It is a different problem from … or It needs a person now: …", every alert
stayed "Held for the agent" so nobody was told, and the breach had joined an hours-old incident
whose only live alert was an unrelated p95 one. These pin the class:

  * the question is built from short candidate labels, never pasted descriptions;
  * an asking triage records its options; a person's pick runs the SAME path (triage_alert),
    recorded as the person's answer beside the agent's question, and Respond automatically runs;
  * an asking (or deciding) triage ends a notification hold;
  * a new release's breach groups into an incident of its own release.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from boss_engine.services import agent_decision as ad
from boss_engine.services import app_alerts as aa
from boss_engine.services import notify


def test_the_question_reads_as_a_question_built_from_labels():
    cands = [ad.Candidate("split", "It is a different problem from the other alerts in its "
                                   "incident: open a new incident for it alone",
                          label="Open its own incident"),
             ad.Candidate("page_now", "It needs a person now: notify at once",
                          label="Page someone now"),
             ad.Candidate("ask", "Ask the person", acts=False, label="Ask a person")]
    q = ad._question_for(cands, "split", "")
    assert q == ("I'm not sure enough to act alone. Should I open its own incident, or page "
                 "someone now?")
    assert "It is a different problem" not in q
    # an unlabelled candidate still reads as a verb phrase: the part after its reason
    q2 = ad._question_for([ad.Candidate("a", "It is noise: mark it noise"),
                           ad.Candidate("b", "It is real: open an incident")], "a", "")
    assert q2.endswith("Should I mark it noise, or open an incident?")


def test_every_triage_candidate_has_a_button_label():
    ctx = {"alerts_in_its_group": [{"id": "x"}], "open_incidents": [
        {"id": "inc_other", "title": "Order API: p95 latency"}]}
    cands = aa._candidates({"incident_id": "inc_1"}, ctx)
    assert all(c.label for c in cands), [c.key for c in cands if not c.label]
    opts = aa.triage_options(cands)
    assert {o["action"] for o in opts} >= {"page_now", "join_incident", "split", "noise",
                                           "join:inc_other"}
    assert not any(o["action"] in ("ask", f"hold:{aa.HOLD_MINUTES}") for o in opts)


@pytest.fixture()
def triage_env(monkeypatch):
    """triage_alert with its database edges faked: what it writes and starts is captured."""
    state = {"alert": {"id": "alrt_1", "project_id": "p1", "state": "firing",
                       "alertname": "AppErrorRate", "target_id": "t1", "subject_id": None,
                       "incident_id": "inc_old", "rule_id": "r1", "triage_state": "done",
                       "triage": {"outcome": "asked", "reason": "84s after release 1.2",
                                  "question": "Should I open its own incident?",
                                  "confidence": 0.62,
                                  "by": {"role_key": "sre", "role": "Site Reliability Engineer"},
                                  "options": [{"action": "split",
                                               "label": "Open its own incident"},
                                              {"action": "page_now",
                                               "label": "Page someone now"}]}},
             "written": {}, "responded": [], "split": []}

    class Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(scalar=lambda: "Seed Owner",
                                   mappings=lambda: SimpleNamespace(first=lambda: None))

        def begin_nested(self):
            return SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    class Begin:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    eng = SimpleNamespace(connect=Begin)
    import boss_engine.db as db
    monkeypatch.setattr(db, "begin", lambda e: Begin())
    monkeypatch.setattr(aa, "_alert", lambda c, i: dict(state["alert"]))
    monkeypatch.setattr(aa, "triage_context", lambda c, a: {
        "alerts_in_its_group": [{"id": "alrt_p95"}], "open_incidents": []})
    monkeypatch.setattr(aa, "subject", lambda c, i: {"app_name": "Order API",
                                                       "environment": "staging"})
    monkeypatch.setattr(aa, "on_call_agent", lambda c, p: {"role_key": "sre",
                                                            "role": "Site Reliability Engineer"})
    monkeypatch.setattr(aa, "_lock", lambda c, i: None)
    monkeypatch.setattr(aa, "_note", lambda *a, **k: None)
    monkeypatch.setattr(aa, "_write_triage", lambda c, i, rec, st: state["written"].update(rec))
    monkeypatch.setattr(aa, "_new_incident",
                        lambda c, t, a, at, split=False: state["split"].append(a["id"]) or "inc_new")
    monkeypatch.setattr(aa, "_respond",
                        lambda c, a, inc, at: state["responded"].append(inc) or {
                            "run_id": "wfr_1", "word": "Responding"})
    from boss_engine.services import incident_response as ir
    monkeypatch.setattr(ir, "_person", lambda c, p: "Seed Owner")
    return eng, state


def test_a_persons_answer_runs_the_agents_path_and_starts_the_response(triage_env):
    eng, state = triage_env
    rec = aa.answer_triage(eng, SimpleNamespace(), "alrt_1", action="split", actor="usr_1")
    assert rec["outcome"] == "split" and rec["incident_id"] == "inc_new"
    assert state["split"] == ["alrt_1"] and state["responded"] == ["inc_new"]
    assert rec["response"] == {"run_id": "wfr_1", "word": "Responding"}
    w = state["written"]
    assert w["by"]["backend"] == "person" and w["answered"]["action"] == "split"
    assert w["answered"]["name"] == "Seed Owner" and w["answered"]["at"]
    # the agent's question stays beside the answer: who asked, what it said, how sure it was
    assert w["asked"]["by"]["role_key"] == "sre" and w["asked"]["confidence"] == 0.62
    assert w["asked"]["options"][0]["label"] == "Open its own incident"


def test_an_answer_the_agent_did_not_offer_is_refused(triage_env):
    eng, state = triage_env
    with pytest.raises(aa.AnswerRefused, match="not one of the options"):
        aa.answer_triage(eng, SimpleNamespace(), "alrt_1", action="noise", actor="usr_1")
    state["alert"]["triage"] = {"outcome": "opened"}
    with pytest.raises(aa.AnswerRefused, match="not asking"):
        aa.answer_triage(eng, SimpleNamespace(), "alrt_1", action="split", actor="usr_1")
    state["alert"]["state"] = "resolved"
    with pytest.raises(aa.AnswerRefused, match="no longer firing"):
        aa.answer_triage(eng, SimpleNamespace(), "alrt_1", action="split", actor="usr_1")


def test_an_agent_that_asks_ends_the_hold_and_the_message_carries_its_question():
    now = datetime.now(timezone.utc)
    held = {"suppressed_by": [{"mode": "hold", "until": (now + timedelta(minutes=10)).isoformat()}]}
    assert notify._held({**held, "triage": {}}, now)
    assert notify._held({**held, "triage": {"outcome": "hold"}}, now)
    for outcome in ("asked", "page", "opened", "joined", "split"):
        assert notify._held({**held, "triage": {"outcome": outcome}}, now) is None, outcome
    line = notify._triage_line({"triage_state": "done", "triage": {
        "outcome": "asked", "word": "Asks you", "reason": "84s after release 1.2.",
        "question": "Should I open its own incident, or page someone now?",
        "options": [{"action": "split", "label": "Open its own incident"},
                    {"action": "page_now", "label": "Page someone now"}]}})
    assert "The agent asks: Should I open its own incident" in line
    assert "Answer in BOSS: Open its own incident · Page someone now" in line


def test_a_new_releases_breach_opens_its_own_incident(monkeypatch):
    open_inc = {"id": "inc_old", "status": "diagnosing", "title": "p95",
                "finalize_ctx": {"deploy": {"commit_sha": "e30c32812d6b0de0"}}}
    monkeypatch.setattr(aa, "_incident_row", lambda c, p, k: open_inc if k == "K" else None)
    alert = {"release": {"commit_sha": "87a5701d55e589bfbc11"}}
    assert aa._release_key(None, "p1", "K", alert) == "K:release:87a5701d55e5"
    # same release, no release, or a closed incident: the group's key, as before
    assert aa._release_key(None, "p1", "K", {"release": {"commit_sha": "e30c32812d6b0de0aa"}}) == "K"
    assert aa._release_key(None, "p1", "K", {"release": None}) == "K"
    open_inc["status"] = "resolved"
    assert aa._release_key(None, "p1", "K", alert) == "K"
