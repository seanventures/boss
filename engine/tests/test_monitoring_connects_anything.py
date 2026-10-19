"""Monitoring Phase 1a (BOSSAPP-598): any app connects — OpenTelemetry, remote write, an
alerting tool — with its own source token, and what it sends lands in the one alert record.

Acceptance (PRD §12, Phase 1a): 1 (an external OTel app, built-ins over semconv, a staged 5xx
fires), 2 (Alertmanager: one alert, its resolve resolves it; a replay, a wrong token and a body
naming another project are refused in words), 10 (tenancy), 11 (the deprecated reliability door
lands in the same pipeline). Plus heartbeats, synthetics' SSRF rule and the 24 h quiet resolve.
"""
from __future__ import annotations

import gzip
import json
import struct
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import alert_ingest, app_alerts as aa, metric_ingest as mi
from boss_engine.services import monitoring as mon
from boss_engine.services import promql_lite as pq

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _source(eng, pid, kind, **kw):
    with eng.begin() as c:
        return mon.create_source(c, pid, kind=kind, name=kw.pop("name", kind), actor="usr_t",
                                 **kw)


def _otlp_json(service: str, t: datetime, ok: float, err: float, *, env="production") -> bytes:
    """Node's OTel SDK with stable HTTP semconv: a cumulative histogram per status code."""
    def point(code, count):
        return {"attributes": [{"key": "http.request.method", "value": {"stringValue": "GET"}},
                               {"key": "http.route", "value": {"stringValue": "/checkout"}},
                               {"key": "http.response.status_code",
                                "value": {"intValue": str(code)}}],
                "timeUnixNano": str(int(t.timestamp() * 1e9)), "count": str(int(count)),
                "sum": count * 0.05, "bucketCounts": [str(int(count)), "0"],
                "explicitBounds": [0.1]}
    return json.dumps({"resourceMetrics": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}},
            {"key": "deployment.environment.name", "value": {"stringValue": env}},
            {"key": "project", "value": {"stringValue": "someone-elses"}}]},
        "scopeMetrics": [{"metrics": [{
            "name": "http.server.request.duration", "unit": "s",
            "histogram": {"aggregationTemporality": 2,
                          "dataPoints": [point(200, ok), point(500, err)]}}]}]}]}).encode()


def _ingest_otlp(eng, src, body, t, *, ctype="application/json", enc=""):
    with eng.begin() as c:
        return mi.ingest_otlp(c, get_settings(), src, body, content_type=ctype,
                              content_encoding=enc, now=t)


# ── 1. an external OTel app ─────────────────────────────────────────────────────────────────
def test_an_external_otel_app_connects_gets_builtins_over_semconv_and_a_5xx_fires(pg):
    eng, pid = pg
    made = _source(eng, pid, "otlp")
    token = made["token"]
    assert token.startswith("bsrc_") and made["source"]["token_hint"] == token[-2:]
    with eng.begin() as c:
        src = mon.authenticate(c, headers={"Authorization": f"Bearer {token}"}, raw=b"x",
                               kinds=("otlp",))
    name = f"checkout-{uuid4().hex[:6]}"
    ok = err = 0.0
    got = None
    for i in range(8):
        ok, err = ok + 100, err + (0 if i < 3 else 30)
        got = _ingest_otlp(eng, src, gzip.compress(_otlp_json(name, T0 + timedelta(minutes=i),
                                                              ok, err)),
                           T0 + timedelta(minutes=i), enc="gzip")
    assert "recognised (OpenTelemetry semconv)" in got["word"] and "rules proposed" in got["word"]
    with eng.begin() as c:
        svc = c.execute(text("SELECT * FROM monitored_services WHERE project_id = :p AND "
                             "name = :n"), {"p": pid, "n": name}).mappings().one()
        labels = c.execute(text("SELECT labels FROM metric_series WHERE service_id = :s LIMIT 1"),
                           {"s": svc["id"]}).scalar()
        rules = {r["kind"]: r for r in aa.rules(c, svc["id"])}
    assert svc["dialect"] == "otel" and svc["onboarding"]["first_data_at"]
    assert labels["project"] == pid, "the client's `project` label is overwritten by BOSS's"
    assert "http_server_request_duration_seconds_count" in rules["error_rate"]["expr"]
    assert 'http_response_status_code=~"5.."' in rules["error_rate"]["expr"]
    assert "health_check" not in rules, "no synthetic check: no health-check rule"
    with eng.begin() as c:
        subj = aa.subject(c, svc["id"])
        out = aa.evaluate_target(c, subj, T0 + timedelta(minutes=7))
        alert = c.execute(text("SELECT * FROM app_alerts WHERE service_id = :s AND "
                               "rule_kind = 'error_rate'"), {"s": svc["id"]}).mappings().one()
    assert out["fired"] and alert["state"] == "firing" and alert["target_id"] is None
    assert alert["incident_id"], "a service BOSS did not deploy gets an incident too"
    assert alert["value"] > 5


def test_otlp_protobuf_and_remote_write_read_the_same_series(pg):
    eng, pid = pg
    # protobuf: one monotonic cumulative sum, http.server.request.count style
    def w(num, wt, payload):
        return mi._w_field(num, wt, payload)
    kv = w(1, 2, b"service.name") + w(2, 2, w(1, 2, b"pb-svc"))
    attr = w(1, 2, b"http.response.status_code") + w(2, 2, w(3, 0, mi._w_varint(500)))
    t_ns = int(T0.timestamp() * 1e9)
    dp = w(7, 2, attr) + w(3, 1, struct.pack("<Q", t_ns)) + w(4, 1, struct.pack("<d", 7.0))
    metric = w(1, 2, b"app.errors") + w(7, 2, w(1, 2, dp) + w(2, 0, mi._w_varint(2))
                                        + w(3, 0, mi._w_varint(1)))
    body = w(1, 2, w(1, 2, w(1, 2, kv)) + w(2, 2, w(2, 2, metric)))
    pts = mi.otlp_points(mi.otlp_protobuf_to_json(body), T0)
    (res, labels, t, v, delta), = pts
    assert res["service.name"] == "pb-svc" and labels["__name__"] == "app_errors_total"
    assert labels["http_response_status_code"] == "500" and v == 7.0 and t == T0
    # remote write round trip through snappy
    rw = mi.encode_remote_write([({"__name__": "up", "job": "api"}, [(1000, 1.0), (2000, 0.0)])])
    back = mi.decode_remote_write(rw)
    assert [(lab["job"], val) for lab, _t, val in back] == [("api", 1.0), ("api", 0.0)]
    with pytest.raises(mon.IngestRefused):
        mi.decode_remote_write(b"\xff\xff not snappy")


# ── 2. Alertmanager, tokens, refusals ───────────────────────────────────────────────────────
def _am(status: str, fp: str, *, project: str | None = None) -> bytes:
    labels = {"alertname": "CheckoutDown", "service": "checkout", "severity": "critical"}
    if project:
        labels["project"] = project
    return json.dumps({"version": "4", "status": status, "alerts": [{
        "status": status, "labels": labels, "annotations": {"summary": "checkout is down"},
        "startsAt": "2026-10-18T09:00:00Z",
        "endsAt": "2026-10-18T09:10:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
        "generatorURL": "https://prom.example.com/graph", "fingerprint": fp}]}).encode()


def _receive(eng, src_id, kind, body, token=None, headers=None, now=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    return alert_ingest.receive(eng, get_settings(), source_id=src_id, kind=kind, headers=h,
                                query={}, raw=body, now=now or datetime.now(timezone.utc))


def test_alertmanager_fires_one_alert_its_resolve_resolves_it_and_refusals_are_said(pg):
    eng, pid = pg
    made = _source(eng, pid, "alertmanager")
    sid, token = made["source"]["id"], made["token"]
    fp = uuid4().hex[:16]
    first = _receive(eng, sid, "alertmanager", _am("firing", fp), token)
    again = _receive(eng, sid, "alertmanager", _am("firing", fp), token)
    assert first["status"] == "accepted" and again["status"] == "deduplicated"
    with eng.begin() as c:
        rows = c.execute(text("SELECT * FROM app_alerts WHERE source_id = :s"),
                         {"s": sid}).mappings().all()
    assert len(rows) == 1 and rows[0]["state"] == "firing" and rows[0]["incident_id"]
    assert rows[0]["external_ref"] == fp and rows[0]["eval_source"] == "alertmanager"
    _receive(eng, sid, "alertmanager", _am("resolved", fp), token)
    with eng.begin() as c:
        row = c.execute(text("SELECT state, cleared_reason FROM app_alerts WHERE source_id = :s"),
                        {"s": sid}).mappings().one()
    assert row["state"] == "resolved" and row["cleared_reason"] == "source_resolved"

    # a wrong token, a body naming another project: refused in words, and counted
    with pytest.raises(mon.IngestRefused) as wrong:
        _receive(eng, sid, "alertmanager", _am("firing", "x1"), "bsrc_wrong")
    assert wrong.value.status == 401 and "token" in wrong.value.word
    with pytest.raises(mon.IngestRefused) as other:
        _receive(eng, sid, "alertmanager", _am("firing", "x2", project="p_someone_else"), token)
    assert other.value.status == 403 and "project comes from the source's token" in \
        other.value.word
    with eng.begin() as c:
        view = mon.source_view(c, pid, sid)
    assert view["accepted_24h"] >= 3 and view["refused_24h"] >= 2
    shown = json.dumps(view, default=str)
    assert token not in shown and token[:12] not in shown and view["token_hint"] == token[-2:], \
        "the console sees a 2-character hint, never the token"
    assert view["last_refusal_word"]


def test_a_signed_body_is_taken_once_and_a_rotated_token_works_for_an_hour(pg):
    eng, pid = pg
    made = _source(eng, pid, "generic", require_signature=True)
    sid, token = made["source"]["id"], made["token"]
    body = json.dumps({"event_action": "trigger", "dedup_key": "k1",
                       "payload": {"summary": "queue backed up", "source": "worker",
                                   "severity": "warning"}}).encode()
    ts = str(int(time.time()))
    sig = {"X-BOSS-Timestamp": ts, "X-BOSS-Signature": mon.sign(token, ts, body)}
    assert _receive(eng, sid, "events", body, headers=sig)["status"] == "accepted"
    with pytest.raises(mon.IngestRefused) as replay:
        _receive(eng, sid, "events", body, headers=sig)
    assert replay.value.status == 409 and "replay" in replay.value.word
    stale = str(int(time.time()) - 900)
    with pytest.raises(mon.IngestRefused) as old:
        _receive(eng, sid, "events", body, headers={
            "X-BOSS-Timestamp": stale, "X-BOSS-Signature": mon.sign(token, stale, body)})
    assert "5 minutes" in old.value.word
    with pytest.raises(mon.IngestRefused):
        _receive(eng, sid, "events", body, token=token)      # must sign: a bearer is not enough
    # rotation: the old token keeps working for one hour, then stops
    plain = _source(eng, pid, "generic")
    pid_src, old_token = plain["source"]["id"], plain["token"]
    with eng.begin() as c:
        new = mon.rotate_source(c, pid, pid_src, actor="usr_t", now=datetime.now(timezone.utc))
    ev = json.dumps({"event_action": "trigger", "dedup_key": "r1",
                     "payload": {"summary": "s", "source": "x"}}).encode()
    assert _receive(eng, pid_src, "events", ev, old_token)["status"] == "accepted"
    assert _receive(eng, pid_src, "events", ev, new["token"])["status"] == "deduplicated"
    with pytest.raises(mon.IngestRefused):
        _receive(eng, pid_src, "events", ev, old_token,
                 now=datetime.now(timezone.utc) + timedelta(hours=2))


def test_every_tool_shape_reads_as_one_event():
    import boss_engine.services.alert_adapters as ada

    pd, = ada.pagerduty({}, json.dumps({"routing_key": "x", "event_action": "trigger",
                                        "payload": {"summary": "disk full", "source": "db-1",
                                                    "severity": "critical"}}).encode(), {})
    assert pd.action == "trigger" and pd.severity == "critical" and pd.service == "db-1"
    with pytest.raises(ada.AdapterRefused):
        ada.pagerduty({}, b'{"event_action": "resolve"}', {})       # a resolve needs its key
    dd, = ada.datadog({}, json.dumps({"alert_id": "9", "aggreg_key": "a", "transition":
                                      "Recovered", "title": "lat", "tags": "service:api,env:prod"}
                                     ).encode(), {})
    assert dd.action == "resolve" and dd.service == "api" and dd.environment == "prod"
    se, = ada.sentry({"Sentry-Hook-Resource": "issue"}, json.dumps({
        "action": "resolved", "data": {"issue": {"id": "77", "title": "TypeError",
                                                 "project": {"slug": "web"}}}}).encode(), {})
    assert se.action == "resolve" and se.external_ref == "issue:77" and se.service == "web"
    msg = {"AlarmName": "HighCPU", "NewStateValue": "ALARM", "AlarmArn": "arn:aws:cw:1",
           "Trigger": {"Dimensions": [{"name": "ServiceName", "value": "orders"}]}}
    cw, = ada.cloudwatch({}, json.dumps({"Type": "Notification",
                                         "Message": json.dumps(msg)}).encode(), {})
    assert cw.action == "trigger" and cw.service == "orders"
    assert ada.cloudwatch({}, json.dumps({"Type": "Notification", "Message": json.dumps(
        {**msg, "NewStateValue": "INSUFFICIENT_DATA"})}).encode(), {}) == [], \
        "no data never resolves anything"
    assert not alert_ingest.verify_sns({"Type": "Notification", "SigningCertURL":
                                        "https://evil.example.com/cert.pem"}), \
        "a certificate off AWS's SNS hosts is never trusted"


def test_a_quiet_source_resolves_after_24_hours_and_says_so(pg):
    eng, pid = pg
    made = _source(eng, pid, "alertmanager")
    then = datetime.now(timezone.utc) - timedelta(hours=25)
    _receive(eng, made["source"]["id"], "alertmanager", _am("firing", uuid4().hex[:12]),
             made["token"], now=then)
    with eng.begin() as c:
        assert aa.quiet_resolve(c) >= 1
        row = c.execute(text("SELECT a.cleared_reason, t.reason FROM app_alerts a JOIN "
                             "alert_transitions t ON t.alert_id = a.id AND t.to_state = "
                             "'resolved' WHERE a.source_id = :s"),
                        {"s": made["source"]["id"]}).mappings().one()
    assert row["cleared_reason"] == "source_quiet" and "went quiet" in row["reason"]


# ── heartbeats, synthetics, tenancy ─────────────────────────────────────────────────────────
def test_a_missed_heartbeat_fires_its_absence_rule(pg):
    eng, pid = pg
    made = _source(eng, pid, "heartbeat", name=f"nightly-{uuid4().hex[:4]}", heartbeat_every_s=300)
    t = datetime.now(timezone.utc)
    _receive(eng, made["source"]["id"], "heartbeat", b"", made["token"], now=t)
    with eng.begin() as c:
        sid = c.execute(text("SELECT service_id FROM signal_sources WHERE id = :i"),
                        {"i": made["source"]["id"]}).scalar()
        subj = aa.subject(c, sid)
        assert not aa.evaluate_target(c, subj, t + timedelta(minutes=2))["fired"]
        fired = aa.evaluate_target(c, subj, t + timedelta(minutes=6))["fired"]
    assert fired, "no ping for longer than its interval: the dead-man's switch fires"


def test_synthetics_never_probe_a_private_address_that_is_not_a_registered_service(pg):
    from boss_engine.services import monitor_scheduler as ms

    eng, pid = pg
    with eng.begin() as c:
        got = ms.check_http(c, pid, "http://127.0.0.1:9/admin")
        meta = ms.check_http(c, pid, "http://169.254.169.254/latest/meta-data")
    assert got["ok"] is False and got.get("refused") and "private" in got["word"]
    assert meta.get("refused"), "the cloud metadata address is never probed"


def test_a_rule_written_to_read_another_projects_series_reads_none(pg):
    eng, pid = pg
    with eng.begin() as c:
        other = project(c)
        mine = mon.find_or_create_service(c, pid, "ten-a")
        theirs = mon.find_or_create_service(c, other, "ten-b")
        for svc, proj in ((mine, pid), (theirs, other)):
            mon.write_points(c, proj, svc, [(mon.stamp({"__name__": "queue_depth"},
                                                       project_id=proj, svc=svc), T0, 99.0)])
        store = mon.floor_store(c, pid, T0, 60_000)
    expr = f'queue_depth{{project="{other}"}} > 1'
    assert pq.instant_vector(expr, store, int(T0.timestamp() * 1000)) == [], \
        "the floor holds only the project's own series"
    scoped = pq.inject_scope(expr, {"project": pid})
    assert f'project="{pid}"' in scoped and other not in scoped, \
        "sent to a shared store, the scope replaces what the query said, on the parsed query"
    with pytest.raises(pq.PromQLUnsupported):
        pq.inject_scope("max_over_time(queue_depth[10m:1m])", {"project": pid})


# ── 11. the reliability door is the same pipeline ──────────────────────────────────────────
def test_the_deprecated_reliability_door_lands_in_the_same_alerts_and_incidents(pg):
    eng, pid = pg
    made = _source(eng, pid, "generic")
    kind, body = alert_ingest.legacy_kind(json.dumps(
        {"title": "PaymentsDown", "service": "payments", "severity": "critical"}).encode())
    got = alert_ingest.receive(eng, get_settings(), source_id=made["source"]["id"], kind=kind,
                               headers={"Authorization": f"Bearer {made['token']}"}, query={},
                               raw=body)
    with eng.begin() as c:
        row = c.execute(text("SELECT a.*, i.status AS inc FROM app_alerts a JOIN incidents i "
                             "ON i.id = a.incident_id WHERE a.id = :a"),
                        {"a": got["alert_id"]}).mappings().one()
        claimed = aa._claim(eng, {pid}, 50)
    assert row["source_kind"] == "external" and row["state"] == "firing"
    assert row["triage_state"] == "pending" and got["alert_id"] in claimed, \
        "the same triage queue as an app alert"
