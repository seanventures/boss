"""The Chief of Staff, Discovery and the demo answer over HTTP (BOSSAPP-904/909; routers/ambient_cos.py).

* "Triage now" sweeps the project and runs the Chief of Staff's turn; what it opened is on the
  board at once, as a diagram.
* The dossier reads back; asking for one on a project with no repository says so in words.
* The demo morning loads only on a demo project (or with BOSS_AMBIENT_DEMO=1).
* The worker's tick is system-only: a person is refused, and the handler checks the principal.
"""
from __future__ import annotations

import inspect
from uuid import uuid4

from apps.api.app import main as api_main

HOME = "boss-dogfood"


def test_triage_now_turns_a_new_signal_into_an_item_on_the_board(client, engine):
    from boss_engine.services.ambient import signals

    app = f"svc-{uuid4().hex[:6]}"
    with engine.begin() as c:
        sig = signals.record_signal(c, project_id=HOME, source="alert", kind="alert.firing",
                                    title=f"{app} 5xx at 9%", subject_kind="app", subject_id=app,
                                    app=app, severity="high", source_ref=f"http:{app}")["signal"]
    got = client.post("/v1/ambient/chief-of-staff/run",
                      json={"project_id": HOME, "actor": "ceo", "sweep": False})
    assert got.status_code == 200, got.text
    assert got.json()["projects"][HOME]["counts"].get("proposed", 0) >= 1
    listed = client.get("/v1/ambient/work-items", params={"project_id": HOME,
                                                           "filter": "needs_you"}).json()
    mine = [i for i in listed["items"] if i["title"] == f"{app} 5xx at 9%"]
    assert mine and mine[0]["strip"][0]["type"] == "signal"
    one = client.get(f"/v1/ambient/work-items/{mine[0]['id']}",
                     params={"project_id": HOME}).json()
    assert any(n["label"] == "Chief of Staff" for n in one["nodes"])
    assert {"kind": "signal", "ref": sig["id"]} in [
        {"kind": l["kind"], "ref": l["ref"]} for l in one["links"]]


def test_the_dossier_reads_back_and_a_project_without_a_repo_is_told_so(client, engine):
    from boss_engine.services.ambient import dossier

    with engine.begin() as c:
        dossier.build(c, project_id=HOME, repo="acme/http-shop",
                      paths=["package.json", "src/index.ts"], seed_signals=False)
    got = client.get("/v1/ambient/dossier", params={"project_id": HOME, "repo": "acme/http-shop"})
    assert got.status_code == 200, got.text
    [one] = got.json()["dossiers"]
    assert one["read_from"] == "graph" and one["highlights"][1] == "No CI"
    asked = client.post("/v1/ambient/dossier", json={"project_id": HOME, "actor": "ceo",
                                                     "repo": "nobody/nothing"})
    assert asked.status_code == 404 and "no registered repository" in asked.json()["detail"]


def test_the_demo_loads_only_on_a_demo_project(client, monkeypatch):
    monkeypatch.delenv("BOSS_AMBIENT_DEMO", raising=False)
    refused = client.post("/v1/ambient/demo", json={"project_id": HOME, "actor": "ceo"})
    assert refused.status_code == 403 and "demo" in refused.json()["detail"]
    monkeypatch.setenv("BOSS_AMBIENT_DEMO", "1")
    loaded = client.post("/v1/ambient/demo", json={"project_id": HOME, "actor": "ceo"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["signals"] == 7
    resolved = client.get("/v1/ambient/work-items", params={
        "project_id": HOME, "filter": "resolved_by_agents"}).json()["items"]
    assert any(i["title"] == "CI failed on BOSS PR: unit-tests" for i in resolved)


def test_the_worker_tick_is_system_only_and_checks_the_principal():
    from apps.api.app import identity_guard
    from boss_engine.identity.declarations import Permit
    from boss_engine.identity.permissions_gen import PERMISSION_META

    entry = next(e for e in identity_guard.route_inventory(api_main.app)
                 if "POST /v1/ambient/tick" in e.labels())
    assert isinstance(entry.access, Permit)
    assert PERMISSION_META[entry.access.permission].system_only
    route = next(r for r in api_main.app.routes if getattr(r, "path", "") == "/v1/ambient/tick")
    assert "require_worker_principal()" in inspect.getsource(route.endpoint)


def test_load_demo_is_offered_only_on_a_demo_project_and_loads_every_story(client, monkeypatch):
    """The console's "Load demo": GET /v1/ambient/demo/status says whether it is offered; POST
    /v1/ambient/demo/load runs every loader (each reported loaded or skipped with why)."""
    monkeypatch.delenv("BOSS_AMBIENT_DEMO", raising=False)
    off = client.get("/v1/ambient/demo/status", params={"project_id": HOME})
    assert off.status_code == 200 and off.json()["available"] is False
    assert client.post("/v1/ambient/demo/load",
                       json={"project_id": HOME, "actor": "ceo"}).status_code == 403
    monkeypatch.setenv("BOSS_AMBIENT_DEMO", "1")
    on = client.get("/v1/ambient/demo/status", params={"project_id": HOME}).json()
    assert on["available"] is True
    assert on["loaders"] == ["order_api_incident", "morning", "squad_cve", "roadmap"]
    got = client.post("/v1/ambient/demo/load", json={"project_id": HOME, "actor": "ceo"})
    assert got.status_code == 200, got.text
    body = got.json()
    for key in on["loaders"]:
        assert (body["loaded"][key] is not None) or key in body["skipped"], key
    assert body["loaded"]["morning"] is not None
