"""App alerts (BOSSAPP-598): rules a person can change, alerts deduped, grouped, held back and
silenced Alertmanager-style, and every fired alert triaged by a model — never by keywords.

Each test stages the breach the way the monitor would see it: scrapes in app_metrics_scrapes
(cumulative counters, as scraped), probes in deployment_health_samples, deploys in
stack_deployments — then evaluates the app (app_alerts.evaluate_target) at a chosen clock.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.config import get_settings
from boss_engine.services import app_alerts as aa
from boss_engine.services import app_alerts_view as view

T0 = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _app(c, pid: str, *, manifest: dict | None = None, name: str = "Order API") -> dict:
    tid = f"t_al_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url) VALUES (:t, :p, :a, 'acme/shop', "
        " 'staging', 'stack', 'order-api', 'active', 'test', '[]'::jsonb, "
        " 'http://203.0.113.10:8000/')"), {"t": tid, "p": pid, "a": name})
    _deploy(c, pid, tid, manifest or {"metrics": {"path": "/metrics"}}, at=T0 - timedelta(hours=3))
    return dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                          {"t": tid}).mappings().one())


def _deploy(c, pid, tid, manifest, *, at, status="healthy", commit=None):
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        " manifest, status, created_by, created_at, finished_at) VALUES (:i, :p, :t, 'acme/shop', "
        " 'main', :c, CAST(:m AS jsonb), :s, 'usr_op', :at, :at)"),
        {"i": f"sd_{uuid4().hex[:10]}", "p": pid, "t": tid, "c": commit or "6bf2673" + "0" * 33,
         "m": json.dumps(manifest), "s": status, "at": at})


def _scrape(c, t: dict, at: datetime, requests: float, errors: float,
            buckets: dict[str, float] | None = None):
    snap = {"requests": requests, "errors": errors, "routes": {},
            "buckets": buckets or {}, "hist_count": (buckets or {}).get("+Inf"),
            "hist_sum": None}
    c.execute(text(
        "INSERT INTO app_metrics_scrapes (id, project_id, deployment_target_id, scraped_at, ok, "
        " word, snapshot) VALUES (:i, :p, :t, :at, true, 'Scraped', CAST(:s AS jsonb))"),
        {"i": f"ams_{uuid4().hex}", "p": t["project_id"], "t": t["id"], "at": at,
         "s": json.dumps(snap)})


def _probe(c, t: dict, at: datetime, ok: bool):
    c.execute(text(
        "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, checked_at, "
        " ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, :ok, :sc, 40)"),
        {"i": f"dhs_{uuid4().hex}", "p": t["project_id"], "t": t["id"], "at": at, "ok": ok,
         "sc": 200 if ok else 503})


def _traffic(c, t, start: datetime, minutes: int, *, per_min: float, err_per_min: float,
             from_requests: float = 0, from_errors: float = 0) -> tuple[float, float]:
    """One scrape a minute: `per_min` requests, `err_per_min` of them 5xx."""
    req, err = from_requests, from_errors
    for i in range(minutes + 1):
        _scrape(c, t, start + timedelta(minutes=i), req, err)
        req += per_min
        err += err_per_min
    return req - per_min, err - err_per_min


def _eval(eng, t, at):
    with eng.begin() as c:
        return aa.evaluate_target(c, t, at)


def _alerts(eng, t, kind=None):
    with eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM app_alerts WHERE target_id = :t"
            + (" AND rule_kind = :k" if kind else "") + " ORDER BY active_at"),
            {"t": t["id"], **({"k": kind} if kind else {})}).mappings().all()]


def _rule(eng, t, kind) -> dict:
    with eng.begin() as c:
        return next(r for r in aa.rules(c, t["id"]) if r["kind"] == kind)


# ── rules: the yaml starts them, the console wins, and both are said ────────────────────────
def test_rules_start_from_the_yaml_and_a_console_edit_wins(pg):
    eng, pid = pg
    manifest = {"metrics": {"path": "/metrics", "alert": {"error_rate_percent": 7,
                                                          "window_minutes": 2}},
                "alerts": {"latency_p95": {"threshold": 500}}}
    with eng.begin() as c:
        t = _app(c, pid, manifest=manifest)
        rules = {r["kind"]: r for r in aa.ensure_rules(c, t)}
    assert set(rules) == set(aa.KINDS)
    er = rules["error_rate"]
    assert er["source"] == "yaml" and er["params"]["threshold"] == 7.0
    assert "[2m]" in er["expr"] and "> 7 and" in er["expr"]
    assert rules["latency_p95"]["params"]["threshold"] == 500
    assert rules["no_traffic"]["enabled"] is False, "no traffic is off until someone wants it"

    with eng.begin() as c:
        edited = aa.update_rule(c, t, er["id"], {"params": {"threshold": 3}}, actor="usr_param",
                                reason="checkout is critical")
        changes = c.execute(text("SELECT change, before, after, reason, changed_by FROM "
                                 "app_alert_rule_changes WHERE rule_id = :r"),
                            {"r": er["id"]}).mappings().all()
    assert edited["source"] == "console" and edited["changed_by"] == "usr_param"
    assert "> 3 and" in edited["expr"]
    assert len(changes) == 1 and changes[0]["change"] == "edited"
    assert changes[0]["before"]["params"]["threshold"] == 7.0
    assert changes[0]["after"]["params"]["threshold"] == 3.0
    assert changes[0]["reason"] == "checkout is critical"

    # the next deploy's yaml changes both rules: the edited one keeps the person's value
    manifest["metrics"]["alert"]["error_rate_percent"] = 9
    manifest["alerts"]["latency_p95"]["threshold"] = 650
    with eng.begin() as c:
        _deploy(c, pid, t["id"], manifest, at=T0 - timedelta(hours=1))
        after = {r["kind"]: r for r in aa.ensure_rules(c, t)}
    assert after["error_rate"]["params"]["threshold"] == 3
    assert after["latency_p95"]["params"]["threshold"] == 650

    with eng.begin() as c, pytest.raises(aa.RuleRefused):
        aa.update_rule(c, t, er["id"], {"params": {"threshold": 400}}, actor="usr_param")
    with eng.begin() as c, pytest.raises(aa.RuleRefused):
        aa.update_rule(c, t, er["id"], {"expr": "vector(1)"}, actor="usr_param")
    with eng.begin() as c, pytest.raises(aa.RuleRefused):
        aa.update_rule(c, t, None, {"alert": "Bad", "expr": "sum(rate(x[5m])"}, actor="u")


def test_the_next_breach_fires_at_the_new_value(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        _traffic(c, t, T0, 6, per_min=100, err_per_min=4)          # 4% failing
    at = T0 + timedelta(minutes=6)
    out = _eval(eng, t, at)
    er = _rule(eng, t, "error_rate")
    assert out["rules"][er["id"]] == "ok" and er["last_value"] == pytest.approx(4.0, abs=0.5)
    assert _alerts(eng, t, "error_rate") == []
    with eng.begin() as c:
        aa.update_rule(c, t, er["id"], {"params": {"threshold": 3}}, actor="usr_param")
    with eng.begin() as c:
        _traffic(c, t, at + timedelta(minutes=1), 0, per_min=100, err_per_min=4,
                 from_requests=700, from_errors=28)
    out = _eval(eng, t, at + timedelta(minutes=1))
    fired = _alerts(eng, t, "error_rate")
    assert len(fired) == 1 and fired[0]["state"] == "firing"
    assert fired[0]["threshold"] == 3 and fired[0]["value"] == pytest.approx(4.0, abs=0.5)
    assert fired[0]["expr"].endswith(">= 20") and "> 3 and" in fired[0]["expr"]


# ── each built-in fires on a staged breach and clears on recovery ───────────────────────────
def test_latency_health_and_no_traffic_fire_and_clear(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
    nt = _rule(eng, t, "no_traffic")
    with eng.begin() as c:
        aa.update_rule(c, t, nt["id"], {"enabled": True}, actor="usr_param")

    # LATENCY: 100 req/min; from minute 3 most of them take 0.5-1 s
    with eng.begin() as c:
        fast, slow = {"0.1": 95, "1": 99, "+Inf": 100}, {"0.1": 20, "1": 98, "+Inf": 100}
        cum = {k: 0.0 for k in fast}
        for i in range(9):
            step = fast if i < 3 or i > 6 else slow
            _scrape(c, t, T0 + timedelta(minutes=i), 100 * i, 0, dict(cum))
            cum = {k: cum[k] + step[k] for k in cum}
    _eval(eng, t, T0 + timedelta(minutes=2))
    assert _alerts(eng, t, "latency_p95") == []
    _eval(eng, t, T0 + timedelta(minutes=6))
    slow_alert = _alerts(eng, t, "latency_p95")
    assert slow_alert[0]["state"] == "firing" and slow_alert[0]["value"] > 800
    assert slow_alert[0]["unit"] == "ms"
    # recovered on the shape of the window (5 min of fast traffic later)
    with eng.begin() as c:
        for i in range(9, 16):
            _scrape(c, t, T0 + timedelta(minutes=i), 100 * i, 0, dict(cum))
            cum = {k: cum[k] + fast[k] for k in cum}
    _eval(eng, t, T0 + timedelta(minutes=15))
    assert _alerts(eng, t, "latency_p95")[0]["state"] == "resolved"
    assert _alerts(eng, t, "latency_p95")[0]["cleared_reason"] == "recovered"

    # NO TRAFFIC: the counter stops moving for 15 minutes, then moves again
    with eng.begin() as c:
        for i in range(16, 33):
            _scrape(c, t, T0 + timedelta(minutes=i), 1500, 0, dict(cum))
    _eval(eng, t, T0 + timedelta(minutes=32))
    quiet = _alerts(eng, t, "no_traffic")
    assert quiet and quiet[0]["state"] == "firing"
    with eng.begin() as c:
        for i in range(33, 50):
            _scrape(c, t, T0 + timedelta(minutes=i), 1500 + 50 * (i - 32), 0, dict(cum))
    _eval(eng, t, T0 + timedelta(minutes=49))
    assert _alerts(eng, t, "no_traffic")[0]["state"] == "resolved"

    # HEALTH CHECK: three failed probes in ten minutes, then it answers again
    with eng.begin() as c:
        for i, ok in enumerate([True, False, False, False]):
            _probe(c, t, T0 + timedelta(minutes=50 + 2 * i), ok)
    _eval(eng, t, T0 + timedelta(minutes=57))
    assert _alerts(eng, t, "health_check") == [], "a success in the window: not all failing"
    _eval(eng, t, T0 + timedelta(minutes=60))
    down = _alerts(eng, t, "health_check")
    assert down and down[0]["state"] == "firing" and down[0]["value"] == 3
    with eng.begin() as c:
        _probe(c, t, T0 + timedelta(minutes=63), True)
    _eval(eng, t, T0 + timedelta(minutes=63))
    assert _alerts(eng, t, "health_check")[0]["state"] == "resolved"


# ── the record: fingerprint dedupe, groups, inhibition, silences ────────────────────────────
def test_a_refiring_alert_is_the_same_alert(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        req, err = _traffic(c, t, T0, 6, per_min=100, err_per_min=20)
    _eval(eng, t, T0 + timedelta(minutes=5))
    _eval(eng, t, T0 + timedelta(minutes=6))
    rows = _alerts(eng, t, "error_rate")
    assert len(rows) == 1 and rows[0]["fire_count"] == 1, "still firing: one alert, updated"
    fp = rows[0]["fingerprint"]
    assert fp == aa.fingerprint(t["id"], "AppErrorRate", {})
    # recovers (healthy traffic) ... then fails again two minutes later: a flap is ONE alert
    with eng.begin() as c:
        req, err = _traffic(c, t, T0 + timedelta(minutes=7), 6, per_min=100, err_per_min=0,
                            from_requests=req + 100, from_errors=err + 20)
    _eval(eng, t, T0 + timedelta(minutes=13))
    assert _alerts(eng, t, "error_rate")[0]["state"] == "resolved"
    with eng.begin() as c:
        req, err = _traffic(c, t, T0 + timedelta(minutes=14), 2, per_min=100, err_per_min=60,
                            from_requests=req + 100, from_errors=err)
    _eval(eng, t, T0 + timedelta(minutes=16))
    rows = _alerts(eng, t, "error_rate")
    assert len(rows) == 1 and rows[0]["state"] == "firing" and rows[0]["fire_count"] == 2
    # ...but long after it cleared, a new breach is a new alert
    with eng.begin() as c:
        req, err = _traffic(c, t, T0 + timedelta(minutes=17), 6, per_min=100, err_per_min=0,
                            from_requests=req + 100, from_errors=err + 60)
    _eval(eng, t, T0 + timedelta(minutes=23))
    with eng.begin() as c:
        _traffic(c, t, T0 + timedelta(minutes=40), 5, per_min=100, err_per_min=50,
                 from_requests=req + 100, from_errors=err)
    _eval(eng, t, T0 + timedelta(minutes=45))
    rows = _alerts(eng, t, "error_rate")
    assert len(rows) == 2 and [r["state"] for r in rows] == ["resolved", "firing"]


def test_breaches_with_one_runbook_become_one_incident(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        cum, slow = {"0.1": 0.0, "1": 0.0, "+Inf": 0.0}, {"0.1": 10, "1": 50, "+Inf": 100}
        for i in range(6):
            _scrape(c, t, T0 + timedelta(minutes=i), 100 * i, 30 * i, dict(cum))
            cum = {k: cum[k] + slow[k] for k in cum}
    out = _eval(eng, t, T0 + timedelta(minutes=5))
    er, lat = _alerts(eng, t, "error_rate")[0], _alerts(eng, t, "latency_p95")[0]
    assert er["incident_id"] and er["incident_id"] == lat["incident_id"], \
        "error rate and latency share the serving runbook: one incident, both alerts in it"
    assert [i["action"] for i in out["incidents"]] == ["opened", "joined"]
    with eng.begin() as c:
        key = c.execute(text("SELECT dedupe_key FROM incidents WHERE id = :i"),
                        {"i": er["incident_id"]}).scalar()
    assert key == "app_error_rate:" + t["id"], "the serving group is the app's error incident"


def test_a_failing_health_check_holds_back_the_symptoms(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        _traffic(c, t, T0, 6, per_min=100, err_per_min=40)
        for i in range(4):
            _probe(c, t, T0 + timedelta(minutes=1 + i), False)
    _eval(eng, t, T0 + timedelta(minutes=6))
    health, errs = _alerts(eng, t, "health_check")[0], _alerts(eng, t, "error_rate")[0]
    assert health["state"] == errs["state"] == "firing"
    assert errs["inhibited_by"] == health["id"] and errs["incident_id"] is None
    assert health["incident_id"], "the cause opens its own (availability) incident"
    with eng.begin() as c:
        claimed = aa._claim(eng, {pid}, 10)
    assert errs["id"] not in claimed, "a held-back symptom is not triaged"
    # the deploy failing its health check is a cause too
    with eng.begin() as c:
        _deploy(c, pid, t["id"], {"metrics": {"path": "/metrics"}}, status="failed",
                at=T0 + timedelta(minutes=6, seconds=30))
    _eval(eng, t, T0 + timedelta(minutes=7))
    assert _alerts(eng, t, "deploy_health")[0]["state"] == "firing"


def test_a_silence_has_a_reason_and_is_audited(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        aa.ensure_rules(c, t)
        _traffic(c, t, T0, 6, per_min=100, err_per_min=30)
    er = _rule(eng, t, "error_rate")
    with eng.begin() as c, pytest.raises(aa.RuleRefused):
        aa.silence(c, t, er["id"], minutes=60, reason="", actor="usr_param")
    with eng.begin() as c:
        s = aa.silence(c, t, er["id"], minutes=60, reason="load test until 9", actor="usr_param",
                       now=T0)
    out = _eval(eng, t, T0 + timedelta(minutes=6))
    row = _alerts(eng, t, "error_rate")[0]
    assert row["silenced_by"] == s["id"] and row["incident_id"] is None
    assert out["rules"][er["id"]] == "silenced"
    with eng.begin() as c:
        aa.end_silence(c, t, s["id"], actor="usr_param", now=T0 + timedelta(minutes=6))
        log = c.execute(text("SELECT change, reason FROM app_alert_rule_changes WHERE rule_id = :r "
                             "ORDER BY changed_at"), {"r": er["id"]}).all()
    assert [r[0] for r in log] == ["silenced", "unsilenced"] and log[0][1] == "load test until 9"
    _eval(eng, t, T0 + timedelta(minutes=6))
    assert _alerts(eng, t, "error_rate")[0]["incident_id"], "the silence ended: it groups now"


# ── triage: a model decides, and its words are kept ────────────────────────────────────────
class Model:
    """A chat adapter that answers one decision, and records what it was shown."""

    def __init__(self, answer: dict):
        self.answer, self.prompts = answer, []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return json.dumps(self.answer)


class NoModel:
    def create_text_response(self, prompt, system_prompt=None):
        raise RuntimeError("no model is connected")


def _fired(eng, pid):
    with eng.begin() as c:
        t = _app(c, pid, name=f"Order API {uuid4().hex[:4]}")
        _traffic(c, t, T0, 6, per_min=100, err_per_min=30)
    _eval(eng, t, T0 + timedelta(minutes=6))
    return t, _alerts(eng, t, "error_rate")[0]


def test_the_agent_triages_with_a_model_and_its_words_are_kept(pg):
    eng, pid = pg
    t, alert = _fired(eng, pid)
    words = ("App-side: release 6bf2673 broke POST /orders 1 min before the alert; "
             "30% of checkouts fail.")
    model = Model({"action": "open_incident", "confidence": 0.92, "reason": words})
    got = aa.triage_alert(eng, get_settings(), alert["id"], adapter=model)
    assert got["outcome"] == "opened" and got["reason"] == words
    row = _alerts(eng, t, "error_rate")[0]
    assert row["triage_state"] == "done" and row["triage"]["reason"] == words
    # the model was shown the breach, the release, the incidents — context, not keywords
    shown = model.prompts[0]
    assert "AppErrorRate" in shown and "serving_release" in shown and "open_incidents" in shown
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                           {"i": row["incident_id"]}).scalar()
        note = c.execute(text("SELECT payload FROM incident_evidence WHERE incident_id = :i "
                              "AND kind = 'note'"), {"i": row["incident_id"]}).scalar()
    assert status == "triaged" and words in note["text"]
    with eng.begin() as c:
        tab = view.app_alerts_tab(c, get_settings(), pid, t["id"])
    assert tab["history"][0]["triage"]["reason"] == words
    assert tab["history"][0]["triage"]["word"] == "Opened an incident"


def test_noise_closes_nothing_silently_and_unsure_asks(pg):
    eng, pid = pg
    t, alert = _fired(eng, pid)
    aa.triage_alert(eng, get_settings(), alert["id"], adapter=Model(
        {"action": "noise", "confidence": 0.9, "reason": "Deploy restart; cleared in a minute."}))
    row = _alerts(eng, t, "error_rate")[0]
    assert row["triage"]["outcome"] == "noise" and row["state"] == "firing"
    with eng.begin() as c:
        inc = c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                        {"i": row["incident_id"]}).scalar()
        why = c.execute(text("SELECT payload->>'text' FROM incident_evidence WHERE "
                             "incident_id = :i AND kind = 'note'"),
                        {"i": row["incident_id"]}).scalar()
    assert inc == "dismissed" and "Deploy restart" in why, "dismissed, with the agent's reason"

    t2, alert2 = _fired(eng, pid)
    aa.triage_alert(eng, get_settings(), alert2["id"], adapter=Model(
        {"action": "open_incident", "confidence": 0.3, "reason": "maybe",
         "question": "Was a load test running on Order API at 07:00?"}))
    row2 = _alerts(eng, t2, "error_rate")[0]
    assert row2["triage"]["outcome"] == "asked"
    assert row2["triage"]["question"] == "Was a load test running on Order API at 07:00?"

    t3, alert3 = _fired(eng, pid)
    got = aa.triage_alert(eng, get_settings(), alert3["id"], adapter=NoModel())
    row3 = _alerts(eng, t3, "error_rate")[0]
    assert got["word"] == aa.NO_MODEL_WORD and row3["triage_state"] == "no_model"
    assert row3["incident_id"], "no model: no triage, said plainly, and the incident stays"


def test_the_agent_can_move_an_alert_into_another_incident(pg):
    eng, pid = pg
    t1, a1 = _fired(eng, pid)
    t2, a2 = _fired(eng, pid)
    target_inc = a1["incident_id"]
    got = aa.triage_alert(eng, get_settings(), a2["id"], adapter=Model(
        {"action": f"join:{target_inc}", "confidence": 0.85,
         "reason": "Same database outage as the other app."}))
    assert got["outcome"] == "joined" and got["incident_id"] == target_inc
    row = _alerts(eng, t2, "error_rate")[0]
    assert row["incident_id"] == target_inc
    with eng.begin() as c:
        old = c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                        {"i": a2["incident_id"]}).scalar()
    assert old == "dismissed", "its own incident had nothing else in it"


def test_respond_automatically_starts_the_response_on_the_rules_grant(pg, monkeypatch):
    eng, pid = pg
    t, alert = _fired(eng, pid)
    er = _rule(eng, t, "error_rate")
    with eng.begin() as c, pytest.raises(aa.RuleRefused):
        aa.update_rule(c, t, er["id"], {"auto_respond": True, "triage": False}, actor="u")
    with eng.begin() as c:
        on = aa.update_rule(c, t, er["id"], {"auto_respond": True}, actor="usr_param")
    assert on["auto_respond"] is True
    started = []
    from boss_engine.services import event_dispatch

    def fake_start(connection, **kw):
        started.append(kw)
        return {"run_id": "wfr_fake", "word": "Started"}
    monkeypatch.setattr(event_dispatch, "start_workflow_on_grant", fake_start)
    got = aa.triage_alert(eng, get_settings(), alert["id"], adapter=Model(
        {"action": "open_incident", "confidence": 0.95, "reason": "App-side regression."}))
    assert got["response"] == {"run_id": "wfr_fake", "word": "Responding"}
    assert started[0]["workflow_key"] == "incident_response"
    assert started[0]["input_data"]["incident_id"] == alert["incident_id"]
    assert started[0]["input_data"]["directive"].startswith("Respond to the incident")
    assert _alerts(eng, t, "error_rate")[0]["response_run_id"] == "wfr_fake"
    # a rule without Respond automatically starts nothing
    t2, alert2 = _fired(eng, pid)
    aa.triage_alert(eng, get_settings(), alert2["id"], adapter=Model(
        {"action": "open_incident", "confidence": 0.95, "reason": "x"}))
    assert len(started) == 1


# ── the history starts with what already happened ───────────────────────────────────────────
def _backfill_sql() -> list[str]:
    schema = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    body = schema.read_text()
    start = body.index("-- THE ALERT HISTORY STARTS WITH WHAT ALREADY HAPPENED.")
    end = body.index("ON CONFLICT DO NOTHING;", body.index("-- ...and every report an app sent"))
    block = body[start:end + len("ON CONFLICT DO NOTHING;")]
    block = re.sub(r"--[^\n]*", "", block)
    return [s for s in re.split(r";\s*\n(?=(?:CREATE|INSERT))", block) if s.strip()]


def test_the_backfill_seeds_the_history_and_writes_the_rule_the_engine_writes(pg):
    eng, pid = pg
    rule = {"enabled": True, "min_requests": 20, "window_minutes": 2, "error_rate_percent": 5.0}
    at = lambda h, m, s=0: datetime(2026, 10, 18, h, m, s, tzinfo=timezone.utc)  # noqa: E731
    with eng.begin() as c:
        t = _app(c, pid)
        reporter = _app(c, pid, name="helpdesk-ai")
        inc = f"inc_{uuid4().hex[:10]}"
        c.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, service, status, "
            " first_seen, last_seen, finalize_ctx) VALUES (:i, :p, :k, 'Order API: 14.4%', "
            " 'Order API', 'resolved', :a, :b, CAST(:ctx AS jsonb))"),
            {"i": inc, "p": pid, "k": "app_error_rate:" + t["id"], "a": at(6, 59), "b": at(7, 17),
             "ctx": json.dumps({"app": {"app_name": "Order API", "environment": "staging",
                                        "target_id": t["id"]}})})
        ev = [("customer_reports", at(6, 59, 54), {"title": "Checkout error"}),
              ("alert", at(7, 5, 53), {"rule": rule, "error_rate": 0.1441}),
              ("deployment", at(7, 5, 53), {"commit_sha": "6bf2673" + "0" * 33,
                                             "ref": "6bf26734d875"}),
              ("rca_hypothesis", at(7, 6, 15), {"root_cause": "Release 6bf2673 broke pricing.",
                                                 "confidence": 0.9}),
              ("monitor_resolved", at(7, 12, 43), {}),
              ("alert", at(7, 13, 4), {"rule": rule, "error_rate": 0.1667}),
              ("monitor_resolved", at(7, 14, 34), {}),
              # written by the live monitor: carries its alert id, never copied again
              ("alert", at(18, 43, 52), {"rule": rule, "error_rate": 0.12, "alert_id": "alrt_x"})]
        for kind, when, payload in ev:
            c.execute(text("INSERT INTO incident_evidence (id, incident_id, kind, payload, "
                           "created_at) VALUES (:i, :inc, :k, CAST(:p AS jsonb), :at)"),
                      {"i": f"ev_{uuid4().hex[:10]}", "inc": inc, "k": kind,
                       "p": json.dumps(payload), "at": when})
        c.execute(text(
            "INSERT INTO app_signal_receipts (id, project_id, reporter_target_id, signal_id, "
            " incident_id, via, payload, received_at) VALUES (:i, :p, :r, 'sig-1', :inc, 'pull', "
            " CAST(:pl AS jsonb), :at)"),
            {"i": f"sgr_{uuid4().hex[:8]}", "p": pid, "r": reporter["id"], "inc": inc,
             "pl": json.dumps({"title": "7 checkout complaints", "count": 7}),
             "at": at(6, 59, 54)})
        for stmt in _backfill_sql() * 2:                     # twice: it runs on every boot
            c.execute(text(stmt.replace(":", r"\:")))
        rows = [dict(r) for r in c.execute(text(
            "SELECT * FROM app_alerts WHERE incident_id = :i ORDER BY active_at"),
            {"i": inc}).mappings().all()]
    signal, alert = rows[0], rows[1]
    assert len(rows) == 2, "the 07:05 alert and its 07:13 re-fire are one alert; one report"
    assert alert["fire_count"] == 2 and alert["state"] == "resolved"
    assert alert["value"] == pytest.approx(14.41) and alert["threshold"] == 5.0
    assert alert["cleared_at"] == at(7, 14, 34) and alert["cleared_reason"] == "recovered"
    assert alert["triage"]["reason"] == "Release 6bf2673 broke pricing."
    assert alert["release"]["commit_sha"].startswith("6bf2673")
    assert alert["expr"] == aa.render("error_rate", {"threshold": 5.0, "window_minutes": 2,
                                                     "min_requests": 20},
                                      "Order API", "staging")["expr"]
    assert alert["fingerprint"] == aa.fingerprint(t["id"], "AppErrorRate", {})
    assert signal["source_kind"] == "signal" and signal["reporter_target_id"] == reporter["id"]
    with eng.begin() as c:
        tab = view.app_alerts_tab(c, get_settings(), pid, t["id"], since="30d")
    shown = {a["id"]: a for a in tab["history"]}
    assert shown[alert["id"]]["incident"]["id"] == inc
    assert shown[alert["id"]]["triage"]["reason"] == "Release 6bf2673 broke pricing."
    assert shown[signal["id"]]["summary"].startswith("helpdesk-ai: ")


# ── Grafana: a link only after BOSS checked that it answers ─────────────────────────────────
def test_a_grafana_link_only_after_it_answered(pg, monkeypatch):
    from boss_engine.services import observability_stack as os_

    eng, pid = pg
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO telemetry_sources (id, project_id, kind, name, config, enabled, "
            " created_at) VALUES (:i, :p, 'prometheus', 'boss-prometheus', CAST(:c AS jsonb), "
            " true, now())"), {"i": f"ts_{pid}", "p": pid,
                               "c": json.dumps({"base_url": "http://203.0.113.20:9091"})})
        unchecked = os_.endpoint_state(c, pid, "grafana")
    assert unchecked["state"] == "unchecked" and unchecked["url"] is None
    assert os_.dashboard_link(unchecked, "Order API") == (None, "BOSS hasn't checked Grafana yet")

    # the VM's port does not answer: said, never linked (the `existing is None` hole)
    monkeypatch.setattr(os_, "provisioned_dashboards", lambda base, timeout=4.0: None)
    monkeypatch.setattr(os_, "_get_json", lambda url, timeout: (_ for _ in ()).throw(
        ConnectionRefusedError()))
    got = os_.check_endpoint(eng, pid, "grafana")
    assert got["state"] == "not_answering" and got["base_url"] == "http://203.0.113.20:3006"
    with eng.begin() as c:
        down = os_.endpoint_state(c, pid, "grafana")
    assert down["url"] is None and down["reason"] == "Grafana isn't answering on its host"
    assert down["why"] == "connection refused"
    assert os_.dashboard_link(down, "Order API")[0] is None

    monkeypatch.setattr(os_, "provisioned_dashboards",
                        lambda base, timeout=4.0: {os_.dashboard_uid("Order API")})
    os_.check_endpoint(eng, pid, "grafana")
    with eng.begin() as c:
        up = os_.endpoint_state(c, pid, "grafana")
    assert up["state"] == "answering" and up["url"] == "http://203.0.113.20:3006"
    assert os_.dashboard_link(up, "Order API")[0] == "http://203.0.113.20:3006/d/boss-order-api"
    assert os_.dashboard_link(up, "helpdesk-ai") == (
        None, "No dashboard for this app yet: deploy the stack again")
    url = os_.explore_url(up["url"], 'up{app="x"}', 1000, 2000)
    assert url.startswith("http://203.0.113.20:3006/explore?") and "up%7Bapp" in url


def test_boss_deploy_yaml_alerts_are_validated_and_start_the_rules(pg):
    from boss_engine.services.app_deploy_manifest import ManifestError
    from boss_engine.services.app_deploy_manifest import load_manifest as parse_manifest

    base = ("version: 1\nname: order-api\nkind: compose\n"
            "compose: {file: docker-compose.yml, project: order-api}\n"
            "metrics: {path: /metrics}\n")
    good = parse_manifest(base + (
        "alerts:\n  latency_p95: {threshold: 500, severity: critical, for: 2m}\n"
        "  no_traffic: {enabled: true}\n"
        "  rules:\n    - {alert: CheckoutSlow, expr: 'sum(rate(x[5m])) > 2', for: 1m,\n"
        "       labels: {severity: critical}, annotations: {summary: Checkout is slow}}\n"))
    summary = good.summary()["alerts"]
    assert summary["latency_p95"] == {"threshold": 500, "severity": "critical", "for_seconds": 120}
    for bad, why in (("alerts: {latency_p95: {threshold: 0}}", "between"),
                     ("alerts: {latncy: {}}", "unknown field"),
                     ("alerts: {rules: [{alert: X, expr: 'sum(rate(x[5m])'}]}", "not valid PromQL")):
        with pytest.raises(ManifestError, match=why):
            parse_manifest(base + bad + "\n")
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid, manifest={"metrics": {"path": "/metrics"}, "alerts": summary})
        rules = {r["kind"] if r["kind"] != "custom" else r["alert"]: r for r in aa.ensure_rules(c, t)}
    assert rules["latency_p95"]["params"]["threshold"] == 500
    assert rules["latency_p95"]["for_seconds"] == 120 and rules["latency_p95"]["source"] == "yaml"
    assert rules["latency_p95"]["labels"]["severity"] == "critical"
    assert rules["no_traffic"]["enabled"] is True
    assert rules["CheckoutSlow"]["for_seconds"] == 60 and rules["CheckoutSlow"]["source"] == "yaml"
