"""RB-12 (auto-tuning + few-shot) + RB-19 (autonomy tiers & policy). Against boss_test."""
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def _errors(service, n, tag="boom"):
    return [{"ts": i, "message": f"{service} {tag} {i}", "service": service, "level": "error"}
            for i in range(n)]


def _win():
    return {"start": 0, "end": 9e9}


# --- RB-12: auto-threshold tuning --------------------------------------------
def test_a_dismissed_signature_is_context_for_the_agent_never_a_coded_filter(engine):
    # RB-12's coded threshold is retired (BOSSAPP-598, G8): nothing is dropped below a count;
    # the dismissal history rides along as context for the agent's triage.
    with begin(engine) as c:
        spec = {"kind": "memory", "logs": _errors("tn-svc", 5)}
        rep = RS.scan(c, PID, spec, _win(), persist=True)
        for inc in rep["incidents"]:
            RS.feedback(c, PID, inc["id"], "false_alarm", actor=ACTOR)
            RS.feedback(c, PID, inc["id"], "false_alarm", actor=ACTOR)
        again = RS.scan(c, PID, spec, _win(), persist=True)
        assert again["incidents"], "the same low-count signature still raises its incident"
        assert again["suppressed"] == []
        assert again["noise_history"] and again["noise_history"][0]["dismissed_before"] >= 2


def test_diagnose_carries_few_shot_context(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("fs-svc", 11)}, _win(), persist=True)
        inc_id = rep["incidents"][0]["id"]
        RS.feedback(c, PID, inc_id, "real",
                    cause={"kind": "deployment", "key": "fs-dep", "name": "fs deploy",
                           "confidence": 0.9}, actor=ACTOR)
        RS.diagnose(c, PID, inc_id)
        ctx = c.execute(
            text("SELECT payload FROM incident_evidence WHERE incident_id = :id AND kind = 'context' "
                 "ORDER BY created_at DESC LIMIT 1"), {"id": inc_id}).scalar_one()
        assert any(f["ref"] == inc_id for f in ctx["few_shot"])   # confirmed example rides along


# --- RB-19: policy + tiers ---------------------------------------------------
def test_policy_defaults_and_roundtrip(engine):
    with begin(engine) as c:
        default = RS.get_policy(c, PID)
        assert default["auto_remediate"] is False and default["min_confidence"] == 0.8
        stored = RS.set_policy(c, PID, auto_remediate=True, min_confidence=0.7,
                               max_blast_radius=4, max_auto_severity="warning")
        assert stored["auto_remediate"] is True and stored["max_auto_severity"] == "warning"


def test_decide_action_tiers(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("tier-svc", 11)}, _win(), persist=True)
        inc_id = rep["incidents"][0]["id"]

        # policy off -> Tier-1 gated PR
        d = RS.decide_action(c, PID, inc_id)
        assert d == {**d, "tier": 1, "action": "gated_pr"}

        # policy on but low confidence -> still Tier-1
        RS.set_policy(c, PID, auto_remediate=True, min_confidence=0.8, max_blast_radius=5)
        d = RS.decide_action(c, PID, inc_id)
        assert d["tier"] == 1 and "confidence" in d["reason"]

        # high confidence within bounds, but no model verdict: statistics never act alone
        c.execute(text("UPDATE incidents SET confidence = 0.95 WHERE id = :id"), {"id": inc_id})
        d = RS.decide_action(c, PID, inc_id)
        assert d["tier"] == 1 and "no model verdict" in d["reason"]
        # a model's verdict on the cause, high confidence within bounds -> Tier-0 auto_remediate
        RS._add_evidence(c, inc_id, "rca_verdict", ref="dep-1",
                         payload={"status": "decided", "cause_key": "dep-1"})
        d = RS.decide_action(c, PID, inc_id)
        assert d["tier"] == 0 and d["action"] == "auto_remediate"

        # blast radius above cap -> Tier-2 handoff
        RS.set_policy(c, PID, auto_remediate=True, min_confidence=0.8, max_blast_radius=0)
        d = RS.decide_action(c, PID, inc_id)
        assert d["tier"] == 2 and d["action"] == "handoff"


def test_http_policy_and_decision(client):
    r = client.post("/v1/reliability/policy", json={
        "project_id": PID, "actor": ACTOR, "auto_remediate": True, "min_confidence": 0.9})
    assert r.status_code == 200 and r.json()["auto_remediate"] is True
    g = client.get("/v1/reliability/policy", params={"project_id": PID})
    assert g.json()["min_confidence"] == 0.9

    scan = client.post("/v1/reliability/scan", json={
        "project_id": PID, "actor": ACTOR, "persist": True,
        "source": {"kind": "memory", "logs": _errors("tier-http", 12)},
        "window": {"start": 0, "end": 9e9}})
    inc_id = scan.json()["incidents"][0]["id"]
    d = client.get(f"/v1/reliability/incidents/{inc_id}/decision", params={"project_id": PID})
    assert d.status_code == 200 and d.json()["action"] in ("gated_pr", "auto_remediate", "handoff")
