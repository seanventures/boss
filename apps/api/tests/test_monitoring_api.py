"""Monitoring Phase 1a over HTTP (BOSSAPP-598): Connect an app hands over the values once, the
ingest routes take a source token (and refuse in words), traces are refused rather than dropped,
the live check says what arrived, ack and the email's link work, the routing preview answers,
and the watchdog always fires."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from apps.api.tests._obs_api_seed import PROJECT_ID


def _otlp(service: str) -> bytes:
    t = str(int(time.time() * 1e9))
    return json.dumps({"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeMetrics": [{"metrics": [{"name": "http.server.request.duration", "unit": "s",
                                       "histogram": {"aggregationTemporality": 2, "dataPoints": [{
                                           "attributes": [{"key": "http.response.status_code",
                                                           "value": {"intValue": "200"}}],
                                           "timeUnixNano": t, "count": "10", "sum": 0.4,
                                           "bucketCounts": ["10", "0"],
                                           "explicitBounds": [0.5]}]}}]}]}]}).encode()


def test_connect_otel_ingest_and_the_live_check(client):
    name = f"stranger-{uuid4().hex[:6]}"
    got = client.post("/v1/monitoring/connect", json={
        "project_id": PROJECT_ID, "actor": "ceo", "path": "otel", "name": name})
    assert got.status_code == 201, got.text
    body = got.json()
    values = {v["name"]: v["value"] for v in body["paste"]["values"]}
    assert set(values) == {"OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS",
                           "OTEL_SERVICE_NAME"} and body["shown_once"] is True
    token = values["OTEL_EXPORTER_OTLP_HEADERS"].split("Bearer ")[1]
    sid = body["service"]["id"]
    before = client.get(f"/v1/monitoring/services/{sid}/live",
                        params={"project_id": PROJECT_ID}).json()
    assert before["arrived"] is False and before["hint"]

    refused = client.post("/v1/ingest/otlp/v1/metrics", content=_otlp(name),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer bsrc_not_a_token"})
    assert refused.status_code == 401 and "token" in refused.json()["detail"]
    ok = client.post("/v1/ingest/otlp/v1/metrics", content=_otlp(name),
                     headers={"Content-Type": "application/json",
                              "Authorization": f"Bearer {token}"})
    assert ok.status_code == 200, ok.text
    traces = client.post("/v1/ingest/otlp/v1/traces", content=b"{}",
                         headers={"Authorization": f"Bearer {token}"})
    assert traces.status_code == 501 and "not stored" in traces.json()["detail"]
    after = client.get(f"/v1/monitoring/services/{sid}/live",
                       params={"project_id": PROJECT_ID}).json()
    assert after["arrived"] and after["series"] >= 1 and name in after["word"]
    assert "OpenTelemetry" in after["word"]
    sources = client.get("/v1/signal-sources", params={"project_id": PROJECT_ID}).json()
    mine = next(s for s in sources["sources"] if s["service_id"] == sid)
    assert token not in json.dumps(sources) and mine["accepted_24h"] >= 1
    assert mine["refused_24h"] == 0, "an unknown token is no source's refusal"


def test_an_alertmanager_source_connects_and_alerts_can_be_acked(client):
    got = client.post("/v1/monitoring/connect", json={
        "project_id": PROJECT_ID, "actor": "ceo", "path": "alerts", "tool": "alertmanager",
        "name": f"am-{uuid4().hex[:4]}"}).json()
    assert "webhook_configs" in got["paste"]["block"]
    url = next(v["value"] for v in got["paste"]["values"] if v["name"] == "URL")
    token = next(v["value"] for v in got["paste"]["values"] if v["name"] == "Token")
    path = url.split("://", 1)[-1].split("/", 1)[1]
    body = json.dumps({"version": "4", "status": "firing", "alerts": [{
        "status": "firing", "labels": {"alertname": "DiskFull", "severity": "warning"},
        "annotations": {"summary": "disk"}, "startsAt": datetime.now(timezone.utc).isoformat(),
        "fingerprint": uuid4().hex[:16]}]}).encode()
    r = client.post("/" + path, content=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
    alert_id = r.json()["alert_id"]
    listed = client.get("/v1/alerts", params={"project_id": PROJECT_ID,
                                              "source": "external"}).json()
    assert any(a["id"] == alert_id and a["source"] == "Alertmanager" for a in listed["alerts"])
    ack = client.post("/v1/alerts/ack", json={"project_id": PROJECT_ID, "actor": "ceo",
                                              "alert_ids": [alert_id]})
    assert ack.status_code == 200 and ack.json()["acked"] == [alert_id], ack.text
    acked = client.get("/v1/alerts", params={"project_id": PROJECT_ID, "acked": "true"}).json()
    assert any(a["id"] == alert_id and a["acked"] for a in acked["alerts"])
    bad = client.post("/" + path, content=b"{not json", headers={
        "Authorization": f"Bearer {token}"})
    assert bad.status_code == 400 and bad.json()["detail"]


def test_preview_policies_and_the_email_link(client, engine):
    from apps.api.app.db import begin
    from boss_engine.services import monitoring as mon

    pv = client.post("/v1/notification-policies/preview", json={
        "project_id": PROJECT_ID, "labels": {"alertname": "X", "severity": "critical"}})
    assert pv.status_code == 200 and pv.json()["routes"][0]["path"], pv.text
    added = client.post("/v1/notification-policies", json={
        "project_id": PROJECT_ID, "actor": "ceo", "name": "db team",
        "matchers": 'team="db"', "group_wait_s": 0})
    assert added.status_code == 201, added.text
    pv2 = client.post("/v1/notification-policies/preview", json={
        "project_id": PROJECT_ID, "labels": {"team": "db"}}).json()
    assert pv2["routes"][0]["path"][-1] == "db team" and pv2["routes"][0]["group_wait_s"] == 0
    with begin(engine) as c:
        token = mon.mint_action_token(c, PROJECT_ID, "ack", incident_id=None, alert_id="alrt_x")
    page = client.get(f"/v1/alert-actions/{token}")
    assert page.status_code == 200 and "<form method=post>" in page.text
    used = client.post(f"/v1/alert-actions/{token}")
    assert used.status_code == 200 and used.json()["action"] == "ack"
    again = client.post(f"/v1/alert-actions/{token}")
    assert again.status_code == 410 and "Already used" in again.json()["detail"]


def test_the_watchdog_always_fires(client):
    r = client.get("/monitoring/watchdog")
    assert r.status_code == 200 and 'alertname="BossWatchdog"' in r.text
