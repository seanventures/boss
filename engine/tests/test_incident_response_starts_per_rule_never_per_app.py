"""Incident Response starts per alert rule, never per app (BOSSAPP-598).

The incident card's app-wide "Respond automatically" was an `incident.opened` workflow
subscription: one switch answered every alert of every app the same way. It is retired in
favour of each rule's own Respond automatically on the app's Alerts tab:

  * no door but an alert rule's own Respond automatically can start Incident Response
    unattended — not a subscription (however it got into the table), a schedule's door, or any
    other — and the refusal is in the one function every unattended start goes through;
  * a subscription that would start it is refused when it is set up or re-enabled;
  * an existing one is migrated without losing what the person chose: the app's rules that fed
    its incidents (every serving rule; at least error rate) respond automatically, on the same
    person's grant, audited; then it is disabled and its row kept;
  * the incident card's view says which rules respond, instead of an app-wide switch.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import app_alerts as aa
from boss_engine.services import event_dispatch
from boss_engine.services import event_subscription_service as subs
from boss_engine.services import incident_response as ir
from .test_app_alerts import _app, _fired, pg  # noqa: F401 — the fixture

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"


def _legacy_subscription(c, pid: str, *, on_behalf_of: str | None = "usr_param_t",
                         grant_id: str | None = "cgr_t") -> str:
    """The row the incident card's old switch wrote (inserted as it stands in an older DB)."""
    sid = f"sub_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO event_subscriptions (id, project_id, event_type, sink_kind, sink_ref, "
        " enabled, on_behalf_of, grant_id) VALUES (:i, :p, 'incident.opened', 'workflow', "
        " 'incident_response', true, :o, :g)"),
        {"i": sid, "p": pid, "o": on_behalf_of, "g": grant_id})
    event_dispatch.invalidate_subscription_cache(pid)
    return sid


def _runs(c, pid: str) -> int:
    return int(c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                         {"p": pid}).scalar())


# ── only a rule's own Respond automatically can start it ─────────────────────────────────────
@pytest.mark.parametrize("door", ["subscription", "schedule", "workflow_webhook_trigger",
                                  "app", "project", ""])
def test_no_door_but_an_alert_rule_starts_incident_response(pg, door):  # noqa: F811
    eng, pid = pg
    with eng.begin() as c:
        before = _runs(c, pid)
        got = event_dispatch.start_workflow_on_grant(
            c, project_id=pid, workflow_key="incident_response", link=None, actor="t",
            input_data={"incident_id": "inc_x"}, dedupe_key=f"t:{uuid4().hex}", door=door,
            door_id="d", door_name="d")
        assert got["run_id"] is None and got["entry"]["reason"] == "per_rule_only"
        assert got["word"] == event_dispatch.PER_RULE_WORD
        assert _runs(c, pid) == before


def test_the_alert_rule_door_is_not_refused_as_app_wide(pg):  # noqa: F811
    eng, pid = pg
    with eng.begin() as c:
        got = event_dispatch.start_workflow_on_grant(
            c, project_id=pid, workflow_key="incident_response", link=None, actor="monitor",
            input_data={"incident_id": "inc_x"}, dedupe_key=f"t:{uuid4().hex}",
            door="alert_rule", door_id="r", door_name="r")
    # (nothing published in this bare project: refused for THAT, not for being app-wide)
    assert got["word"] != event_dispatch.PER_RULE_WORD


def test_an_app_wide_subscription_left_in_the_table_starts_nothing(pg):  # noqa: F811
    eng, pid = pg
    with eng.begin() as c:
        _legacy_subscription(c, pid)
        before = _runs(c, pid)
        out = event_dispatch.dispatch_event(
            c, event_id=f"evt_{uuid4().hex[:8]}", project_id=pid, event_type="incident.opened",
            actor="monitor", entity_type="incident", entity_id="inc_x",
            data={"incident_id": "inc_x"})
        assert _runs(c, pid) == before
    assert out and all(e.get("refused") and e.get("reason") == "per_rule_only" for e in out), out


def test_only_the_alert_rule_names_the_rule_door():
    """The guard trusts the door's name, so only an alert rule's own response may use it."""
    users = sorted(str(p.relative_to(ENGINE)) for p in ENGINE.rglob("*.py")
                   if re.search(r"door\s*=\s*(\"alert_rule\"|PER_RULE_DOOR)", p.read_text()))
    assert users == ["services/app_alerts.py"], users
    body = (ENGINE / "services/app_alerts.py").read_text()
    assert body.count('door="alert_rule"') == 1 and "rule[\"auto_respond\"]" in body


def test_a_subscription_that_would_start_it_is_refused_at_setup(pg):  # noqa: F811
    eng, pid = pg
    with eng.begin() as c, pytest.raises(ValueError, match="per alert rule"):
        subs.create_event_subscription(c, pid, "incident.opened", "workflow",
                                       "incident_response", actor="t", authorize=False)
    with eng.begin() as c, pytest.raises(ValueError, match="per alert rule"):
        subs.create_event_subscription(c, pid, "incident.*", "workflow", "incident_response",
                                       actor="t", authorize=False)
    with eng.begin() as c, pytest.raises(ValueError, match="per alert rule"):
        subs.ensure_event_subscription(c, pid, "incident.opened", "workflow",
                                       "incident_response")
    # any other workflow may still follow incidents
    with eng.begin() as c:
        ok = subs.create_event_subscription(c, pid, "incident.opened", "workflow",
                                            "tell_the_team", actor="t", authorize=False)
    # ...but not be re-pointed at Incident Response, and a retired one is not re-enabled
    with eng.begin() as c, pytest.raises(ValueError, match="per alert rule"):
        subs.update_event_subscription(c, pid, ok["id"], sink_ref="incident_response")
    with eng.begin() as c:
        sid = _legacy_subscription(c, pid)
        c.execute(text("UPDATE event_subscriptions SET enabled = false WHERE id = :i"),
                  {"i": sid})
    with eng.begin() as c, pytest.raises(ValueError, match="per alert rule"):
        subs.update_event_subscription(c, pid, sid, enabled=True)
    with eng.begin() as c:   # turning it off, or anything else on an off row, still works
        assert subs.update_event_subscription(c, pid, sid, enabled=False) is not None


# ── the migration keeps what the person chose ────────────────────────────────────────────────
def test_the_app_wide_switch_moves_onto_the_rules_that_fed_its_incident(pg):  # noqa: F811
    eng, pid = pg
    t, alert = _fired(eng, pid)          # the Order API's error-rate alert opened an incident
    with eng.begin() as c:
        quiet = _app(c, pid, name="Quiet app")      # never had an incident: left alone
        aa.ensure_rules(c, quiet)
        sid = _legacy_subscription(c, pid)
        got = aa.retire_app_wide_response(c)
    mine = next(g for g in got if g["subscription_id"] == sid)
    assert mine["on_behalf_of"] == "usr_param_t"
    with eng.begin() as c:
        rules = {r["kind"]: r for r in aa.rules(c, t["id"])}
        others = aa.rules(c, quiet["id"])
        sub = dict(c.execute(text("SELECT * FROM event_subscriptions WHERE id = :i"),
                             {"i": sid}).mappings().one())
        audit = [dict(r) for r in c.execute(text(
            "SELECT * FROM app_alert_rule_changes WHERE target_id = :t ORDER BY rule_id"),
            {"t": t["id"]}).mappings().all()]
    # every serving rule, error rate first among them; availability rules were not fed
    on = {k for k, r in rules.items() if r["auto_respond"]}
    assert on == {"error_rate", "latency_p95", "no_traffic"}, on
    assert {m["kind"] for m in mine["rules"] if m["target_id"] == t["id"]} == on
    for k in on:
        assert rules[k]["triage"] is True
        assert rules[k]["respond_link"] == {"on_behalf_of": "usr_param_t", "grant_id": "cgr_t"}
        assert rules[k]["changed_by"] == "boss"
    assert not any(r["auto_respond"] for r in others)
    # each change audited, naming the subscription and the person
    assert len(audit) == 3 and all(a["changed_by"] == "boss" for a in audit)
    assert all(sid in a["reason"] and "usr_param_t" in a["reason"] for a in audit)
    assert all(a["before"]["auto_respond"] is False and a["after"]["auto_respond"] is True
               for a in audit)
    # the subscription: off, kept, and says why
    assert sub["enabled"] is False
    assert sub["config"]["retired"]["rules"] == [m["rule_id"] for m in mine["rules"]]
    # once: a second pass moves nothing
    with eng.begin() as c:
        assert all(g["subscription_id"] != sid for g in aa.retire_app_wide_response(c))


def test_a_rule_that_fed_an_incident_outside_the_serving_group_moves_too(pg):  # noqa: F811
    eng, pid = pg
    t, alert = _fired(eng, pid)
    with eng.begin() as c:
        hc = next(r for r in aa.ensure_rules(c, t) if r["kind"] == "health_check")
        c.execute(text("UPDATE app_alerts SET rule_id = :r WHERE id = :a"),
                  {"r": hc["id"], "a": alert["id"]})
        # a person had already switched latency on themselves: theirs is left as it is
        lat = next(r for r in aa.rules(c, t["id"]) if r["kind"] == "latency_p95")
        c.execute(text("UPDATE app_alert_rules SET auto_respond = true, respond_link = "
                       "CAST(:l AS JSONB) WHERE id = :r"),
                  {"r": lat["id"], "l": json.dumps({"on_behalf_of": "usr_other", "grant_id": "g2"})})
        _legacy_subscription(c, pid, on_behalf_of=None, grant_id=None)
        aa.retire_app_wide_response(c)
        rules = {r["kind"]: r for r in aa.rules(c, t["id"])}
    assert rules["health_check"]["auto_respond"] and rules["error_rate"]["auto_respond"]
    assert rules["error_rate"]["respond_link"] is None, "it ran as BOSS; so does the rule"
    assert rules["latency_p95"]["respond_link"]["on_behalf_of"] == "usr_other"


# ── the card says which rules respond, not an app-wide switch ───────────────────────────────
def test_the_incident_view_lists_the_rules_that_respond(pg):  # noqa: F811
    eng, pid = pg
    t, alert = _fired(eng, pid)
    with eng.begin() as c:
        c.execute(text("UPDATE incidents SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb)"
                       " || CAST(:x AS JSONB) WHERE id = :i"),
                  {"i": alert["incident_id"], "x": json.dumps({"app": {"target_id": t["id"]}})})
        before = ir.response_view(c, pid, alert["incident_id"])
        er = next(r for r in aa.rules(c, t["id"]) if r["kind"] == "error_rate")
        c.execute(text("UPDATE app_alert_rules SET auto_respond = true, respond_link = "
                       "CAST(:l AS JSONB) WHERE id = :r"),
                  {"r": er["id"], "l": json.dumps({"on_behalf_of": "usr_param_t"})})
        after = ir.response_view(c, pid, alert["incident_id"])
    assert "auto_respond" not in before and before["auto_respond_rules"] == []
    assert after["auto_respond_rules"] == [{"rule_id": er["id"], "kind": "error_rate",
                                            "alert": "AppErrorRate", "label": "Error rate",
                                            "on_behalf_of": "usr_param_t"}]
