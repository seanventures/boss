"""Project › Health reads like wireframe 12 (BOSSAPP-913/914, GAPS #13 and #9, ACCEPTANCE 12-xx).

A HelpDesk-like project with Beacon's pack (rules from the repo's boss.deploy.yaml), the app's own
metrics, two incidents the SRE squad worked and an older latency alert — plus one of BOSS's OWN
platform alerts (layer boss, "1 rule group(s) are evaluating late…") — read through
GET /v1/projects/{id}/health:

  * the Alerts card is one row per RULE, by name ("5xx on /api/tickets", "p95 latency"), with
    where it stands: resolved at …, or ok · quiet Nd; the platform alert never shows;
  * each incident is its own story from the squad's ledger: signal → SRE squad → cause → fix →
    Recovered (or Approve fix while the fix waits on a person), with when, how long and how it ended;
  * Beacon's pack says what it watches and links its file in the repo;
  * the SLO burn says how much of the day's error budget was used.
Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("health-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-health-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _health(world, client, pid):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(f"/v1/projects/{pid}/health")
    assert r.status_code == 200, r.text
    return r.json()


def test_alerts_are_one_row_per_rule_by_name_and_boss_platform_alerts_stay_out(world, client,
                                                                                helpdesk):
    h = _health(world, client, helpdesk["id"])
    labels = [a["label"] for a in h["alerts"]]
    assert "5xx on /api/tickets" in labels and "p95 latency" in labels and "Health check" in labels
    assert not any("rule group" in a["label"] or "rule group" in (a.get("detail") or "")
                   for a in h["alerts"])
    assert len(labels) == len(set(labels)) == 7          # one row per rule of the pack
    tickets = next(a for a in h["alerts"] if a["label"] == "5xx on /api/tickets")
    assert tickets["state"] == "resolved" and tickets["severity"] == "page" and tickets["since"]
    p95 = next(a for a in h["alerts"] if a["label"] == "p95 latency")
    assert p95["state"] == "ok" and p95["severity"] == "ticket" and p95["quiet"] == "quiet 3d"
    assert p95["condition"] == "> 800 ms for 5 min"
    health_check = next(a for a in h["alerts"] if a["label"] == "Health check")
    assert health_check["quiet"] == "quiet 5d"
    assert [a["state"] for a in h["alerts"]][0] == "resolved"   # what moved first, then quiet


def test_a_firing_rule_comes_first_and_says_since_when(world, client, helpdesk, engine):
    rule = helpdesk["alerting"]["rules"]["AppErrorRate"]
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, subject_id, rule_id, rule_kind, "
            "fingerprint, alertname, labels, state, active_at, fired_at) VALUES (:i, :p, :t, :t, :r, "
            "'error_rate', :f, 'AppErrorRate', '{\"severity\": \"sev1\"}', 'firing', now(), now())"),
            {"i": f"alr_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"], "t": helpdesk["target_id"],
             "r": rule, "f": uuid.uuid4().hex})
    h = _health(world, client, helpdesk["id"])
    first = h["alerts"][0]
    assert first["label"] == "Error rate" and first["state"] == "firing" and first["since"]


def test_each_incident_is_its_own_story_from_the_squads_ledger(world, client, helpdesk):
    h = _health(world, client, helpdesk["id"])
    items = helpdesk["alerting"]["items"]
    done = next(i for i in h["incidents"] if i["id"] == items["5xx"])
    assert done["state"] == "resolved" and done["outcome_word"] == "resolved by the squad"
    assert done["duration_s"] == 14 * 60 and done["since"]
    assert [(m["label"], m["state"], m.get("type")) for m in done["mini"]] == [
        ("Alert", "done", "signal"), ("SRE squad", "done", "agent"),
        ("Pool exhausted", "done", "cause"), ("Pool 10→20", "done", "fix"),
        ("Recovered", "done", None)]
    waiting = next(i for i in h["incidents"] if i["id"] == items["slow"])
    assert waiting["state"] == "needs_you" and waiting["outcome_word"] == "fix needs your approval"
    assert [(m["label"], m["state"]) for m in waiting["mini"]] == [
        ("p95 1.4s", "done"), ("SRE squad", "done"), ("Missing index", "done"),
        ("Approve fix", "waiting"), ("Recovered", "todo")]
    assert waiting["graph_href"].endswith(f"area=agent_ops&item={items['slow']}")
    assert all(len(m["label"].split()) <= 4 for i in h["incidents"] for m in i["mini"])


def test_beacons_pack_says_what_it_watches_and_where_it_lives(world, client, helpdesk):
    pack = _health(world, client, helpdesk["id"])["monitoring_pack"]
    assert pack["rules"] == 7 and pack["managed_in"] == "repo"
    assert set(pack["kinds"]) >= {"Uptime", "5xx", "Latency", "Disk", "DB conns", "TLS"}
    assert pack["file"]["path"] == "boss.deploy.yaml"
    assert pack["file"]["external_url"].startswith("https://github.com/acme/helpdesk-ai-demo/blob/")


def test_the_environment_says_how_much_of_the_error_budget_the_day_used(world, client, helpdesk):
    env = _health(world, client, helpdesk["id"])["environments"][0]
    assert env["slo"]["target"] == 99.5 and env["slo"]["burn"] in ("ok", "warning", "breach")
    assert isinstance(env["slo"]["budget_used_pct"], int)
    assert 0 <= env["slo"]["budget_used_pct"] <= 100
    # The app's own metrics feed error rate and p95 (one p95 source for every tab).
    assert env["error_rate"] is not None and env["p95_ms"] is not None
    assert len(env["series"]["errors"]) > 2


def test_budget_used_is_downtime_over_what_the_slo_allows():
    from boss_engine.services.project_experience.health import alert_name, budget_used, rule_words

    assert budget_used(100.0) == 0 and budget_used(99.89) == 22 and budget_used(99.0) == 100
    assert budget_used(None) is None
    assert alert_name("5xx on /api/tickets", "TicketsApi5xx") == "5xx on /api/tickets"
    assert alert_name("1 rule group(s) are evaluating late (more than twice their interval)",
                      "BossEvaluatorLag") == "Evaluator lag"
    assert rule_words("HelpdeskErrorRate") == "Error rate"
