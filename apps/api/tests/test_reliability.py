"""RB-3/RB-4/RB-5 — the reliability pipeline end to end: scan → signals → named incidents →
diagnose (RCA over the knowledge graph). Service-level + full HTTP flow against boss_test."""
from apps.api.app.db import begin
from apps.api.app.services import reliability as rel
from apps.api.app.services.knowledge_graph import KnowledgeGraphService
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()
KG = KnowledgeGraphService()


def _errors(service, n, tag="err"):
    return [{"ts": i, "message": f"{tag} boom {i}", "service": service, "level": "error"} for i in range(n)]


def _window():
    return {"start": 0, "end": 10_000}


# --- pure helpers ------------------------------------------------------------
def test_dedupe_key_stable_and_severity():
    a = rel._dedupe_key("api", "error_surge", "error_rate")
    assert a == rel._dedupe_key("api", "error_surge", "error_rate")   # stable
    assert a != rel._dedupe_key("web", "error_surge", "error_rate")   # service-scoped
    assert rel._severity_for("error_surge") == "critical"
    assert rel._severity_for("metric_spike") == "warning"


def test_build_source_memory_and_bad_kind():
    src = rel.build_source({"kind": "memory", "logs": [{"ts": 1, "message": "hi"}]})
    assert src.kind == "memory"
    try:
        rel.build_source({"kind": "nope"})
        assert False, "should reject unknown kind"
    except ValueError:
        pass


# --- scan / incidents (service) ---------------------------------------------
def test_scan_readonly_does_not_persist(engine):
    with begin(engine) as c:
        spec = {"kind": "memory", "logs": _errors("checkout-x", 12)}
        report = RS.scan(c, PID, spec, _window(), persist=False)
        assert report["events"] == 12 and report["signal_count"] >= 1
        assert report["incidents"] == []                       # read-only: nothing written


def test_scan_persist_clusters_and_dedupes(engine):
    with begin(engine) as c:
        spec = {"kind": "memory", "logs": _errors("billing-svc", 15, tag="dberr")}
        first = RS.scan(c, PID, spec, _window(), persist=True)
        assert first["incidents"], "persisted scan should create an incident"
        inc_id = first["incidents"][0]["id"]
        before = RS.get_incident(c, PID, inc_id)["signal_count"]

        # a second scan of the same signature collapses onto the SAME incident
        second = RS.scan(c, PID, spec, _window(), persist=True)
        assert any(i["id"] == inc_id for i in second["incidents"])
        after = RS.get_incident(c, PID, inc_id)["signal_count"]
        assert after > before                                  # signal_count accrued, not duplicated

        # the service is now a graph node
        assert KG.get_node(c, PID, "service", "billing-svc") is not None


def test_get_incident_bundles_signals(engine):
    with begin(engine) as c:
        spec = {"kind": "memory", "logs": _errors("orders-svc", 11)}
        rep = RS.scan(c, PID, spec, _window(), persist=True)
        detail = RS.get_incident(c, PID, rep["incidents"][0]["id"])
        assert detail["signals"] and detail["signals"][0]["service"] == "orders-svc"


# --- diagnose (RCA over the graph) ------------------------------------------
def test_diagnose_uses_memory_when_a_known_cause_exists(engine):
    with begin(engine) as c:
        # seed a prior confirmed cause for the service (the learning write-back)
        KG.record_root_cause(c, PID, "payments-svc", "deployment", "deploy-9", "deploy 9",
                             confidence=0.92, evidence={"onset": "14:32"})
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("payments-svc", 13)},
                      _window(), persist=True)
        out = RS.diagnose(c, PID, rep["incidents"][0]["id"])
        assert out["hypothesis"]["basis"] == "memory"
        assert out["hypothesis"]["root_cause"] == "deploy 9"
        assert out["hypothesis"]["confidence"] == 0.92
        # incident advanced + confidence recorded
        assert RS.get_incident(c, PID, rep["incidents"][0]["id"])["status"] == "diagnosing"


def test_diagnose_falls_back_to_context_suspect(engine):
    with begin(engine) as c:
        # topology but no prior cause: svc depends_on a datastore suspect
        svc = KG.upsert_node(c, PID, "service", "search-svc")
        ds = KG.upsert_node(c, PID, "datastore", "search-es")
        KG.upsert_edge(c, PID, svc, ds, "depends_on")
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("search-svc", 12)},
                      _window(), persist=True)
        out = RS.diagnose(c, PID, rep["incidents"][0]["id"])
        assert out["hypothesis"]["basis"] == "context"
        assert out["hypothesis"]["cause_key"] == "search-es"


# --- full HTTP flow ----------------------------------------------------------
def test_http_scan_list_get_diagnose(client):
    body = {"project_id": PID, "actor": ACTOR, "persist": True,
            "source": {"kind": "memory", "logs": _errors("http-svc", 14)},
            "window": {"start": 0, "end": 10_000}}
    scan = client.post("/v1/reliability/scan", json=body)
    assert scan.status_code == 200, scan.text
    assert scan.json()["incidents"], "persisted scan returns incidents"

    lst = client.get("/v1/reliability/incidents", params={"project_id": PID})
    assert lst.status_code == 200
    incs = lst.json()["items"]
    mine = [i for i in incs if i["service"] == "http-svc"]
    assert mine, "our incident is on the board"
    inc_id = mine[0]["id"]

    got = client.get(f"/v1/reliability/incidents/{inc_id}", params={"project_id": PID})
    assert got.status_code == 200 and got.json()["service"] == "http-svc"

    diag = client.post(f"/v1/reliability/incidents/{inc_id}/diagnose",
                       json={"project_id": PID, "actor": ACTOR})
    assert diag.status_code == 200, diag.text
    assert "hypothesis" in diag.json() and diag.json()["hypothesis"]["confidence"] >= 0


def test_http_scan_readonly(client):
    body = {"project_id": PID, "actor": ACTOR, "persist": False,
            "source": {"kind": "memory", "logs": _errors("ro-svc", 12)},
            "window": {"start": 0, "end": 10_000}}
    r = client.post("/v1/reliability/scan", json=body)
    assert r.status_code == 200 and r.json()["incidents"] == []


def test_http_get_missing_incident_404(client):
    r = client.get("/v1/reliability/incidents/inc_does_not_exist", params={"project_id": PID})
    assert r.status_code == 404
