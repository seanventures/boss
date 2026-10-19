"""Monitoring Phase 1a (BOSSAPP-598): one alert state machine and the noise controls around it.

Acceptance (PRD §12, Phase 1a): 5 (flapping), 6 (no data never resolves a firing alert),
7 (maintenance mutes notification while the console shows the state), 8 (declarative
inhibition), 9 (storm: triaged once per group). Plus keep_firing_for, ack never silences and
expires, deploy holds, rule groups on their own clock, and the Observability card that read
"requests failing" over a list reading "Nothing firing" (operator screenshot, rc10.8).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import app_alerts as aa
from boss_engine.services import app_alerts_view as view
from boss_engine.services import monitoring as mon

T0 = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _svc(c, pid, name=None):
    svc = mon.find_or_create_service(c, pid, name or f"svc-{uuid4().hex[:6]}")
    return svc, aa.subject(c, svc["id"])


def _gauge(c, pid, svc, name, t, v, **labels):
    mon.write_points(c, pid, svc, [(mon.stamp({"__name__": name, **labels}, project_id=pid,
                                              svc=svc), t, v)], now=t)


def _rule(c, subj, expr, **change):
    rule = aa.update_rule(c, subj, None, {"alert": "QueueDeep", "expr": expr,
                                          "labels": {"severity": "critical"}}, actor="usr_t")
    return aa.update_rule(c, subj, rule["id"], change, actor="usr_t") if change else rule


def _eval(eng, subj, t):
    with eng.begin() as c:
        return aa.evaluate_target(c, subj, t)


def _alert(eng, subj, name="QueueDeep"):
    with eng.begin() as c:
        return dict(c.execute(text("SELECT * FROM app_alerts WHERE subject_id = :s AND "
                                   "alertname = :n ORDER BY active_at DESC LIMIT 1"),
                              {"s": subj["id"], "n": name}).mappings().first() or {})


# ── 6. no data never resolves a firing alert ────────────────────────────────────────────────
def test_a_scrape_gap_while_firing_does_not_resolve_it(pg):
    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        _rule(c, subj, "max(queue_depth) > 100")
        _gauge(c, pid, svc, "queue_depth", T0, 500)
    _eval(eng, subj, T0)
    assert _alert(eng, subj)["state"] == "firing"
    # 10 minutes later the series is gone (older than the 5-minute lookback): no data
    _eval(eng, subj, T0 + timedelta(minutes=10))
    a = _alert(eng, subj)
    assert a["state"] == "firing" and a["no_data_since"], "no data is said, never a recovery"
    with eng.begin() as c:
        _gauge(c, pid, svc, "queue_depth", T0 + timedelta(minutes=11), 5)
    _eval(eng, subj, T0 + timedelta(minutes=11))
    assert _alert(eng, subj)["state"] == "resolved", "data back and healthy: now it resolves"


def test_keep_firing_for_holds_a_disappearing_series_firing_first(pg):
    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        rule = _rule(c, subj, "max(queue_depth) > 100", keep_firing_for="3m")
        assert rule["keep_firing_for_s"] == 180
        for i in range(3):
            _gauge(c, pid, svc, "queue_depth", T0 + timedelta(minutes=i), 500 if i == 0 else 5)
    _eval(eng, subj, T0)
    _eval(eng, subj, T0 + timedelta(minutes=1))
    assert _alert(eng, subj)["state"] == "firing", "it cleared, but keep_firing_for holds it"
    with eng.begin() as c:
        _gauge(c, pid, svc, "queue_depth", T0 + timedelta(minutes=5), 5)
    _eval(eng, subj, T0 + timedelta(minutes=5))
    a = _alert(eng, subj)
    assert a["state"] == "resolved" and a["cleared_reason"] == "recovered"
    with eng.begin() as c:
        steps = [r[0] for r in c.execute(text(
            "SELECT to_state FROM alert_transitions WHERE alert_id = :a ORDER BY at"),
            {"a": a["id"]}).all()]
    assert steps == ["firing", "resolved"], "every transition is in the append-only log"


# ── 5. flapping ─────────────────────────────────────────────────────────────────────────────
def test_a_flapping_series_becomes_flapping_once_and_settles_with_hysteresis():
    hist, flapping = "", False
    for i in range(12):
        hist, score = mon.flap_update(hist, i % 2 == 0)
        flapping = mon.flap_state(flapping, score)
    assert flapping and score > 0.5
    for _ in range(6):                     # steady for a while: still above the 0.3 floor
        hist, score = mon.flap_update(hist, True)
        flapping = mon.flap_state(flapping, score)
    assert flapping, "it stops flapping only below 0.3, not just below 0.5"
    for _ in range(15):
        hist, score = mon.flap_update(hist, True)
        flapping = mon.flap_state(flapping, score)
    assert not flapping and 0.0 <= score <= 1.0


def test_an_alert_that_flaps_is_marked_flapping_and_the_agent_proposes_a_tuning(pg):
    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        _rule(c, subj, "max(queue_depth) > 100")
    for i in range(10):
        t = T0 + timedelta(minutes=i)
        with eng.begin() as c:
            _gauge(c, pid, svc, "queue_depth", t, 500 if i % 2 == 0 else 5)
        _eval(eng, subj, t)
    a = _alert(eng, subj)
    assert a["flapping"] and a["fire_count"] >= 4, "one alert, reopened, now flapping"

    class Model:
        def create_text_response(self, prompt, system_prompt=None):
            return json.dumps({"action": "for:120", "confidence": 0.9,
                               "reason": "it alternates every minute", "question": ""})
    assert aa.propose_flap_tuning(eng, None, project_ids={pid}, adapter=Model()) >= 1
    tuned = _alert(eng, subj)
    assert json.loads(json.dumps(tuned["triage"]))["flap_tuning"]["action"] == "for:120"
    with eng.begin() as c:
        rule = aa.rules(c, subj["id"])[0]
    assert rule["for_seconds"] == 0, "a person applies the tuning; BOSS changes no rule"


# ── ack ─────────────────────────────────────────────────────────────────────────────────────
def test_ack_never_silences_and_expires_after_four_hours(pg):
    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        _rule(c, subj, "max(queue_depth) > 100")
        _gauge(c, pid, svc, "queue_depth", T0, 500)
    _eval(eng, subj, T0)
    a = _alert(eng, subj)
    with eng.begin() as c:
        mon.ack(c, pid, alert_ids=[a["id"]], actor="usr_t", now=T0)
    _eval(eng, subj, T0 + timedelta(minutes=1))
    after = _alert(eng, subj)
    assert after["state"] == "firing" and after["silenced_by"] is None, "ack is not a silence"
    assert mon.acked_now(after, T0 + timedelta(hours=3))
    assert not mon.acked_now(after, T0 + timedelta(hours=4, minutes=1))


# ── 7, 8. maintenance and inhibition ────────────────────────────────────────────────────────
def test_a_maintenance_window_mutes_while_the_state_stays_visible(pg):
    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        _rule(c, subj, "max(queue_depth) > 100")
        _gauge(c, pid, svc, "queue_depth", T0, 500)
        c.execute(text(
            "INSERT INTO maintenance_windows (id, project_id, name, matchers, rrule, starts_at, "
            " duration_min, mode, created_by) VALUES (:i, :p, 'Nightly', CAST(:m AS JSONB), "
            " 'FREQ=DAILY', :s, 30, 'mute', 'usr_t')"),
            {"i": f"mw_{uuid4().hex[:8]}", "p": pid, "s": T0 - timedelta(days=3, minutes=5),
             "m": json.dumps([{"name": "service", "op": "=", "value": svc["name"]}])})
    _eval(eng, subj, T0)
    a = _alert(eng, subj)
    assert a["state"] == "firing", "evaluation goes on underneath"
    assert a["silenced_by"] and a["incident_id"] is None
    assert any(x["kind"] == "maintenance" for x in a["suppressed_by"])
    with eng.begin() as c:
        _gauge(c, pid, svc, "queue_depth", T0 + timedelta(minutes=30), 500)
    _eval(eng, subj, T0 + timedelta(minutes=30))
    a = _alert(eng, subj)
    assert a["silenced_by"] is None and a["incident_id"], "the window ended: it is told now"
    assert mon.parse_rrule("FREQ=WEEKLY;BYDAY=SA,SU")["days"] == [5, 6]
    with pytest.raises(ValueError):
        mon.parse_rrule("FREQ=HOURLY")


def test_a_declarative_inhibit_rule_holds_back_matching_symptoms(pg):
    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        aa.update_rule(c, subj, None, {"alert": "DbDown", "expr": "max(db_up) == 0",
                                       "labels": {"severity": "critical", "tier": "db"}},
                       actor="usr_t")
        aa.update_rule(c, subj, None, {"alert": "SlowPages", "expr": "max(page_ms) > 800",
                                       "labels": {"severity": "warning", "tier": "web"}},
                       actor="usr_t")
        c.execute(text(
            "INSERT INTO inhibit_rules (id, project_id, name, source_matchers, target_matchers, "
            " equal) VALUES (:i, :p, 'db holds web', CAST(:s AS JSONB), CAST(:t AS JSONB), "
            " '[\"boss_subject\"]'::jsonb)"),
            {"i": f"inh_{uuid4().hex[:8]}", "p": pid,
             "s": json.dumps([{"name": "tier", "op": "=", "value": "db"}]),
             "t": json.dumps([{"name": "tier", "op": "=", "value": "web"}])})
        _gauge(c, pid, svc, "db_up", T0, 0)
        _gauge(c, pid, svc, "page_ms", T0, 2000)
    _eval(eng, subj, T0)
    db, web = _alert(eng, subj, "DbDown"), _alert(eng, subj, "SlowPages")
    assert web["inhibited_by"] == db["id"] and web["incident_id"] is None
    assert db["incident_id"], "the cause is told; the symptom is held back"


def test_a_deploy_hold_keeps_the_alert_visible_and_holds_only_the_message(pg):
    from boss_engine.services import notify

    eng, pid = pg
    now = datetime.now(timezone.utc)
    with eng.begin() as c:
        tid = f"t_hold_{uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            " target_kind, target_ref, status, created_by, domains) VALUES (:t, :p, 'Shop', "
            " 'acme/shop', 'prod', 'stack', 'shop', 'active', 'test', '[]'::jsonb)"),
            {"t": tid, "p": pid})
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            " commit_sha, manifest, status, created_by, created_at, finished_at) VALUES (:i, :p, "
            " :t, 'acme/shop', 'main', 'abc1234', '{}'::jsonb, 'healthy', 'usr_t', :at, :at)"),
            {"i": f"sd_{uuid4().hex[:8]}", "p": pid, "t": tid, "at": now - timedelta(minutes=5)})
        held = [x for x in mon.suppressors(c, pid, now) if x["kind"] == "deploy_hold"]
    assert held and held[0]["mode"] == "hold", "15 minutes after a deploy: held, never muted"
    alert = {"suppressed_by": [{"mode": "hold", "until": held[0]["until"].isoformat()}],
             "triage": None}
    assert notify._held(alert, now), "the message waits for the agent"
    assert not notify._held({**alert, "triage": {"outcome": "page"}}, now), \
        "the agent said page: it goes now"


# ── 9. storm ────────────────────────────────────────────────────────────────────────────────
def test_a_storm_is_triaged_once_per_group(pg):
    eng, pid = pg
    now = datetime.now(timezone.utc)
    with eng.begin() as c:
        subjects = []
        for g in range(3):
            svc, subj = _svc(c, pid, f"storm-{g}-{uuid4().hex[:4]}")
            subjects.append((svc, subj))
            for i in range(10):
                aa.update_rule(c, subj, None, {"alert": f"Shard{i}", "expr": f"max(s{i}) > 1",
                                               "labels": {"severity": "critical",
                                                          "group_key": "shards"}},
                               actor="usr_t")
                _gauge(c, pid, svc, f"s{i}", now, 5)
    for _svc_, subj in subjects:
        _eval(eng, subj, now)
    folded = aa.storm_fold(eng, {pid}, now=now)
    assert len(folded) == 3 and all("Storm: 30 alerts in 3 groups" in f["word"] for f in folded)
    claimed = aa._claim(eng, {pid}, 50)
    assert len(claimed) == 3, "30 alerts in a minute: at most 3 triages"


# ── rule groups on their own clock ─────────────────────────────────────────────────────────
def test_rule_groups_are_claimed_once_by_lease_and_never_backfilled(pg):
    from boss_engine.services import monitor_scheduler as ms

    eng, pid = pg
    with eng.begin() as c:
        svc, subj = _svc(c, pid)
        _rule(c, subj, "max(queue_depth) > 100")
        ms.ensure_groups(c, T0)
        c.execute(text("UPDATE rule_groups SET next_eval_at = :t WHERE subject_id = :s"),
                  {"t": T0 - timedelta(minutes=10), "s": svc["id"]})
    first = [g for g in ms.claim(eng, "w1", T0) if g["subject_id"] == svc["id"]]
    second = [g for g in ms.claim(eng, "w2", T0) if g["subject_id"] == svc["id"]]
    assert first and not second, "a leased group is not claimed twice"
    out = ms.run_group(eng, None, first[0], T0)
    assert out["lag_ms"] >= 600_000 and out["error"] is None
    with eng.begin() as c:
        g = c.execute(text("SELECT next_eval_at, lease_owner FROM rule_groups WHERE "
                           "subject_id = :s"), {"s": svc["id"]}).mappings().one()
    assert g["next_eval_at"] > T0 and g["lease_owner"] is None, "no backfill of missed evals"


# ── the screenshot: one answer to "is it failing now?" ──────────────────────────────────────
def test_an_open_incident_that_recovered_is_never_carded_as_failing(pg):
    from boss_engine.services import infra_view, system_doctor

    eng, pid = pg
    with eng.begin() as c:
        tid = f"t_card_{uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            " target_kind, target_ref, status, created_by, domains) VALUES (:t, :p, 'Order API', "
            " 'acme/shop', 'staging', 'stack', 'order', 'active', 'test', '[]'::jsonb)"),
            {"t": tid, "p": pid})
        inc = f"inc_{uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, status) VALUES (:i, :p, "
            " :k, 'Order API (staging): 6.6% of requests failing', 'diagnosing')"),
            {"i": inc, "p": pid, "k": "app_error_rate:" + tid})
        c.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, "
            " alertname, state, active_at, fired_at, cleared_at, cleared_reason, incident_id, "
            " backfilled) VALUES (:a, :p, :t, 'error_rate', :f, 'AppErrorRate', 'resolved', :at, "
            " :at, :clr, 'recovered', :i, true)"),
            {"a": f"alrt_{uuid4().hex[:8]}", "p": pid, "t": tid, "f": uuid4().hex[:16],
             "at": T0, "clr": T0 + timedelta(hours=4), "i": inc})
        card = next(i for i in infra_view.attention(c, None, pid)
                    if i["id"] == f"infra:app_errors:{tid}")
        listed = view.alerts(c, pid, state="firing")
        items = system_doctor.card_items(c, pid)
    assert listed["counts"]["firing"] == 0
    state = next(s for s in listed["incidents"] if s["id"] == inc)
    # the time travels as recovered_at, for the viewer's clock — never baked in UTC
    assert not state["happening"] and state["word"].startswith("Recovered")
    assert state["recovered_at"] and " at " not in state["word"]
    assert "requests failing" not in card["title"] and "recovered" in card["title"]
    assert all(i.get("area") in ("factory", "observability") for i in items), \
        "every card says which area owns it"
    assert all(i["area"] == "factory" for i in items if str(i["id"]).startswith("shepherd:")), \
        "a build question is Factory's, never Observability's"
