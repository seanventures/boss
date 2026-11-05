"""An incident's response says WHO did each part, what that agent is for, and what it found.

D2 operations video: the card showed the response as coloured dots, and the report as "Step
investigate succeeded". The operator could not say which agent triaged the alert, who found the
cause and from what evidence, who wrote the fix and who reviewed it, who approved, or who
verified. These pin the pieces that answer it:

  * the regression test is proven RED on the faulty code before the fix (red_first), not taken
    on trust from a criterion that says it is;
  * a triage records which agent decided, apart from the model it thought with;
  * the card reads only THIS occurrence of a reused incident;
  * every step of a finished response names its agent, person or BOSS (the end-to-end test in
    test_an_app_incident_is_responded_to asserts that on a real run).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import incident_response as ir
from boss_engine.services import incident_responders as rs
from boss_engine.services import red_first
from boss_engine.services.workflow_capability_runner import INCIDENT_FIX_CRITERIA

UNITTEST_RED = """test_create_order (tests.test_orders_create.CreateOrder.test_create_order) ... ok
test_three_lines_ship (tests.test_shipping_fee.ShippingFee.test_three_lines_ship) ... ERROR

======================================================================
ERROR: test_three_lines_ship (tests.test_shipping_fee.ShippingFee.test_three_lines_ship)
----------------------------------------------------------------------
KeyError: 3
FAILED (errors=1)
"""


def test_an_incident_fix_brief_demands_red_first_and_a_feature_brief_does_not():
    assert red_first.demanded(INCIDENT_FIX_CRITERIA)
    assert red_first.demanded(["The new test fails before the fix and passes after it."])
    assert not red_first.demanded(["Add a CSV export button.", "Tests pass."])


def test_red_first_names_the_new_tests_that_fail_on_the_faulty_code():
    got = red_first.record(SimpleNamespace(stdout=UNITTEST_RED, stderr="", exit_code=1,
                                           timed_out=False),
                           ["order-api/tests/test_shipping_fee.py"])
    assert got["failed"] and got["reproduces"]
    assert got["failing"] == ["test_three_lines_ship (tests.test_shipping_fee.ShippingFee."
                              "test_three_lines_ship)"]
    assert got["word"].startswith("Fails on the faulty code")


def test_a_new_test_that_passes_on_the_faulty_code_is_said_not_to_reproduce_it():
    got = red_first.record(SimpleNamespace(stdout="OK", stderr="", exit_code=0, timed_out=False),
                           ["order-api/tests/test_shipping_fee.py"])
    assert not got["failed"] and not got["reproduces"]
    assert "do not reproduce" in got["word"]
    # and a timeout proves nothing either way
    slow = red_first.record(SimpleNamespace(stdout="", stderr="", exit_code=124, timed_out=True),
                            ["t.py"])
    assert not slow["failed"] and "timed out" in slow["word"]


def test_the_card_reads_a_reviewers_summary_as_one_plain_sentence():
    s = ("REVIEWED: order-api/app/pricing.py\n\n**Analysis**\n\n`pricing.py`: The fix only "
         "changes `get_bulk_price(sku)` handling. Nothing else moves.")
    assert rs.first_sentence(s) == "pricing.py: The fix only changes get_bulk_price(sku) handling."


def test_a_numbered_security_finding_is_not_cut_to_the_list_marker():
    """BOSSAPP-618: the report's security summary was cut at the "1." of a numbered finding."""
    summary = "1. FINDING: the token is logged in plain text. The key is also in the URL."
    assert rs.first_sentence(summary) == "1. FINDING: the token is logged in plain text."
    word, verdict = rs._stage_finding("security_check", {
        "security_summary": summary, "security_verdict": "fail"})
    assert word == "1. FINDING: the token is logged in plain text." and verdict == "fail"


def test_the_build_crew_says_red_on_the_faulty_code_and_green_with_the_fix():
    state = {"test_files": ["order-api/tests/test_shipping_fee.py"], "tests_passed": True,
             "qa_summary": "All 42 tests pass. More words.",
             "red_first": {"ran": True, "failed": True, "failing": ["test_three_lines_ship"],
                           "word": "Fails on the faulty code: 1 new test(s) red"}}
    rg = rs.red_green(state)
    assert rg["red"]["failed"] and rg["green"] is True and rg["suite"] == "All 42 tests pass."
    word, verdict = rs._stage_finding("write_tests", state)
    assert word == "Wrote test_shipping_fee.py; it fails on the faulty release" and verdict == "red"


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _what(connection, incident_id: str) -> str:
    return ir._timeline(connection, {"id": incident_id, "finalize_ctx": {}}, None)[0]["what"]


def test_the_timeline_names_the_threshold_on_every_stored_alert_shape(pg):
    """BOSSAPP-618: a joined firing spreads the rule's percent onto the payload, and a split
    keeps it as `threshold`. The timeline read only a nested `rule.error_rate_percent` and
    printed "threshold None%"."""
    eng, pid = pg
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def one(payload: dict) -> str:
        inc = f"inc_{uuid4().hex[:10]}"
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO incidents (id, project_id, dedupe_key, title, status, first_seen, "
                " last_seen) VALUES (:i, :p, :k, 'Order API', 'open', :t, :t)"),
                {"i": inc, "p": pid, "k": f"k_{inc}", "t": now})
            ir._evidence(c, inc, "alert", ref="AppErrorRate", payload=payload, at=now)
            return _what(c, inc)

    nested = one({"fired_at": now.isoformat(), "error_rate": 0.144, "requests": 220,
                  "rule": {"error_rate_percent": 5.0, "kind": "error_rate", "alert": "AppErrorRate"}})
    assert nested == "Alert fired: 14.4% of 220 requests failing (threshold 5.0%)"
    # Joined and settling firings spread the rule at the top and keep `value` as the percent.
    # They do not nest `rule` and they do not repeat error_rate or requests.
    joined = one({"fired_at": now.isoformat(), "joined": True, "error_rate_percent": 5.0,
                  "kind": "error_rate", "alert": "AppErrorRate", "value": 14.4})
    assert "threshold 5" in joined and "None" not in joined and "14.4%" in joined
    split = one({"fired_at": now.isoformat(), "value": 14.4, "threshold": 5.0,
                 "rule": {"alert": "AppErrorRate", "kind": "error_rate"}})
    assert "threshold 5" in split and "None" not in split and "14.4%" in split


def test_a_recovered_check_replaces_the_waiting_note(pg):
    """BOSSAPP-617: verify-recovery kept the note it wrote when it started waiting, so a
    succeeded step still said "Waiting for enough traffic"."""
    from boss_engine.services import workflow_capability_runner as runner

    eng, pid = pg
    now = datetime.now(timezone.utc).replace(microsecond=0)
    inc, chk = f"inc_{uuid4().hex[:10]}", f"rcv_{uuid4().hex[:8]}"
    summary = "Recovered: 0.4% of 260 requests failed over the last 10 min"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, status, first_seen, "
            " last_seen) VALUES (:i, :p, :k, 'Order API', 'resolved', :t, :t)"),
            {"i": inc, "p": pid, "k": f"k_{inc}", "t": now})
        c.execute(text(
            "INSERT INTO incident_recovery_checks (id, project_id, incident_id, target_id, "
            " since, deadline, status, summary) VALUES (:id, :p, :i, 'deptgt_x', :t, :t, "
            " 'recovered', :s)"),
            {"id": chk, "p": pid, "i": inc, "t": now, "s": summary})
        evidence = runner._terminal_evidence(c, "recovery_check", chk)
    assert evidence.get("note") == summary
    assert evidence.get("recovery_summary") == summary
    finished = {"status": "succeeded", "note": "Waiting for enough traffic on the new release",
                **evidence}
    assert finished["note"] == summary


def test_a_reused_incident_shows_only_this_occurrence(pg):
    """The 18:43 re-fire of Order API's error-rate incident showed the morning's ten HelpDesk
    reports and its root cause as if they were this occurrence's."""
    eng, pid = pg
    now = datetime.now(timezone.utc).replace(microsecond=0)
    earlier, occurrence = now - timedelta(hours=12), now - timedelta(minutes=5)
    inc = f"inc_{uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, status, first_seen, "
            " last_seen, finalize_ctx) VALUES (:i, :p, :k, 'Order API: errors', 'diagnosing', "
            " :o, :o, CAST(:x AS JSONB))"),
            {"i": inc, "p": pid, "k": f"k_{inc}", "o": occurrence,
             "x": json.dumps({"occurrence": occurrence.isoformat()})})
        ir._evidence(c, inc, "rca_hypothesis", payload={"root_cause": "the morning's"},
                     at=earlier)
        ir._evidence(c, inc, "rca_hypothesis", payload={"root_cause": "this one's"},
                     at=occurrence + timedelta(minutes=1))
        for at, sig in ((earlier, "old"), (occurrence + timedelta(seconds=5), "new")):
            c.execute(text(
                "INSERT INTO app_signal_receipts (id, project_id, reporter_target_id, signal_id, "
                " via, incident_id, payload, received_at) VALUES (:id, :p, 'deptgt_hd', :s, "
                " 'push', :i, CAST(:x AS JSONB), :at)"),
                {"id": f"rcpt_{uuid4().hex[:8]}", "p": pid, "s": f"{sig}_{inc}", "i": inc, "at": at,
                 "x": json.dumps({"count": 1, "refs": [sig]})})
        view = ir.response_view(c, pid, inc)
    assert [e["payload"]["root_cause"] for e in view["evidence"]] == ["this one's"]
    assert [r["refs"] for r in view["reports"]] == [["new"]]
    assert view["responders"] == []          # no triage, no response yet: nobody to name


def test_a_triage_names_the_on_call_agent_apart_from_the_model(pg, monkeypatch):
    from boss_engine.services import agent_decision as ad
    from boss_engine.services import app_alerts as aa

    eng, pid = pg
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name) VALUES "
            " (:i, :p, 'sre', 'Site Reliability Engineer') ON CONFLICT DO NOTHING"),
            {"i": f"role_{uuid4().hex[:8]}", "p": pid})
        assert aa.on_call_agent(c, pid) == {"role_key": "sre",
                                            "role": "Site Reliability Engineer"}
        assert aa.on_call_agent(c, "p_nobody") is None
    seen: dict = {}

    def fake_decide(connection, settings, project_id, **kw):
        seen["agent"] = kw["agent"]
        return ad.AgentDecision(ad.DECIDED, action="noise", confidence=0.9,
                                reason="A restart blip; nothing is failing now.",
                                by={"model": "m-1", "backend": "model"})

    monkeypatch.setattr(ad, "decide", fake_decide)
    alert = {"id": "alrt_x", "project_id": pid, "state": "firing", "alertname": "OrdersFailing",
             "target_id": "deptgt_x", "subject_id": None, "incident_id": None}
    monkeypatch.setattr(aa, "_alert", lambda c, i: dict(alert))
    monkeypatch.setattr(aa, "triage_context", lambda c, a: {
        "alerts_in_its_group": [], "open_incidents": []})
    monkeypatch.setattr(aa, "subject", lambda c, i: {"app_name": "Order API",
                                                       "environment": "staging"})
    monkeypatch.setattr(aa, "_lock", lambda c, i: None)
    written: dict = {}
    monkeypatch.setattr(aa, "_write_triage", lambda c, i, rec, st: written.update(rec))
    monkeypatch.setattr(aa, "_retire_if_noise", lambda *a, **k: None)
    aa.triage_alert(eng, SimpleNamespace(), "alrt_x")
    assert seen["agent"].startswith("the Site Reliability Engineer")
    assert written["by"] == {"model": "m-1", "backend": "model", "role_key": "sre",
                             "role": "Site Reliability Engineer"}
    from boss_engine.services.app_alerts_view import _triage_wire

    wire = _triage_wire({"triage_state": "done", "triage": written})
    assert wire["agent"] == {"role_key": "sre", "role": "Site Reliability Engineer"}
    assert wire["model"] == "m-1" and wire["reason"].startswith("A restart blip")


def test_an_agents_work_list_names_the_person_who_handed_it_the_work(pg):
    """Beacon's page listed "usr_wsw5h4jk…" beside every step a person's run handed it."""
    from boss_engine.services import agent_acts

    eng, _pid = pg
    uid = f"usr_{uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:i, 'org_obs', 'user', 'active', 'Seed Owner')"), {"i": uid})
        assert agent_acts._person_names(c, {uid, "usr_nobody"}) == {uid: "Seed Owner"}
        assert agent_acts._person_names(c, set()) == {}
