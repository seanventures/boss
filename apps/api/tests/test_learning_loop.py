"""RB-6 — the native LearningLoop primitive + the SRE feedback that feeds it. Against boss_test."""
from apps.api.app.db import begin
from apps.api.app.services.knowledge_graph import KnowledgeGraphService
from apps.api.app.services.learning_loop import LearningLoopService
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
LL = LearningLoopService()
RS = ReliabilityService()
KG = KnowledgeGraphService()


def _errors(service, n):
    return [{"ts": i, "message": f"{service} boom {i}", "service": service, "level": "error"}
            for i in range(n)]


# --- primitive ---------------------------------------------------------------
def test_record_corpus_and_evaluate(engine):
    with begin(engine) as c:
        for outcome in ("confirmed", "confirmed", "dismissed"):
            LL.record(c, PID, agent="ll_test", kind="incident_feedback", label="v",
                      outcome=outcome, payload={"signatures": ["sig_x"]})
        LL.record(c, PID, agent="ll_test", kind="incident_feedback", label="v",
                  outcome="fix_good", payload={})
        rows = LL.corpus(c, PID, "ll_test")
        assert len(rows) == 4
        ev = LL.evaluate(c, PID, "ll_test")
        assert ev["by_outcome"]["confirmed"] == 2
        assert ev["false_alarm_rate"] == round(1 / 3, 3)     # 1 dismissed of 3 triaged
        assert ev["fix_success_rate"] == 1.0                 # 1 good, 0 bad


def test_suggest_tuning_raises_repeatedly_dismissed_signatures(engine):
    with begin(engine) as c:
        # same signature dismissed twice -> should be suggested for a threshold raise
        for _ in range(2):
            LL.record(c, PID, agent="ll_tune", kind="incident_feedback", label="false_alarm",
                      outcome="dismissed", payload={"signatures": ["noisy_sig"]})
        LL.record(c, PID, agent="ll_tune", kind="incident_feedback", label="false_alarm",
                  outcome="dismissed", payload={"signatures": ["rare_sig"]})   # once only
        LL.record(c, PID, agent="ll_tune", kind="incident_feedback", label="real",
                  outcome="confirmed", ref="inc_9", payload={"signatures": ["good_sig"]})
        tuning = LL.suggest_tuning(c, PID, "ll_tune")
        raised = {t["signature"] for t in tuning["raise_threshold"]}
        assert raised == {"noisy_sig"}                       # rare_sig below the 2x floor
        assert any(f["ref"] == "inc_9" for f in tuning["few_shot_candidates"])


# --- SRE feedback consumer ---------------------------------------------------
def test_feedback_real_writes_cause_back_to_graph(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("ll-pay", 12)},
                      {"start": 0, "end": 9e9}, persist=True)
        inc_id = rep["incidents"][0]["id"]
        out = RS.feedback(c, PID, inc_id, "real",
                          cause={"kind": "deployment", "key": "dep-x", "name": "deploy x",
                                 "confidence": 0.9}, actor=ACTOR)
        assert out["outcome"] == "confirmed" and out["status"] == "triaged"
        # the cause is now recallable for the service
        assert KG.known_causes(c, PID, "ll-pay")[0]["node_key"] == "dep-x"


def test_feedback_false_alarm_dismisses_and_tunes(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("ll-noise", 12)},
                      {"start": 0, "end": 9e9}, persist=True)
        inc_id = rep["incidents"][0]["id"]
        RS.feedback(c, PID, inc_id, "false_alarm", actor=ACTOR)
        again = RS.feedback(c, PID, inc_id, "false_alarm", actor=ACTOR)
        assert again["status"] == "dismissed"
        # both dismissals share the incident's signature -> tuning suggests raising it
        assert again["tuning"]["raise_threshold"], "repeated false alarms should suggest a threshold raise"


def test_feedback_unknown_verdict_rejected(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("ll-x", 11)},
                      {"start": 0, "end": 9e9}, persist=True)
        try:
            RS.feedback(c, PID, rep["incidents"][0]["id"], "bogus")
            assert False, "should reject unknown verdict"
        except ValueError:
            pass


# --- HTTP --------------------------------------------------------------------
def test_http_feedback_flow(client):
    scan = client.post("/v1/reliability/scan", json={
        "project_id": PID, "actor": ACTOR, "persist": True,
        "source": {"kind": "memory", "logs": _errors("ll-http", 13)},
        "window": {"start": 0, "end": 9e9}})
    inc_id = scan.json()["incidents"][0]["id"]
    r = client.post(f"/v1/reliability/incidents/{inc_id}/feedback",
                    json={"project_id": PID, "actor": ACTOR, "verdict": "real",
                          "cause": {"kind": "deployment", "key": "http-dep", "name": "http deploy",
                                    "confidence": 0.88}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "confirmed" and body["status"] == "triaged"

    # re-diagnosing now recalls the cause from memory
    diag = client.post(f"/v1/reliability/incidents/{inc_id}/diagnose",
                       json={"project_id": PID, "actor": ACTOR})
    assert diag.json()["hypothesis"]["basis"] == "memory"
    assert diag.json()["hypothesis"]["cause_key"] == "http-dep"
