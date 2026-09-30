"""RB-7 — remediation: a diagnosed incident → bug-framed governed code task → gated PR linkage.
Against boss_test."""
from apps.api.app.config import get_settings
from apps.api.app.db import begin
from apps.api.app.services.jobs import JobLeaseService
from apps.api.app.services.reliability import ReliabilityService, _bug_frame

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService(get_settings())
JOBS = JobLeaseService(get_settings())


def _errors(service, n):
    return [{"ts": i, "message": f"{service} boom {i}", "service": service, "level": "error"}
            for i in range(n)]


# --- framing -----------------------------------------------------------------
def test_bug_frame_includes_reproduce_and_hypothesis():
    text = _bug_frame({"title": "error_surge: db", "service": "orders"},
                      {"root_cause": "deploy 9", "basis": "memory", "confidence": 0.9})
    assert "BUG REPORT" in text and "regression test" in text
    assert "orders" in text and "deploy 9" in text


# --- remediate (service) -----------------------------------------------------
def test_remediate_creates_governed_code_task_and_links(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("rem-svc", 13)},
                      {"start": 0, "end": 9e9}, persist=True)
        inc_id = rep["incidents"][0]["id"]
        RS.diagnose(c, PID, inc_id)                       # produce a hypothesis to frame the fix
        out = RS.remediate(c, PID, inc_id, jobs=JOBS)
        assert out["code_task_run_id"].startswith("crun_")
        assert out["status"] == "queued"

        # incident advanced + a code_task_run row exists + remediation is linked
        detail = RS.get_incident(c, PID, inc_id)
        assert detail["status"] == "remediating"
        assert detail["remediations"] and detail["remediations"][0]["code_task_run_id"] == out["code_task_run_id"]
        run = c.execute(
            __import__("sqlalchemy").text("SELECT status, spec FROM code_task_runs WHERE id = :id"),
            {"id": out["code_task_run_id"]},
        ).mappings().first()
        assert run is not None                            # the governed pipeline run was created
        assert "BUG REPORT" in run["spec"]["instructions"]  # bug-framed
        assert run["spec"]["risk_tier"] == 3              # bug rigor


def test_remediate_missing_incident(engine):
    with begin(engine) as c:
        assert RS.remediate(c, PID, "inc_nope", jobs=JOBS) is None


# --- full HTTP flow: scan → diagnose → remediate -----------------------------
def test_http_scan_diagnose_remediate(client):
    scan = client.post("/v1/reliability/scan", json={
        "project_id": PID, "actor": ACTOR, "persist": True,
        "source": {"kind": "memory", "logs": _errors("rem-http", 14)},
        "window": {"start": 0, "end": 9e9}})
    inc_id = scan.json()["incidents"][0]["id"]
    client.post(f"/v1/reliability/incidents/{inc_id}/diagnose",
                json={"project_id": PID, "actor": ACTOR})
    rem = client.post(f"/v1/reliability/incidents/{inc_id}/remediate",
                      json={"project_id": PID, "actor": ACTOR})
    assert rem.status_code == 200, rem.text
    body = rem.json()
    assert body["code_task_run_id"].startswith("crun_") and body["status"] == "queued"

    got = client.get(f"/v1/reliability/incidents/{inc_id}", params={"project_id": PID})
    assert got.json()["status"] == "remediating"
    assert got.json()["remediations"][0]["code_task_run_id"] == body["code_task_run_id"]
