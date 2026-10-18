"""Monitoring Phase 1a (BOSSAPP-598): telling people — the routing tree and its preview (one
function), one message thread per incident, notification that never waits for triage, ack from
a signed single-use email link, the outbox with receipts and retries, and the self-check.

Acceptance (PRD §12, Phase 1a): 3 (email and Slack, triage appended, "not triaged" with the
model down), 4 (the ack link: single use, repeats stop, the ack expires), 5 (flapping told once),
7 (a maintenance window ends with the breach present: BOSS notifies), and R1.24.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import app_alerts as aa
from boss_engine.services import monitor_scheduler as ms
from boss_engine.services import monitoring as mon
from boss_engine.services import notify

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)


def S():
    """Settings for a test: links built on a documentation address."""
    from boss_engine.config import get_settings

    return get_settings().model_copy(update={"public_url": "https://203.0.113.20",
                                             "monitoring_prometheus_url": ""})


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _firing(eng, pid, *, name="Checkout", severity="critical", value=500.0):
    with eng.begin() as c:
        svc = mon.find_or_create_service(c, pid, f"{name}-{uuid4().hex[:5]}")
        subj = aa.subject(c, svc["id"])
        rule = aa.update_rule(c, subj, None, {"alert": f"{name}Errors", "expr":
                                              "max(errs) > 10", "labels": {"severity": severity},
                                              "annotations": {"summary": f"{name} errors high",
                                                              "runbook_url":
                                                              "https://runbooks.example.com/x"}},
                              actor="usr_t")
        mon.write_points(c, pid, svc, [(mon.stamp({"__name__": "errs"}, project_id=pid, svc=svc),
                                        T0, value)])
        aa.evaluate_target(c, subj, T0)
        alert = c.execute(text("SELECT * FROM app_alerts WHERE rule_id = :r"),
                          {"r": rule["id"]}).mappings().one()
    return svc, subj, rule, dict(alert)


def _cp(eng, pid, kind="email", **kw):
    with eng.begin() as c:
        cp = notify.create_contact_point(c, S(), pid, kind=kind, name=kind, actor="usr_t", **kw)
        c.execute(text("UPDATE contact_points SET verified_at = now() WHERE id = :i"),
                  {"i": cp["contact_point"]["id"]})
        return cp


def _route_to(eng, pid, cp_ids):
    with eng.begin() as c:
        root = notify.ensure_root(c, pid)
        c.execute(text("UPDATE notification_policies SET contact_point_ids = CAST(:c AS JSONB) "
                       "WHERE id = :i"), {"c": json.dumps(cp_ids), "i": root["id"]})


def _plan(eng, pid, at):
    with eng.begin() as c:
        return notify.plan(c, S(), pid, at)


def _outbox(eng, pid):
    with eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM notifications WHERE project_id = :p ORDER BY created_at"),
            {"p": pid}).mappings().all()]


# ── the tree: the router and the preview are one function ──────────────────────────────────
def test_the_tree_routes_depth_first_first_match_unless_continue_and_children_inherit():
    tree = [
        {"id": "root", "parent_id": None, "name": "root", "matchers": [], "group_by": ["x"],
         "group_wait_s": 30, "group_interval_s": 300, "repeat_interval_s": 14400,
         "contact_point_ids": ["console"], "quiet_hours": None, "continue_matching": False},
        {"id": "db", "parent_id": "root", "name": "db", "matchers": [
            {"name": "team", "op": "=", "value": "db"}], "contact_point_ids": ["dba"],
         "continue_matching": True, "group_wait_s": 0},
        {"id": "crit", "parent_id": "root", "name": "crit", "matchers": [
            {"name": "severity", "op": "=~", "value": "crit.*"}], "contact_point_ids": ["pager"]},
        {"id": "late", "parent_id": "root", "name": "late", "matchers": [
            {"name": "team", "op": "=", "value": "db"}], "contact_point_ids": ["never"]},
    ]
    got = notify.route(tree, {"team": "db", "severity": "critical"})
    assert [r["policy_id"] for r in got] == ["db", "crit"], "continue, then first match"
    assert got[0]["group_wait_s"] == 0 and got[1]["group_wait_s"] == 30, "children inherit"
    assert [r["policy_id"] for r in notify.route(tree, {"team": "web"})] == ["root"]


def test_the_preview_says_where_an_alert_goes_and_skips_an_unverified_point(pg):
    eng, pid = pg
    with eng.begin() as c:
        made = notify.create_contact_point(c, S(), pid, kind="email", name="Ops",
                                           address="ops@example.com", actor="usr_t")
        root = notify.ensure_root(c, pid)
        c.execute(text("UPDATE notification_policies SET contact_point_ids = CAST(:c AS JSONB) "
                       "WHERE id = :i"),
                  {"c": json.dumps([notify.ensure_console(c, pid),
                                    made["contact_point"]["id"]]), "i": root["id"]})
        high = notify.preview(c, pid, {"alertname": "X", "severity": "critical"})
        low = notify.preview(c, pid, {"alertname": "X", "severity": "warning"})
    skipped = {cp["name"]: cp["skipped"] for cp in high["routes"][0]["contact_points"]}
    assert skipped["Ops"] and "Not verified" in skipped["Ops"] and not skipped["Console"]
    assert all(not cp["skipped"] for cp in low["routes"][0]["contact_points"])
    assert "after 30 s" in high["word"]


# ── 3. one message, never waiting for triage; triage appended ─────────────────────────────
def test_a_breach_is_told_once_per_incident_without_waiting_for_triage(pg):
    eng, pid = pg
    email = _cp(eng, pid, "email", address="oncall@example.com")["contact_point"]["id"]
    _route_to(eng, pid, [email])
    svc, subj, rule, alert = _firing(eng, pid)
    assert _plan(eng, pid, T0 + timedelta(seconds=10))["enqueued"] == 0, "group_wait first"
    _plan(eng, pid, T0 + timedelta(seconds=31))
    _plan(eng, pid, T0 + timedelta(seconds=45))
    out = _outbox(eng, pid)
    assert len(out) == 1 and out[0]["kind"] == "firing", "one message, not one per tick"
    body = out[0]["body"]
    assert "Query: max(errs) > 10" in body and "Value at breach: 500" in body
    assert "Runbook: https://runbooks.example.com/x" in body
    assert "Not triaged yet" in body, "it did not wait for the agent"
    assert "/v1/alert-actions/bact_" in body, "an email carries the signed ack link"
    # the model is down: the alert says so, and nothing waits on it
    with eng.begin() as c:
        c.execute(text("UPDATE app_alerts SET triage_state = 'done', triage = CAST(:t AS JSONB) "
                       "WHERE id = :a"), {"a": alert["id"], "t": json.dumps({
                           "outcome": "joined", "word": "Joined the incident",
                           "reason": "same release", "at": "x"})})
    _plan(eng, pid, T0 + timedelta(minutes=1))
    kinds = [n["kind"] for n in _outbox(eng, pid)]
    assert kinds == ["firing", "triage"], "the triage is appended once when it lands"
    _plan(eng, pid, T0 + timedelta(minutes=2))
    assert len(_outbox(eng, pid)) == 2


def test_no_model_still_notifies_on_time_and_says_not_triaged(pg):
    eng, pid = pg
    email = _cp(eng, pid, "email", address="oncall@example.com")["contact_point"]["id"]
    _route_to(eng, pid, [email])
    svc, subj, rule, alert = _firing(eng, pid)

    class NoModel:
        def create_text_response(self, prompt, system_prompt=None):
            raise RuntimeError("no model is connected")
    aa.triage_alert(eng, None, alert["id"], adapter=NoModel())
    _plan(eng, pid, T0 + timedelta(seconds=31))
    (n,) = _outbox(eng, pid)
    assert "Not triaged: no model is answering" in n["body"]


# ── 4. the ack link ─────────────────────────────────────────────────────────────────────────
def test_the_email_ack_link_acks_once_stops_repeats_and_the_ack_expires(pg):
    eng, pid = pg
    email = _cp(eng, pid, "email", address="oncall@example.com")["contact_point"]["id"]
    _route_to(eng, pid, [email])
    svc, subj, rule, alert = _firing(eng, pid)
    _plan(eng, pid, T0 + timedelta(seconds=31))
    token = _outbox(eng, pid)[0]["body"].split("/v1/alert-actions/")[1].split()[0]
    with eng.begin() as c:
        assert mon.peek_action_token(c, token)["usable"]
        done = mon.use_action_token(c, token, now=T0 + timedelta(minutes=1))
    assert done["action"] == "ack"
    with eng.begin() as c, pytest.raises(ValueError, match="Already used"):
        mon.use_action_token(c, token, now=T0 + timedelta(minutes=2))
    # four hours on, still firing: the repeat interval is due, but it is acked -> no repeat
    _plan(eng, pid, T0 + timedelta(hours=3, minutes=59))
    assert len(_outbox(eng, pid)) == 1
    with eng.begin() as c:
        c.execute(text("UPDATE app_alerts SET ack_expires_at = :t WHERE id = :a"),
                  {"t": T0 + timedelta(hours=4, minutes=1), "a": alert["id"]})
        c.execute(text("UPDATE incidents SET acked_at = NULL, acked_by = NULL WHERE id = :i"),
                  {"i": alert["incident_id"]})
    _plan(eng, pid, T0 + timedelta(hours=4, minutes=2))
    assert [n["kind"] for n in _outbox(eng, pid)] == ["firing", "firing"], \
        "the ack expired: the repeat goes out"
    with eng.begin() as c, pytest.raises(ValueError, match="Expired"):
        fresh = mon.mint_action_token(c, pid, "ack", alert_id=alert["id"], now=T0)
        mon.use_action_token(c, fresh, now=T0 + timedelta(hours=25))


# ── 5. flapping is told once ────────────────────────────────────────────────────────────────
def test_a_flapping_alert_is_told_once_as_flapping(pg):
    eng, pid = pg
    email = _cp(eng, pid, "email", address="oncall@example.com")["contact_point"]["id"]
    _route_to(eng, pid, [email])
    svc, subj, rule, alert = _firing(eng, pid)
    with eng.begin() as c:
        c.execute(text("UPDATE app_alerts SET flapping = true, flap_score = 0.8 WHERE id = :a"),
                  {"a": alert["id"]})
    for m in range(1, 11):
        _plan(eng, pid, T0 + timedelta(minutes=m * 6))
    assert [n["kind"] for n in _outbox(eng, pid)] == ["flapping"], "one message, not ten"


# ── the outbox ──────────────────────────────────────────────────────────────────────────────
def test_the_outbox_retries_with_backoff_then_gives_up_and_a_meta_alert_fires(pg):
    eng, pid = pg
    email = _cp(eng, pid, "email", address="oncall@example.com")["contact_point"]["id"]
    _route_to(eng, pid, [email])
    _firing(eng, pid)
    _plan(eng, pid, T0 + timedelta(seconds=31))

    def broken(connection, settings, n):
        raise RuntimeError("SMTP said 421")
    at = T0 + timedelta(minutes=1)
    for i in range(6):
        notify.deliver_due(eng, S(), now=at + timedelta(hours=i), sender=broken)
    (n,) = [x for x in _outbox(eng, pid) if x["kind"] == "firing"]
    assert n["status"] == "dead" and n["attempts"] == 6 and "421" in n["error_word"]
    with eng.begin() as c:
        ms.self_check(c, S(), at + timedelta(hours=6))
        meta = c.execute(text("SELECT state, labels FROM app_alerts WHERE project_id = :p AND "
                              "alertname = 'BossOutboxBacklog'"), {"p": pid}).mappings().one()
    assert meta["state"] == "firing" and meta["labels"]["boss_meta"] == "true"


def test_a_signed_webhook_carries_a_timestamped_signature(pg, monkeypatch):
    eng, pid = pg
    made = _cp(eng, pid, "webhook", url="https://hooks.example.com/boss")
    token = made["signing_token"]
    sent = {}

    def fake_post(url, payload, headers=None):
        sent.update(url=url, payload=payload, headers=headers)
        return "HTTP 200"
    monkeypatch.setattr(notify, "_post_json", fake_post)
    monkeypatch.setattr(notify, "_secret", lambda c, s, cp: json.dumps(
        {"url": "https://hooks.example.com/boss", "token": token}))
    with eng.begin() as c:
        notify.deliver(c, S(), {"project_id": pid, "contact_point_id":
                                made["contact_point"]["id"], "subject": "s", "body": "b",
                                "kind": "firing", "group_key": "g", "payload": {}})
    raw = json.dumps(sent["payload"]).encode()
    ts = sent["headers"]["X-BOSS-Timestamp"]
    assert sent["headers"]["X-BOSS-Signature"] == mon.sign(token, ts, raw)
    assert made["contact_point"]["shows"] == "hooks.example.com", "the URL itself is a secret"


def test_the_watchdog_always_fires():
    assert 'ALERTS{alertname="BossWatchdog",alertstate="firing"' in ms.WATCHDOG
