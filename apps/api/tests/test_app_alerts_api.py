"""App alerts over HTTP (BOSSAPP-598): an app's rules, a person's edit and silence, the alert
history, every app's alerts, a signals token rotated without ever being shown, and Grafana
linked only after BOSS checked that it answers."""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import PROJECT_ID, deployed_target


def _app(engine, name: str = "Order API") -> str:
    tid = f"t_al_{uuid4().hex[:8]}"
    with begin(engine) as c:
        deployed_target(c, PROJECT_ID, tid, name, "staging")
    return tid


def test_an_apps_rules_can_be_read_changed_and_silenced(client, engine):
    tid = _app(engine)
    got = client.get(f"/v1/apps/{tid}/alert-rules", params={"project_id": PROJECT_ID})
    assert got.status_code == 200, got.text
    body = got.json()
    rules = {r["kind"]: r for r in body["rules"]}
    assert set(rules) == {"error_rate", "latency_p95", "health_check", "no_traffic",
                          "deploy_health"}
    er = rules["error_rate"]
    assert er["condition"] == "> 5% for 5 min (≥ 20 req)" and er["source_word"] == "BOSS default"
    assert er["expr"].startswith("100 * sum(increase(http_requests_total{app=\"Order API\"")
    assert er["runbook"]["kind"] == "builtin" and er["runbook"]["markdown"]
    assert body["evaluator"] == "BOSS's own monitoring"
    assert body["export"]["groups"][0]["rules"][0]["alert"] == "AppErrorRate"

    put = client.put(f"/v1/apps/{tid}/alert-rules", json={
        "project_id": PROJECT_ID, "actor": "ceo", "reason": "checkout matters",
        "rules": [{"id": er["id"], "params": {"threshold": 3}, "severity": "warning"}]})
    assert put.status_code == 200, put.text
    edited = next(r for r in put.json()["rules"] if r["kind"] == "error_rate")
    assert edited["condition"].startswith("> 3%") and edited["source"] == "console"
    assert edited["severity"] == "warning" and edited["changed_at"]
    assert put.json()["changes"][0]["reason"] == "checkout matters"

    bad = client.put(f"/v1/apps/{tid}/alert-rules", json={
        "project_id": PROJECT_ID, "actor": "ceo",
        "rules": [{"id": er["id"], "params": {"threshold": 400}}]})
    assert bad.status_code == 422 and "between" in bad.json()["detail"]
    custom = client.put(f"/v1/apps/{tid}/alert-rules", json={
        "project_id": PROJECT_ID, "actor": "ceo",
        "rules": [{"alert": "CheckoutSlow", "for": "2m",
                   "expr": 'histogram_quantile(0.99, sum by (le) (rate('
                           'http_request_duration_seconds_bucket{route="/orders"}[5m]))) > 2',
                   "labels": {"severity": "critical"},
                   "annotations": {"summary": "Checkout is slow",
                                   "runbook_url": "docs/runbooks/checkout.md"}}]})
    assert custom.status_code == 200, custom.text
    mine = next(r for r in custom.json()["rules"] if r["alert"] == "CheckoutSlow")
    assert mine["kind"] == "custom" and mine["for_seconds"] == 120
    assert mine["runbook"]["kind"] == "repo" and mine["runbook"]["href"].endswith(
        "/acme/app/blob/HEAD/docs/runbooks/checkout.md")

    no_reason = client.post(f"/v1/apps/{tid}/alert-rules/{er['id']}/silence",
                            json={"project_id": PROJECT_ID, "actor": "ceo", "minutes": 60})
    assert no_reason.status_code == 422
    silenced = client.post(f"/v1/apps/{tid}/alert-rules/{er['id']}/silence",
                           json={"project_id": PROJECT_ID, "actor": "ceo", "minutes": 60,
                                 "reason": "load test"})
    assert silenced.status_code == 200, silenced.text
    after = client.get(f"/v1/apps/{tid}/alert-rules", params={"project_id": PROJECT_ID}).json()
    assert next(r for r in after["rules"] if r["kind"] == "error_rate")["silence"]["reason"] \
        == "load test"
    ended = client.post(f"/v1/apps/{tid}/alert-silences/{silenced.json()['id']}/end",
                        json={"project_id": PROJECT_ID, "actor": "ceo"})
    assert ended.status_code == 200

    hist = client.get(f"/v1/apps/{tid}/alerts", params={"project_id": PROJECT_ID})
    assert hist.status_code == 200 and hist.json()["alerts"] == []
    every = client.get("/v1/alerts", params={"project_id": PROJECT_ID, "state": "all"})
    assert every.status_code == 200
    assert any(a["target_id"] == tid for a in every.json()["apps"])
    assert client.get("/v1/apps/t_nope/alerts",
                      params={"project_id": PROJECT_ID}).status_code == 404


def test_rotating_a_signals_token_never_shows_it(client, engine):
    tid = _app(engine, "Shop")
    reporter = _app(engine, "helpdesk-ai")
    r = client.post(f"/v1/apps/{tid}/signal-sources/{reporter}/rotate",
                    json={"project_id": PROJECT_ID, "actor": "ceo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"target_id", "rotated_at", "word"}
    assert body["word"] == "Rotated. Redeploy helpdesk-ai so it has the new token."
    with begin(engine) as c:
        meta = c.execute(text("SELECT metadata FROM secret_refs WHERE scope_id = :t"),
                         {"t": reporter}).scalar()
    assert "last4" not in (meta or {})


def test_grafana_is_linked_only_after_it_answered(client, engine):
    tid = _app(engine, "Ledger")
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO telemetry_sources (id, project_id, kind, name, config, enabled, "
            " created_at) VALUES ('tsrc_alerts_api', :p, 'prometheus', 'boss-prometheus', "
            " CAST(:c AS jsonb), true, now()) ON CONFLICT (project_id, kind, name) DO UPDATE "
            " SET config = EXCLUDED.config"),
            {"p": PROJECT_ID, "c": json.dumps({"base_url": "http://127.0.0.1:1"})})
    # the stack is registered where nothing answers (the VM's port 3006 seen from a laptop)
    stack = client.get("/v1/observability/stack", params={"project_id": PROJECT_ID})
    assert stack.status_code == 200, stack.text
    app = next(a for a in stack.json()["apps"] if a["target_id"] == tid)
    assert app["dashboard_url"] is None
    assert app["dashboard_reason"] == "Grafana isn't answering on its host"
    assert stack.json()["grafana"]["state"] == "not_answering"
    assert stack.json()["grafana_url"] == ""

    check = client.post("/v1/observability/grafana/check",
                        json={"project_id": PROJECT_ID, "actor": "ceo"})
    assert check.status_code == 200, check.text
    assert check.json()["grafana"]["state"] == "not_answering"
    assert check.json()["grafana"]["url"] is None
    drawer = client.get(f"/v1/observability/apps/{tid}", params={"project_id": PROJECT_ID})
    facts = drawer.json()["facts"]
    assert facts["dashboard_url"] is None
    assert facts["dashboard_reason"] == "Grafana isn't answering on its host"
    pulse = client.get("/v1/observability/apps", params={"project_id": PROJECT_ID}).json()
    assert pulse["grafana"]["state"] == "not_answering" and pulse["grafana"]["url"] is None
