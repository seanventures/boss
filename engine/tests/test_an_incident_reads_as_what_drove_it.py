"""An incident reads as what drove it; a pending alert counts from when it went pending; and a
message nothing is configured to send is a setup prompt, not a SEV1 page (operator, Oct 18).

4/5. An incident row is reused: AppErrorRate (SEV1) opened it in the morning, then a SEV3
     AppLatencyP95 alert fired into it. The incident read "Errors" at SEV1. Its label, title and
     SEV now come from the alert driving it: firing now, else pending, else the latest episode.
6.   A flapped alert pending again keeps its first fired_at; "pending 39 min" counted from it.
8.   BossOutboxBacklog fired SEV1 for 4.5 hours because the host may not send email. A send
     that fails because nothing is configured is a "Set up a contact point" card (-> Notify);
     only a send that actually fails pages.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import app_alerts_view as view
from boss_engine.services import monitor_scheduler as ms
from boss_engine.services import monitoring as mon
from boss_engine.services import notify
from boss_engine.services import obs_view

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _app(c, pid) -> dict:
    tid = f"t_drv_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) VALUES (:t, :p, 'api', 'acme/shop', 'prod', "
        " 'stack', 'api', 'active', 'test', '[]'::jsonb)"), {"t": tid, "p": pid})
    mon.ensure_target_services(c, pid)
    return {"id": tid}


def _incident(c, pid, title) -> str:
    iid = f"inc_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, status, "
        " signal_count, first_seen, last_seen, urgency, lifecycle) VALUES (:i, :p, :k, :t, 'api', "
        " 'critical', 'diagnosing', 1, :f, :f, 'high', 'detected')"),
        {"i": iid, "p": pid, "k": uuid4().hex, "t": title, "f": NOW - timedelta(hours=12)})
    return iid


def _alert(c, pid, t, iid, name, kind, severity, state, fired, cleared=None, value=None, unit=""):
    c.execute(text(
        "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, alertname, "
        " fingerprint, labels, state, active_at, fired_at, cleared_at, value, unit, incident_id) "
        "VALUES (:i, :p, :t, :k, :n, :f, CAST(:l AS jsonb), :s, :a, :a, :c, :v, :u, :inc)"),
        {"i": f"al_{uuid4().hex[:8]}", "p": pid, "t": t["id"], "k": kind, "n": name,
         "f": uuid4().hex[:16], "l": json.dumps({"severity": severity}), "s": state, "a": fired,
         "c": cleared, "v": value, "u": unit, "inc": iid})


def test_an_incident_takes_its_label_title_and_sev_from_the_alert_driving_it(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        iid = _incident(c, pid, "api (prod): 14.4% of requests failing over 5 min (alert above 5.0%)")
        _alert(c, pid, t, iid, "AppErrorRate", "error_rate", "critical", "resolved",
               NOW - timedelta(hours=12), NOW - timedelta(hours=11), 14.4, "%")
        _alert(c, pid, t, iid, "AppLatencyP95", "latency_p95", "warning", "firing",
               NOW - timedelta(minutes=4), None, 1240, "ms")
        got = next(i for i in view.incident_states(c, pid) if i["id"] == iid)
        assert got["sev"] == "sev3"                       # not the morning's SEV1
        assert got["driver"]["kind"] == "latency_p95"
        assert "AppLatencyP95" in got["title"] and "p95 latency" in got["title"]
        assert "failing" not in got["title"]

        # Recovered: it still reads as the latency alert (the latest episode), never "Errors".
        c.execute(text("UPDATE app_alerts SET state = 'resolved', cleared_at = :n "
                       "WHERE incident_id = :i AND alertname = 'AppLatencyP95'"),
                  {"n": NOW, "i": iid})
        got = next(i for i in view.incident_states(c, pid) if i["id"] == iid)
        assert (got["sev"], got["driver"]["alertname"]) == ("sev3", "AppLatencyP95")
        assert got["status_chip"]["key"] == "recovered"

        # The error alert firing again drives it again, at SEV1, with its own stored title.
        _alert(c, pid, t, iid, "AppErrorRate", "error_rate", "critical", "firing",
               NOW - timedelta(minutes=1), None, 9.0, "%")
        got = next(i for i in view.incident_states(c, pid) if i["id"] == iid)
        assert got["sev"] == "sev1" and got["title"].endswith("(alert above 5.0%)")


def test_a_pending_alert_counts_from_when_it_went_pending():
    fired = NOW - timedelta(minutes=40)
    again = NOW - timedelta(minutes=1)
    a = {"state": "pending", "fired_at": fired, "active_at": again, "cleared_at": None}
    assert view.duration_s(a, NOW) == 60
    assert view.duration_s({**a, "state": "firing"}, NOW) == 2400


def _notification(c, pid, cp, status, word):
    c.execute(text(
        "INSERT INTO notifications (id, project_id, group_key, contact_point_id, kind, dedupe_key, "
        " status, attempts, error_word, created_at) VALUES (:i, :p, 'g', :cp, 'firing', :d, :s, 6, "
        " :w, :at)"),
        {"i": f"ntf_{uuid4().hex[:10]}", "p": pid, "cp": cp, "d": uuid4().hex, "s": status,
         "w": word, "at": NOW - timedelta(minutes=30)})


def test_nothing_configured_to_send_is_a_setup_prompt_not_a_page(pg):
    eng, pid = pg
    settings = SimpleNamespace(monitoring_prometheus_url="")
    with eng.begin() as c:
        cp = f"cp_{uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO contact_points (id, project_id, name, kind, config, created_by) VALUES "
            " (:i, :p, 'On-call email', 'email', CAST(:c AS jsonb), 'test')"),
            {"i": cp, "p": pid, "c": json.dumps({"address": "oncall@example.invalid"})})
        _notification(c, pid, cp, "dead",
                      "IntegrationConfigurationError: BOSS_EMAIL_REAL_SEND_ALLOWED is false.")
        _notification(c, pid, cp, "failed", "NotConfigured: The contact point was removed")
        assert notify.not_configured("NotConfigured: x") and not notify.not_configured("HTTPError: 500")

        assert ms.self_check(c, settings, NOW)[f"{pid}:BossOutboxBacklog"] is False
        cards = obs_view.notify_setup(c, pid)
        assert [(x["title"], x["fix"]["view"]) for x in cards] == [("Set up a contact point", "notify")]
        assert cards[0]["detail"].startswith("2 messages not sent")

        # A send that actually fails still pages.
        _notification(c, pid, cp, "dead", "HTTPError: 500 Server Error")
        assert ms.self_check(c, settings, NOW)[f"{pid}:BossOutboxBacklog"] is True
