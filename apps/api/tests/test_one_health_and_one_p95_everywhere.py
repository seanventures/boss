"""One health word and one p95 on every surface (BOSSAPP-913/914, tester round 2: 12-05, 15-03 /
PX-09, 12-14/15, 12-17).

  * 12-05  a project is never "Healthy" while its SLO is breached or an alert fires: the state is
           decided once (health.state_of) from the probe, the 24 h SLO and what fires, and the
           Health tab, the Overview header and tile, Deployments and the Projects list all say it;
  * 15-03  one p95 and one window: the Health tab, the Overview tile, Deployments and
           Observability's app row print the same number, BOSS's probes over the last 24 h;
  * 12-14  rule names in words everywhere (no `TicketsApi5xx`); one app's alert history never
           carries BOSS's own platform alerts;
  * 12-17  Beacon's pack counts every rule (the switched-off one said), the same rows the Alerts
           card lists.
Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("one-health-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-onehealth-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _get(world, client, path):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def _breach(engine, pid, target):
    """Ten failed checks two to three hours ago: the day's uptime falls far under 99.5 %, while
    the newest checks still answer (the probe alone would say "up")."""
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        for i in range(10):
            db.execute(text(
                "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
                "checked_at, ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, false, 502, "
                "NULL)"), {"i": f"dhs_{uuid.uuid4().hex[:12]}", "p": pid, "t": target,
                           "at": now - timedelta(hours=2, minutes=6 * i)})


def test_a_breached_slo_is_never_healthy_on_any_surface(world, client, helpdesk, engine):
    pid, target = helpdesk["id"], helpdesk["target_id"]
    _breach(engine, pid, target)
    env = next(e for e in _get(world, client, f"/v1/projects/{pid}/health")["environments"]
               if e["target_id"] == target)
    assert env["slo"]["burn"] == "breach"
    assert env["health"] == "degraded" and "SLO breached" in env["health_reason"]
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    assert ov["project"]["health"] == "degraded"
    assert "SLO breached" in ov["project"]["health_reason"]
    assert ov["tiles"]["staging"]["health"] == "degraded"
    dep = next(e for e in _get(world, client, f"/v1/projects/{pid}/deployments")["environments"]
               if e["target_id"] == target)
    assert dep["health"] == "degraded"
    card = next(p for p in _get(world, client, "/v1/projects/summary")["items"] if p["id"] == pid)
    assert card["health"] == "degraded"


def test_a_firing_alert_is_never_healthy(world, client, helpdesk, engine):
    pid, target = helpdesk["id"], helpdesk["target_id"]
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, "
            "alertname, labels, state, active_at, fired_at) VALUES (:i, :p, :t, 'error_rate', "
            ":f, 'AppErrorRate', '{\"severity\": \"sev1\"}', 'firing', now(), now())"),
            {"i": f"alr_{uuid.uuid4().hex[:10]}", "p": pid, "t": target, "f": uuid.uuid4().hex})
    env = _get(world, client, f"/v1/projects/{pid}/health")["environments"][0]
    assert env["health"] == "degraded" and env["health_reason"].startswith("Error rate firing")
    assert _get(world, client, f"/v1/projects/{pid}/overview")["project"]["health"] == "degraded"


def test_state_of_reads_probe_slo_and_alerts_in_one_place():
    from boss_engine.services.project_experience.health import state_of

    assert state_of("up", 99.9, []) == ("healthy", None)
    assert state_of("down", 99.9, [])[0] == "down"
    assert state_of("up", 98.13, [])[0] == "degraded"            # breached: never healthy
    assert state_of("up", 99.2, [])[0] == "degraded"             # at risk (warning): amber
    assert state_of("up", 99.9, ["5xx on /api/tickets"]) == ("degraded",
                                                             "5xx on /api/tickets firing")
    assert state_of("not_deployed", None, []) == ("unknown", None)


def test_one_p95_and_one_window_on_every_surface(world, client, helpdesk, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import obs_view

    pid, target = helpdesk["id"], helpdesk["target_id"]
    env = next(e for e in _get(world, client, f"/v1/projects/{pid}/health")["environments"]
               if e["target_id"] == target)
    assert env["p95_ms"] is not None and env["p95_source"] == "probe" and env["window"] == "24 h"
    tile = _get(world, client, f"/v1/projects/{pid}/overview")["tiles"]["staging"]
    dep = next(e for e in _get(world, client, f"/v1/projects/{pid}/deployments")["environments"]
               if e["target_id"] == target)
    with engine.connect() as db:
        row = next(r for r in obs_view.pulse(db, get_settings(), pid)["rows"]
                   if r["target_id"] == target)
    assert tile["p95_ms"] == dep["p95_ms"] == env["p95_ms"] == row["p95_ms"]
    assert tile["window"] == dep["window"] == "24 h"
    assert round(row["uptime"] * 100, 2) == env["uptime_pct"] == dep["uptime_pct"]


def test_rule_names_are_words_and_an_apps_history_has_no_boss_alerts(world, client, helpdesk,
                                                                     engine):
    from boss_engine.services import app_alerts_view
    from boss_engine.services.app_alerts import rule_label, rule_name_words

    assert rule_label("custom", "TicketsApi5xx", "5xx on /api/tickets") == "5xx on /api/tickets"
    assert rule_label("custom", "HostDiskFilling", None) == "Host disk filling"
    assert rule_label("error_rate", "AppErrorRate", "anything") == "Error rate"
    assert rule_label("custom", "BossEvaluatorLag",
                      "1 rule group(s) are evaluating late (more than twice their interval)") \
        == "Evaluator lag"
    assert rule_name_words("PgConnectionsHigh") == "Pg connections high"
    pid, target = helpdesk["id"], helpdesk["target_id"]
    # The seed's platform alert sits on the app's target (subject), as a stray one would.
    with engine.connect() as db:
        hist = app_alerts_view.alerts(db, pid, target_id=target, since="7d")
    names = [a["alertname"] for a in hist["alerts"]]
    assert "BossEvaluatorLag" not in names
    labels = [a["label"] for a in hist["alerts"]]
    assert "TicketsApi5xx" not in labels and "5xx on /api/tickets" in labels
    import re

    for a in _get(world, client, f"/v1/projects/{pid}/health")["alerts"]:
        assert not re.search(r"[a-z][A-Z]|_", a["label"]), a["label"]     # never an identifier


def test_the_pack_counts_every_rule_the_alerts_card_lists(world, client, helpdesk, engine):
    pid = helpdesk["id"]
    with engine.begin() as db:
        db.execute(text("UPDATE app_alert_rules SET enabled = false WHERE id = :r"),
                   {"r": helpdesk["alerting"]["rules"]["AppLatencyP95"]})
    h = _get(world, client, f"/v1/projects/{pid}/health")
    assert h["monitoring_pack"]["rules"] == 7 and h["monitoring_pack"]["off"] == 1
    assert len(h["alerts"]) == 7
    off = [a for a in h["alerts"] if a["state"] == "off"]
    assert len(off) == 1 and off[0]["label"] == "p95 latency" and h["alerts"][-1]["state"] == "off"
