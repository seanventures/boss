"""P8a (BOSSAPP-1063) through the REAL app under BOSS_AUTHZ_MODE=enforce: the guard, the PDP and
the identity matrix's declarations, with REAL credentials — a host's `BossHost` proof signed by
its BOSS-issued certificate, a service account's real key forwarded through the run plane's
admit. boss-agent-hosts-lld.md §3.2, §3.13.2."""
from __future__ import annotations

import base64
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from boss_engine.identity.principal import PrincipalKind


@contextmanager
def enforced():
    """The guard enforces, and authenticates every request for real."""
    from boss_engine.config import get_settings

    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    try:
        yield
    finally:
        settings.authz_mode, settings.authz_router_modes = saved


@pytest.fixture
def fleet_world(engine, monkeypatch):
    from apps.api.app.main import app
    from apps.api.tests.identity_fixtures import IdentityWorld
    from boss_engine.agents import versions
    from boss_engine.identity import vault
    from boss_engine.services import agent_deployments, fleet_ca

    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"p8a-fleet-api-test-key-000000000").decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    fleet_ca._CA_CACHE.clear()
    world = IdentityWorld.create(engine)
    world.claimed()
    owner = world.person("Olive Owner")
    world.bind(owner, "owner")
    viewer = world.person("Vic Viewer")
    world.bind(viewer, "viewer")
    with engine.begin() as c:
        versions.save(c, world.project_a, {
            "apiVersion": "boss.agent/v1", "kind": "Agent",
            "metadata": {"key": "triage", "name": "Triage bot"},
            "spec": {"mission": "Triage issues.", "prompts": {"system": "I am v1."},
                     "limits": {"max_steps": 6}}}, actor="ceo", source="operator")
        agent_deployments.deploy(c, world.project_a, "triage", "dev", actor="ceo", version=1)
    yield {"world": world, "owner": owner, "viewer": viewer,
           "client": TestClient(app, raise_server_exceptions=False)}
    vault.reset_master_key_cache()
    fleet_ca._CA_CACHE.clear()


def test_hosts_enrol_beat_and_serve_the_run_plane_under_enforce(engine, fleet_world):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "engine" / "tests"))
    from _fleet_support import FakeHost  # noqa: E402

    from boss_engine.identity import api_keys
    from boss_engine.runtime.client import HttpTransport, RunPlaneClient, RunPlaneHTTPError

    world, client = fleet_world["world"], fleet_world["client"]
    project = world.project_a

    # A person issues the join token (fleet:manage); a viewer may not.
    with world.acting_as(world.ctx(fleet_world["viewer"])):
        refused = client.post("/v1/fleet/join-tokens", json={"pool": "default"})
    assert refused.status_code == 403, refused.text
    with world.acting_as(world.ctx(fleet_world["owner"])):
        issued = client.post("/v1/fleet/join-tokens", json={"pool": "default"})
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]

    with enforced():
        host = FakeHost(client)
        host.enrol(token)
        beat = host.heartbeat(facts={"kvm_live": True})
        assert beat.status_code == 200, beat.text
        assert client.post(f"/v1/hosts/{host.host_id}/heartbeat", json={}).status_code == 401

    # A person never acts as a host: fleet:host is system-only.
    with world.acting_as(world.ctx(fleet_world["owner"])):
        person = client.post(f"/v1/hosts/{host.host_id}/heartbeat", json={})
        assert person.status_code == 403, person.text
        hosts = client.get("/v1/fleet/hosts")
        assert [h["id"] for h in hosts.json()["hosts"]] == [host.host_id]
        placed = client.put("/v1/agents/triage/deployments/dev/runtime",
                            json={"project_id": project, "runtime": "microvm", "replicas": 1})
    assert placed.status_code == 200, placed.text
    assert placed.json()["runtime"] == "microvm" and placed.json()["placement_state"] == "ok"
    with engine.begin() as c:
        replica = c.execute(text("SELECT id FROM agent_replicas WHERE host_id = :h"),
                            {"h": host.host_id}).scalar()
        dep_id = c.execute(text("SELECT id FROM agent_deployments WHERE project_id = :p "
                                "AND agent_key = 'triage' AND environment = 'dev'"),
                           {"p": project}).scalar()

    def service_account(name: str, permissions: tuple) -> str:
        sa = world.principal(name, kind=PrincipalKind.SERVICE_ACCOUNT)
        world.bind(sa, world.custom_role(name.lower().replace(" ", "_"), permissions),
                   project=project)
        with engine.begin() as db:
            return api_keys.create(db, creator=world.ctx(fleet_world["owner"]), owner=sa,
                                   kind="sak", name=name, scopes=list(permissions),
                                   check_subset=False).key

    portal = service_account("Support portal", ("agent:invoke",))
    reader = service_account("Read only", ("agent:read",))
    with enforced():
        desired = host.desired(since=0)
        assert [v["replica_id"] for v in desired["vms"]] == [replica]
        rp = RunPlaneClient(HttpTransport(client, signer=host.signer()), replica)
        with pytest.raises(RunPlaneHTTPError) as forbidden:
            rp.admit(caller=f"Bearer {reader}", input="triage #311")
        assert forbidden.value.status == 403 and forbidden.value.code == "caller_forbidden"
        admitted = rp.admit(caller=f"Bearer {portal}", input="triage #311")
    assert admitted["status"] == "leased" and admitted["lease"]["fence_epoch"] == 1
    with engine.begin() as c:
        job = c.execute(text("SELECT lease_owner, payload FROM jobs WHERE id = :j"),
                        {"j": admitted["lease"]["job_id"]}).mappings().one()
        run = c.execute(text("SELECT status, deployment_id, job_id FROM agent_runs "
                             "WHERE id = :r"), {"r": admitted["run_id"]}).mappings().one()
    assert job["lease_owner"] == f"replica:{replica}" and job["payload"]["runtime"] == "microvm"
    assert run["status"] == "running" and run["deployment_id"] == dep_id, \
        "admitted, leased to this replica and started: one transaction"
    assert job["payload"]["_identity"]["on_behalf_of"] != "sys_host", \
        "the run acts for its caller, never for the host"
