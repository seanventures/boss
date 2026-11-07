"""ONE ALERTS CONCEPT (operator, 2026-11-07: "what is Alerts under Observability vs Alerts we have
under Health in each project — are they same or we have some differences?").

They are the same list. A project's Health shows its slice — GET /v1/alerts?scope=project: its
apps' alerts and open incidents, never BOSS's own platform alerts. Observability › Alerts shows
every readable project's slice, row for row the same, each tagged with its project, plus BOSS's
platform alerts once (each project carries its own copy of the same self-check) —
GET /v1/alerts/all. Both say what they cover (`scope`).

Real Postgres and the HelpDesk seed; no model, no GitHub.
"""
from __future__ import annotations

import json
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
    w.owner = w.person("al-owner")
    w.bind(w.owner, "owner")
    w.stranger = w.person("al-stranger")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _project(world, client, engine, name: str) -> dict:
    slug = f"al-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": name, "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, "name": name, **story}


def _alert(db, pid: str, subject: str, name: str, *, at: datetime, state: str = "firing",
           labels: dict | None = None) -> str:
    aid = f"alrt_{uuid.uuid4().hex[:10]}"
    db.execute(text(
        "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, "
        " alertname, labels, state, active_at, fired_at, cleared_at) VALUES (:i, :p, :t, "
        " 'custom', :f, :n, CAST(:l AS jsonb), :s, :at, :at, :c)"),
        {"i": aid, "p": pid, "t": subject, "f": uuid.uuid4().hex, "n": name,
         "l": json.dumps({"severity": "warning", **(labels or {})}), "s": state, "at": at,
         "c": None if state != "resolved" else at + timedelta(minutes=5)})
    return aid


def _get(world, client, who, path):
    with world.acting_as(world.ctx(who)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_health_and_observability_list_the_same_alerts_each_saying_its_scope(world, client,
                                                                           engine):
    a = _project(world, client, engine, "HelpDesk")
    b = _project(world, client, engine, "NorthPeak Retail")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    boss = {"layer": "boss"}
    with engine.begin() as db:
        a_fire = _alert(db, a["id"], a["target_id"], "HelpdeskErrorRate", at=now)
        a_old = _alert(db, a["id"], a["target_id"], "AppLatencyP95",
                       at=now - timedelta(hours=2), state="resolved")
        b_fire = _alert(db, b["id"], b["target_id"], "CheckoutLatency", at=now)
        # BOSS's own self-check, as every project records it: the same alert twice.
        a_boss = _alert(db, a["id"], f"svc_{uuid.uuid4().hex[:8]}", "BossEvaluatorLag",
                        at=now - timedelta(minutes=3), labels=boss)
        b_boss = _alert(db, b["id"], f"svc_{uuid.uuid4().hex[:8]}", "BossEvaluatorLag",
                        at=now - timedelta(minutes=3), labels=boss)

    # HEALTH: this project's slice — its apps only, and it says so.
    health = _get(world, client, world.owner,
                  f"/v1/alerts?project_id={a['id']}&scope=project&state=all")
    ids = [x["id"] for x in health["alerts"]]
    assert {a_fire, a_old} <= set(ids)
    assert not {a_boss, b_boss} & set(ids), "a project's Health never lists BOSS's platform alerts"
    assert not any(x["platform"] for x in health["alerts"])
    assert health["scope"]["kind"] == "project" and health["scope"]["project_name"] == "HelpDesk"
    assert health["scope"]["apps"] >= 1 and health["scope"]["boss"] is False
    assert health["counts"]["firing"] == sum(x["state"] == "firing" for x in health["alerts"])

    # OBSERVABILITY: every project's slice, the same rows, plus BOSS itself once.
    every = _get(world, client, world.owner, "/v1/alerts/all?state=all")
    rows = {x["id"]: x for x in every["alerts"]}
    assert {a_fire, a_old, b_fire} <= set(rows)
    assert len({a_boss, b_boss} & set(rows)) == 1, "BOSS's self-check is one alert, not one each"
    platform = rows[a_boss] if a_boss in rows else rows[b_boss]
    assert platform["platform"] is True and platform["app"] == "BOSS"
    for mine in health["alerts"]:
        theirs = rows[mine["id"]]
        assert {k: theirs[k] for k in mine} == mine, "row for row the same as Health"
        assert theirs["project_id"] == a["id"] and theirs["project_name"] == "HelpDesk"
        assert "tab=health" in theirs["project_href"]
    assert rows[b_fire]["project_name"] == "NorthPeak Retail"
    assert every["scope"]["kind"] == "all" and every["scope"]["boss"] is True
    assert every["scope"]["apps"] >= 2 and every["scope"]["projects"] >= 2
    # Firing now: each project's app alert once, and BOSS's self-check once.
    firing = _get(world, client, world.owner, "/v1/alerts/all")
    assert {a_fire, b_fire} <= {x["id"] for x in firing["alerts"]}
    assert a_old not in {x["id"] for x in firing["alerts"]}
    assert {x["target_id"] for x in health["apps"]} <= {x["target_id"] for x in every["apps"]}
    assert all(x["project_name"] for x in every["apps"])

    # One app, wherever it lives: Observability's app filter.
    only_b = _get(world, client, world.owner, f"/v1/alerts/all?state=all&app={b['target_id']}")
    assert b_fire in {x["id"] for x in only_b["alerts"]}
    assert {x["project_id"] for x in only_b["alerts"]} == {b["id"]}
    assert not any(x["platform"] for x in only_b["alerts"])

    # A stranger reads none of it.
    with world.acting_as(world.ctx(world.stranger)):
        seen = client.get("/v1/alerts/all?state=all").json()
    assert not {a_fire, b_fire, a_boss, b_boss} & {x["id"] for x in seen.get("alerts", [])}


def test_a_self_check_on_an_apps_own_target_never_hides_the_apps_incident(world, client, engine):
    """BOSS's self-check can be recorded against an app's own target: the app (and its open
    incident) stays the app's in Health; only incidents made of platform alerts alone are BOSS's."""
    a = _project(world, client, engine, "HelpDesk")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    inc = f"inc_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, status, "
            " signal_count, first_seen, last_seen) VALUES (:i, :p, :k, 'p95 latency', 'app', "
            " 'critical', 'open', 1, :n, :n)"), {"i": inc, "p": a["id"], "k": inc, "n": now})
        app_alert = _alert(db, a["id"], a["target_id"], "AppLatencyP95", at=now)
        db.execute(text("UPDATE app_alerts SET incident_id = :i WHERE id = :a"),
                   {"i": inc, "a": app_alert})
        _alert(db, a["id"], a["target_id"], "BossEvaluatorLag", at=now, labels={"layer": "boss"})
    health = _get(world, client, world.owner,
                  f"/v1/alerts?project_id={a['id']}&scope=project&state=all")
    assert inc in {i["id"] for i in health["incidents"]}
    assert a["target_id"] in {x["target_id"] for x in health["apps"]} or not health["apps"]
