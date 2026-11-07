"""The Chief of Staff reads an alert from its structured facts, not its title (design §12.5).

Every firing alert used to become `incident_response` (high risk), so even a staging rollback
needed a person at A2/A3, and a latency alert went to `perf_regression`, away from the squad.
Now the reading comes from the rows the signal cites — the target's environment, the rule's
metric, the severity, the blast radius (apps firing at once):

* a staging 5xx on one app → incident_staging_mitigation: at A2+ the squad may roll back
  staging after a critic agrees, and reports;
* production, an unknown environment, or a wide blast radius → incident_response: a person
  approves the mitigation;
* a latency alert reaches the squad too (an incident class), on staging or production;
* the model, when it is asked, sees the same facts.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.ambient import board, chief_of_staff as cos, policy

from ._ambient_support import eng, project, signal  # noqa: F401


def _target(c, pid, env, app="order-api"):
    tid = f"t_cos_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) VALUES (:t, :p, :a, 'acme/shop', :e, 'stack', "
        " :a, 'active', 'test', '[]'::jsonb)"), {"t": tid, "p": pid, "a": app, "e": env})
    return tid


def _alert(c, pid, tid, kind="error_rate", alertname="AppErrorRate"):
    aid = f"al_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, alertname, "
        " labels, state, active_at, fired_at) VALUES (:i, :p, :t, :k, :f, :n, "
        " CAST(:l AS JSONB), 'firing', now(), now())"),
        {"i": aid, "p": pid, "t": tid, "k": kind, "f": f"fp_{aid}", "n": alertname,
         "l": json.dumps({"app": "order-api"})})
    return aid


def _alert_signal(c, pid, aid, severity="critical", title="Error rate firing"):
    return signal(c, pid, title=title, severity=severity, fingerprint_value=f"fp:{aid}",
                  evidence=[{"kind": "alert", "ref": aid}])


def _read(c, pid, sig):
    return cos.classify(c, get_settings(), pid, sig, budget=cos.Budget(calls_left=0), bar=0.8)


def test_a_staging_5xx_on_one_app_is_a_staging_mitigation(eng, project):
    with eng.begin() as c:
        sig = _alert_signal(c, project, _alert(c, project, _target(c, project, "staging")))
        facts = cos.alert_facts(c, sig)
        reading = _read(c, project, sig)
    assert facts["environment"] == "staging" and not facts["production"]
    assert facts["metric"] == "error_rate" and facts["blast"] == "one app"
    assert reading.task_class == "incident_staging_mitigation" and reading.by == "rule"


@pytest.mark.parametrize("env", ["production", "prod", ""])
def test_production_or_unknown_is_an_incident_a_person_approves(eng, project, env):
    with eng.begin() as c:
        sig = _alert_signal(c, project, _alert(c, project, _target(c, project, env)))
        reading = _read(c, project, sig)
    assert reading.task_class == "incident_response"


def test_a_wide_blast_radius_on_staging_asks_a_person(eng, project):
    with eng.begin() as c:
        first = _alert(c, project, _target(c, project, "staging", "order-api"))
        _alert(c, project, _target(c, project, "staging", "billing-api"))
        sig = _alert_signal(c, project, first)
        assert cos.alert_facts(c, sig)["blast"] == "wide"
        assert _read(c, project, sig).task_class == "incident_response"


@pytest.mark.parametrize("env,klass", [("staging", "incident_staging_mitigation"),
                                       ("production", "incident_response")])
def test_a_latency_alert_reaches_the_incident_squad(eng, project, env, klass):
    with eng.begin() as c:
        aid = _alert(c, project, _target(c, project, env), kind="latency_p95",
                     alertname="AppLatencyP95")
        sig = _alert_signal(c, project, aid, severity="high", title="p95 latency firing")
        assert cos.alert_facts(c, sig)["metric"] == "latency_p95"
        assert _read(c, project, sig).task_class == klass


def test_the_title_does_not_decide(eng, project):
    """'latency' in a title on an error-rate rule is still an error-rate incident."""
    with eng.begin() as c:
        aid = _alert(c, project, _target(c, project, "staging"))
        sig = _alert_signal(c, project, aid, title="slow latency p95 duration")
        assert _read(c, project, sig).task_class == "incident_staging_mitigation"


def test_the_model_sees_the_same_facts(eng, project):
    with eng.begin() as c:
        aid = _alert(c, project, _target(c, project, "production"), kind="latency_p95",
                     alertname="AppLatencyP95")
        sig = _alert_signal(c, project, aid, severity="medium")
        facts = cos.alert_facts(c, sig)
    rules = cos.rule_candidates(sig, facts)
    assert [k for k, _, _ in rules] == ["incident_response", "perf_regression"]
    assert rules[0][1] - rules[1][1] < cos.AMBIGUITY_GAP   # close: the model judges
    assert cos._signal_context(sig, facts)["alert_facts"]["environment"] == "production"


def _route_settings():
    return SimpleNamespace(**{**get_settings().__dict__, "ambient_cos_mode": "route",
                              "ambient_cos_model_calls": 0})


def test_at_a2_in_route_mode_a_staging_incident_goes_to_the_squad_who_may_roll_back(eng, project):
    with eng.begin() as c:
        _alert_signal(c, project, _alert(c, project, _target(c, project, "staging")))
        report = cos.triage_project(c, _route_settings(), project, mode="route",
                                    max_model_calls=0)
        [item] = board.list_items(c, project, limit=5)
        gate = policy.gate(c, project, item["task_class"], "staging_rollback")
    assert report["items"][0]["outcome"] in ("routed", "handed_over", "assigned"), report
    assert item["task_class"] == "incident_staging_mitigation"
    assert gate["allowed"] and not gate["needs_person"]
    assert gate["consensus_mode"] == "maker_checker"


def test_a_production_incident_keeps_a_person_on_the_rollback_at_a3(eng, project):
    with eng.begin() as c:
        policy.set_level(c, project, "incident_response", actor="person:owner", level="A3")
        _alert_signal(c, project, _alert(c, project, _target(c, project, "production")))
        cos.triage_project(c, _route_settings(), project, mode="route", max_model_calls=0)
        [item] = board.list_items(c, project, limit=5)
        gate = policy.gate(c, project, item["task_class"], "prod_rollback")
    assert item["task_class"] == "incident_response"
    assert gate["needs_person"]
