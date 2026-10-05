"""Retiring a machine needs its name, typed (spec §4.4): the server checks the typed name too,
then runs the existing retire path with the confirm sheet's four options. Community keeps host
retirement behind the direct-host-write policy, exactly as the old route does."""
from __future__ import annotations

import pytest

from apps.api.app import main as api_main
from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def retire(engine, monkeypatch):
    from boss_engine.services import deployment_fleet

    seed(engine, HOME, samples=False)
    calls = []

    def fake(connection, settings, *, host_id, revoke_key, remove_docker, purge, force, actor):
        calls.append({"host_id": host_id, "revoke_key": revoke_key,
                      "remove_docker": remove_docker, "purge": purge, "force": force,
                      "actor": actor})
        return {"host_id": host_id, "status": "retired"}

    monkeypatch.setattr(deployment_fleet, "retire_host", fake)
    yield calls
    with engine.begin() as c:
        clear(c, HOME)


def _retire(client, **body):
    return client.post("/v1/infra/hosts/h_down/retire",
                       json={"project_id": HOME, "actor": "ceo", **body})


def test_the_wrong_name_retires_nothing(client, retire, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    for typed in ("", "edge", "EDGE-2", "edge-ready"):
        r = _retire(client, confirm_name=typed)
        assert r.status_code == 422, (typed, r.text)
    assert retire == []


def test_the_right_name_runs_the_retire_path_with_the_four_options(client, retire, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    r = _retire(client, confirm_name=" edge-2 ", revoke_key=True, uninstall_docker=True,
                delete_record=False, detach_apps=True)
    assert r.status_code == 200, r.text
    assert retire == [{"host_id": "h_down", "revoke_key": True, "remove_docker": True,
                       "purge": False, "force": True, "actor": "ceo"}]


def test_community_keeps_retire_behind_the_host_write_policy(client, retire, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    r = _retire(client, confirm_name="edge-2")
    assert r.status_code == 409, r.text
    assert retire == []
    host = client.get("/v1/infra/hosts/h_down", params={"project_id": HOME}).json()
    assert host["can"]["retire"] is False


def test_retire_needs_the_permission_and_the_project(client, retire, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", True)
    assert _retire(client, confirm_name="edge-2", actor="nobody-here").status_code == 403
    r = client.post("/v1/infra/hosts/nope/retire",
                    json={"project_id": HOME, "actor": "ceo", "confirm_name": "nope"})
    assert r.status_code == 404
    assert retire == []
