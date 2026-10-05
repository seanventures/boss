"""One Deploy button, routed by target kind (spec §4.5, §6.5): a stack target plans a stack
deployment, an app target a delivery deploy, a cloud-account target a registry run, each through
its existing engine. Community refuses the per-app and registry deploys (`Paused in Community`,
as the console has always said) and still deploys stacks. Dollars become micros server-side."""
from __future__ import annotations

import pytest

from apps.api.app import main as api_main
from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def engines(engine, monkeypatch):
    from boss_engine.services import resource_plane, stack_deploy

    seed(engine, HOME, samples=False)
    calls: list[tuple] = []

    def fake_stack(engine_, settings, *, project_id, target_id, repository, ref, actor, **kw):
        calls.append(("stack", target_id, ref, actor))
        return {"id": "sd_new", "status": "awaiting_approval"}

    def fake_registry(self, connection, *, project_id, deployment_target_id, commit_ref,
                      release_packet, expected_cost_usd_micros, reservation_id, actor):
        calls.append(("registry", deployment_target_id, commit_ref, expected_cost_usd_micros,
                      reservation_id, release_packet, actor))
        return {"deployment_run_id": "dr_reg", "status": "created"}

    def fake_delivery(payload):
        p = payload.payload()
        calls.append(("delivery", p["target_id"], p.get("ref"), p.get("actor")))
        return {"deployment_run_id": "dr_app", "status": "planned", "trigger": "manual"}

    monkeypatch.setattr(stack_deploy, "plan_stack_deploy", fake_stack)
    monkeypatch.setattr(resource_plane.ResourcePlaneService, "plan_deployment_run", fake_registry)
    monkeypatch.setattr(api_main, "delivery_deploy_endpoint", fake_delivery)
    yield calls
    with engine.begin() as c:
        clear(c, HOME)


def _deploy(client, target, **body):
    return client.post(f"/v1/infra/targets/{target}/deploy",
                       json={"project_id": HOME, "actor": "ceo", **body})


def test_three_kinds_hit_three_engines(client, engines, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    r = _deploy(client, "t_stack", ref="next")
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "stack:sd_new"
    r = _deploy(client, "t_live", ref="main")
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "delivery:dr_app"
    r = _deploy(client, "t_cloud", ref="v2", expected_cost_usd=12.5,
                budget_reservation_id="res_1", release_packet={"notes": "hi"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "registry:dr_reg"
    assert engines == [
        ("stack", "t_stack", "next", "ceo"),
        ("delivery", "t_live", "main", "ceo"),
        ("registry", "t_cloud", "v2", 12_500_000, "res_1", {"notes": "hi"}, "ceo"),
    ]


def test_community_refuses_app_and_registry_but_deploys_a_stack(client, engines, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    for target in ("t_live", "t_cloud"):
        r = _deploy(client, target)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "Paused in Community"
    r = _deploy(client, "t_stack")
    assert r.status_code == 200, r.text
    assert engines == [("stack", "t_stack", "main", "ceo")]
    detail = client.get("/v1/infra/targets/t_live", params={"project_id": HOME}).json()
    assert detail["disabled"] == {"deploy": "Paused in Community",
                                  "auto_deploy": "Paused in Community"}
    assert detail["can"]["deploy"] is False
    stack = client.get("/v1/infra/targets/t_stack", params={"project_id": HOME}).json()
    assert stack["can"]["deploy"] is True and "deploy" not in stack["disabled"]


def test_a_deploy_binds_its_actor_and_its_project(client, engines, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    r = client.post("/v1/infra/targets/t_live/deploy",
                    json={"project_id": HOME, "actor": "nobody-here"})
    assert r.status_code == 403, r.text
    r = client.post("/v1/infra/targets/no_such/deploy", json={"project_id": HOME, "actor": "ceo"})
    assert r.status_code == 404
    r = _deploy(client, "t_cloud", expected_cost_usd=-1)
    assert r.status_code == 422
    assert engines == []


def test_moving_a_target_attaches_it_to_the_machine(client, engine, engines):
    r = client.post("/v1/infra/targets/t_never/move",
                    json={"project_id": HOME, "actor": "ceo", "host_id": "h_ready"})
    assert r.status_code == 200, r.text
    assert r.json()["host_id"] == "h_ready"
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    ready = next(r for r in fleet["racks"] if r["id"] == "h_ready")
    assert "t_never" in {x["id"] for x in ready["residents"]}
    r = client.post("/v1/infra/targets/t_never/move",
                    json={"project_id": HOME, "actor": "ceo", "host_id": "h_retired"})
    assert r.status_code == 422
