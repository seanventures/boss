"""RB-10 — alert-driven trigger: signed webhook → incident → scoped scan of registered sources →
diagnose. Fail-closed signature; Alertmanager/PagerDuty/generic shapes. Against boss_test."""
import hashlib
import hmac
import json

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
SECRET = "alert-test-secret"


def _rs():
    s = get_settings()
    s.reliability_alert_secret = SECRET
    return ReliabilityService(s)


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# --- signature ---------------------------------------------------------------
def test_signature_fail_closed():
    rs = _rs()
    assert rs.verify_alert_signature(b"x", "") is False
    assert rs.verify_alert_signature(b"x", "bad") is False
    good = _sign(b"x")
    assert rs.verify_alert_signature(b"x", good) is True
    assert rs.verify_alert_signature(b"x", "sha256=" + good) is True     # prefixed form tolerated
    rs.settings.reliability_alert_secret = None
    assert rs.verify_alert_signature(b"x", good) is False                # no secret -> closed


# --- shape parsing -----------------------------------------------------------
def test_parse_alert_shapes():
    rs = _rs()
    am = rs._parse_alert({"alerts": [{"labels": {"alertname": "HighErr", "service": "pay",
                                                 "severity": "critical"},
                                      "annotations": {"summary": "err rate 40%"}}]})
    assert am == {"title": "HighErr", "service": "pay", "severity": "critical",
                  "description": "err rate 40%"}
    pd = rs._parse_alert({"event": {"data": {"title": "DB down", "priority": {"summary": "P1"},
                                             "service": {"summary": "orders"}}}})
    assert pd["severity"] == "critical" and pd["service"] == "orders"
    generic = rs._parse_alert({"alertname": "X", "service": "s"})
    assert generic["title"] == "X"
    assert rs._parse_alert({"nope": 1}) is None


# --- ingest ------------------------------------------------------------------
def test_ingest_alert_creates_incident_scans_sources_and_diagnoses(engine):
    rs = _rs()
    with begin(engine) as c:
        # a registered memory source whose logs still carry the failure (ts inside [now-window, now])
        rs.register_source(c, PID, "memory", "stub-src", config={"logs": [
            {"ts": float(i), "message": f"al-pay upstream refused {i}", "service": "al-pay",
             "level": "error"} for i in range(8)]})
        out = rs.ingest_alert(c, PID, {"alerts": [{"labels": {
            "alertname": "PayErrors", "service": "al-pay", "severity": "critical"}}]},
            scan_window_minutes=10**9)          # start goes far negative -> covers ts=0..7
        assert out["incident_id"].startswith("inc_")
        assert out["scans"] and out["scans"][0]["source"] == "stub-src"
        assert out["scans"][0]["signals"] >= 1                     # the source scan found the errors
        assert out["hypothesis"] is not None
        detail = rs.get_incident(c, PID, out["incident_id"])
        assert detail["severity"] == "critical" and detail["status"] == "diagnosing"


def test_ingest_alert_broken_source_does_not_kill_intake(engine):
    rs = _rs()
    with begin(engine) as c:
        rs.register_source(c, PID, "log_export", "bad-src", config={"path": "/nope/missing.log"})
        out = rs.ingest_alert(c, PID, {"alertname": "X", "service": "al-x"})
        assert out is not None
        assert any("error" in s for s in out["scans"])             # recorded, not raised


# --- HTTP (signed, auth-exempt) ----------------------------------------------
def test_http_alert_requires_valid_signature(client, monkeypatch):
    # patch the EXACT settings instance the app's reliability service holds (conftest clears the
    # get_settings cache, so a fresh get_settings() is a different object)
    from apps.api.app import main as api_main
    monkeypatch.setattr(api_main.reliability.settings, "reliability_alert_secret", SECRET)
    body = json.dumps({"alertname": "HttpAlert", "service": "al-http",
                       "project_id": PID}).encode()
    r = client.post("/v1/reliability/alert", content=body,
                    headers={"X-Reliability-Signature": "wrong"})
    assert r.status_code == 401
    r2 = client.post("/v1/reliability/alert", content=body,
                     headers={"X-Reliability-Signature": _sign(body)})
    assert r2.status_code == 202, r2.text
    assert r2.json()["incident_id"].startswith("inc_")


def test_http_sources_roundtrip(client):
    r = client.post("/v1/reliability/sources", json={
        "project_id": PID, "actor": ACTOR, "kind": "prometheus", "name": "prom-main",
        "config": {"base_url": "http://prom:9090", "bearer_env": "PROM_TOKEN"}})
    assert r.status_code == 201, r.text
    lst = client.get("/v1/reliability/sources", params={"project_id": PID})
    assert any(s["name"] == "prom-main" for s in lst.json()["items"])
