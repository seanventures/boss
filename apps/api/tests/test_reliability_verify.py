"""RB-18 — production verification loop: recurrence reopens, a clear stability window auto-resolves,
bookkeeping survives repeated checks. Against boss_test."""
from apps.api.app.db import begin
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def _errors(service, n, tag="boom"):
    return [{"ts": i, "message": f"{service} {tag} {i}", "service": service, "level": "error"}
            for i in range(n)]


def _healthy(service, n):
    return [{"ts": i, "message": f"{service} request ok {i}", "service": service, "level": "info"}
            for i in range(n)]


def _mk_incident(c, service):
    rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors(service, 12)},
                  {"start": 0, "end": 9e9}, persist=True)
    return rep["incidents"][0]["id"]


def _win():
    return {"start": 0, "end": 9e9}


def test_recurrence_reopens_incident(engine):
    with begin(engine) as c:
        inc_id = _mk_incident(c, "vf-recur")
        # the "fix" shipped but the same errors are still flowing
        out = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _errors("vf-recur", 10)}, _win())
        assert out["state"] == "recurred" and out["status"] == "open"
        assert out["recurred_signatures"]
        detail = RS.get_incident(c, PID, inc_id)
        assert detail["status"] == "open"
        assert any(e["kind"] == "verification" for e in detail["evidence"])


def test_clear_signal_resolves_after_stability_window(engine):
    with begin(engine) as c:
        inc_id = _mk_incident(c, "vf-clear")
        # signal is gone; stability_minutes=0 -> the window has trivially elapsed -> auto-resolve
        out = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _healthy("vf-clear", 20)},
                        _win(), stability_minutes=0)
        assert out["state"] == "recovered" and out["status"] == "resolved"
        assert RS.get_incident(c, PID, inc_id)["status"] == "resolved"


def test_stabilizing_holds_and_preserves_first_clear(engine):
    with begin(engine) as c:
        inc_id = _mk_incident(c, "vf-stab")
        first = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _healthy("vf-stab", 5)},
                          _win(), stability_minutes=60)
        assert first["state"] == "stabilizing" and first["checks"] == 1
        second = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _healthy("vf-stab", 5)},
                           _win(), stability_minutes=60)
        assert second["state"] == "stabilizing" and second["checks"] == 2
        assert second["first_clear"] == first["first_clear"]     # the window did NOT restart


def test_recurrence_resets_the_stability_window(engine):
    with begin(engine) as c:
        inc_id = _mk_incident(c, "vf-reset")
        a = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _healthy("vf-reset", 5)},
                      _win(), stability_minutes=60)
        assert a["state"] == "stabilizing"
        b = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _errors("vf-reset", 10)}, _win())
        assert b["state"] == "recurred"                          # relapse
        cchk = RS.verify(c, PID, inc_id, {"kind": "memory", "logs": _healthy("vf-reset", 5)},
                         _win(), stability_minutes=60)
        assert cchk["state"] == "stabilizing"
        assert cchk["first_clear"] != a["first_clear"]           # fresh window after the relapse


def test_verify_missing_incident(engine):
    with begin(engine) as c:
        assert RS.verify(c, PID, "inc_nope", {"kind": "memory", "logs": []}, _win()) is None


def test_http_verify_flow(client):
    scan = client.post("/v1/reliability/scan", json={
        "project_id": PID, "actor": ACTOR, "persist": True,
        "source": {"kind": "memory", "logs": _errors("vf-http", 13)},
        "window": {"start": 0, "end": 9e9}})
    inc_id = scan.json()["incidents"][0]["id"]
    # still failing -> recurred + reopened
    r1 = client.post(f"/v1/reliability/incidents/{inc_id}/verify", json={
        "project_id": PID, "actor": ACTOR,
        "source": {"kind": "memory", "logs": _errors("vf-http", 10)},
        "window": {"start": 0, "end": 9e9}})
    assert r1.status_code == 200, r1.text
    assert r1.json()["state"] == "recurred" and r1.json()["status"] == "open"
    # healthy + zero stability window -> auto-resolved
    r2 = client.post(f"/v1/reliability/incidents/{inc_id}/verify", json={
        "project_id": PID, "actor": ACTOR, "stability_minutes": 0,
        "source": {"kind": "memory", "logs": _healthy("vf-http", 20)},
        "window": {"start": 0, "end": 9e9}})
    assert r2.json()["state"] == "recovered" and r2.json()["status"] == "resolved"
