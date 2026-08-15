"""Evidence-only AI SRE postmortems.

Drafts are assembled from existing incident evidence only. Diagnose is never
implied; unresolved incidents still draft; missing incidents 404. Not GA.
"""
from apps.api.app.db import begin
from apps.api.app.services.reliability import ReliabilityService
from boss_engine.services.sre_postmortem import draft_postmortem, get_postmortem

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def _errors(service, n, tag="err"):
    return [{"ts": i, "message": f"{tag} boom {i}", "service": service, "level": "error"} for i in range(n)]


def _window():
    return {"start": 0, "end": 10_000}


def _scan_incident(connection, service):
    report = RS.scan(
        connection,
        PID,
        {"kind": "memory", "logs": _errors(service, 12)},
        _window(),
        persist=True,
    )
    assert report["incidents"], "persisted scan should create an incident"
    return report["incidents"][0]["id"]


def test_http_missing_incident_postmortem_404(client):
    body = {"project_id": PID, "actor": ACTOR}
    posted = client.post("/v1/reliability/incidents/inc_does_not_exist/postmortem", json=body)
    assert posted.status_code == 404
    got = client.get(
        "/v1/reliability/incidents/inc_does_not_exist/postmortem",
        params={"project_id": PID},
    )
    assert got.status_code == 404


def test_unresolved_incident_still_drafts(engine, client):
    with begin(engine) as connection:
        inc_id = _scan_incident(connection, "pm-open-svc")
        before = RS.get_incident(connection, PID, inc_id)
        assert before["status"] == "open"

    posted = client.post(
        f"/v1/reliability/incidents/{inc_id}/postmortem",
        json={"project_id": PID, "actor": ACTOR},
    )
    assert posted.status_code == 200, posted.text
    payload = posted.json()
    assert payload["incident_id"] == inc_id
    assert payload["service"] == "pm-open-svc"
    assert payload["status"] == "draft"
    assert payload["winner_hypothesis"] is None

    fetched = client.get(
        f"/v1/reliability/incidents/{inc_id}",
        params={"project_id": PID},
    )
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "open"

    stored = client.get(
        f"/v1/reliability/incidents/{inc_id}/postmortem",
        params={"project_id": PID},
    )
    assert stored.status_code == 200
    assert stored.json()["status"] == "draft"
    assert stored.json()["incident_id"] == inc_id


def test_no_invented_root_cause_when_diagnose_never_ran(engine):
    with begin(engine) as connection:
        inc_id = _scan_incident(connection, "pm-nrca-svc")
        payload = draft_postmortem(connection, PID, inc_id)
        assert payload is not None
        assert payload["status"] == "draft"
        assert payload["winner_hypothesis"] is None
        assert payload.get("root_cause") is None
        assert "winner_hypothesis" in payload["missing"]
        assert "unknown" not in str(payload.get("root_cause") or "").lower()
        # Timeline is only timestamps that already existed on the incident.
        kinds = {item["kind"] for item in payload["timeline"]}
        assert "first_seen" in kinds
        assert "last_seen" in kinds
        persisted = get_postmortem(connection, PID, inc_id)
        assert persisted["incident_id"] == inc_id
        assert persisted["winner_hypothesis"] is None
        assert RS.get_incident(connection, PID, inc_id)["status"] == "open"


def test_recorded_rca_is_complete_enough_and_not_invented(engine):
    with begin(engine) as connection:
        inc_id = _scan_incident(connection, "pm-rca-svc")
        diagnosed = RS.diagnose(connection, PID, inc_id)
        assert diagnosed is not None
        winner = diagnosed["hypothesis"]
        payload = draft_postmortem(connection, PID, inc_id)
        assert payload["status"] == "complete_enough"
        assert payload["winner_hypothesis"] is not None
        assert payload["winner_hypothesis"]["root_cause"] == winner["root_cause"]
        assert payload["winner_hypothesis"]["cause_key"] == winner["cause_key"]
        assert "winner_hypothesis" not in payload["missing"]
