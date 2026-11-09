"""A new project's onboarding offers the org's shared, verified host for staging — and "Use for
staging" really makes the target there, in place.

2026-11-07 (helpdesk from paramg/helpdesk-ai-v3): onboarding offered 2.29.2.202, a verified host
registered under helpdesk-ai-demo, but the button opened Infra's generic App target sheet
(production, no port) and the create refused it as raw JSON: {"headline":"No such host in this
project.",...}. Target creation asked "is the host in THIS project?" while every other path asked
deployment_fleet.host_usable_by. One rule now (deployment_fleet.usable_host): this project's
hosts, and the verified hosts its organisation shares; never an unverified shared host, never
another organisation's.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from apps.api.tests.test_the_onboarding_agent_readies_a_project import (  # noqa: F401 (fixtures)
    StubProbes, _mapped, _project, _run_id, _run_record_is_the_model_answer, _view, checkout,
    client, world,
)
from boss_engine.config import get_settings
from boss_engine.services import deployment_fleet as fleet
from boss_engine.services.project_experience import onboarding
from boss_engine.services.stack_deploy import StackDeployError, create_stack_target


def _host(engine, project_id: str, *, status: str = "verified", address: str | None = None) -> str:
    hid = f"dh_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status, "
            "port_min, port_max, created_by) VALUES (:i, :p, :n, :a, 'deploy', :s, 8000, 8099, 'test')"),
            {"i": hid, "p": project_id, "n": f"edge-{hid[-4:]}", "s": status,
             "a": address or f"203.0.113.{int(hid[-2:], 16) % 250 + 1}"})
    return hid


def _foreign_project(engine) -> str:
    org = f"org_{uuid.uuid4().hex[:8]}"
    pid = f"p_{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
        db.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :o)"),
                   {"w": f"ws_{org}", "o": org})
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                   {"p": pid, "w": f"ws_{org}"})
    return pid


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    """The free-port probe also asks the machine; here no port answers, so only the ports BOSS
    already promised (deployment_fleet._ports_assigned_on) are taken."""
    def quiet(connection, address, *, start=3000, end=None):
        taken = fleet._ports_assigned_on(connection, address)
        return next(p for p in range(max(1024, start), (end or 65535) + 1) if p not in taken)

    monkeypatch.setattr(fleet, "_first_free_port", quiet)


def _onboarded(world, client, engine, checkout) -> str:
    """A new project, onboarded to the point where only the deploy confirmation is left."""
    pid = _project(world, client, engine)
    _mapped(engine, pid)
    (checkout / "boss.deploy.yaml").write_text(
        "version: 1\nname: helpdesk\nkind: commands\ninstall: [{name: up, run: npm start}]\n")
    onboarding.advance(engine, get_settings(), _run_id(engine, pid),
                       probes=StubProbes(checkout, ci="passed"), requeue=False)
    return pid


def test_use_for_staging_makes_the_target_on_the_shared_host_in_place(world, client, engine, checkout):
    shared = _host(engine, world.project_a)
    pid = _onboarded(world, client, engine, checkout)
    run = _view(engine, pid)
    assert run["state"] == "needs_you" and [i["key"] for i in run["only_you"]] == ["deploy_target"]
    action = {i["key"]: i for i in run["checklist"]}["deploy_target"]["action"]
    # The action is done ON the page: a confirm sheet that POSTs, not a trip to Infra.
    assert action["do"] == "staging_target" and action["host_id"] == shared
    assert action["post"] == f"/v1/projects/{pid}/onboarding/deploy-target"
    assert "env=staging" in action["href"]

    with world.acting_as(world.ctx(world.owner)):
        plan = client.get(f"/v1/projects/{pid}/onboarding/deploy-target",
                          params={"host_id": shared})
    assert plan.status_code == 200, plan.text
    plan = plan.json()
    assert plan["environment"] == "staging" and plan["shared"] is True
    assert plan["manifest_path"] == "boss.deploy.yaml" and plan["port"] == 8000
    assert plan["repository"] == "acme/helpdesk-ai-demo" and plan["project"] == "HelpDesk AI Demo"

    with world.acting_as(world.ctx(world.owner)):
        made = client.post(f"/v1/projects/{pid}/onboarding/deploy-target",
                           json={"host_id": shared, "port": plan["port"]})
    assert made.status_code == 200, made.text
    with engine.connect() as db:
        target = db.execute(text(
            "SELECT environment, host_id, domains, required_resources, manifest_path, project_id "
            "FROM deployment_targets WHERE id = :t"), {"t": made.json()["target_id"]}).mappings().one()
    assert target["project_id"] == pid and target["host_id"] == shared
    assert target["environment"] == "staging" and target["manifest_path"] == "boss.deploy.yaml"
    assert target["domains"] == [f"{plan['address']}:8000"]
    assert {"kind": "app_port", "name": "8000"} in target["required_resources"]

    # The row is green and the run moved on, without a re-read.
    after = _view(engine, pid)
    deploy = {i["key"]: i for i in after["checklist"]}["deploy_target"]
    assert deploy["state"] == "done" and deploy["owner"] == "agent" and deploy["action"] is None
    assert after["state"] == "ready" and after["only_you"] == []
    assert made.json()["onboarding"]["state"] == "ready"

    # Re-running onboarding sees the target and marks deploy_target done itself.
    with world.acting_as(world.ctx(world.owner)):
        again = client.post(f"/v1/projects/{pid}/onboarding/start")
    assert again.status_code == 200, again.text
    onboarding.advance(engine, get_settings(), _run_id(engine, pid),
                       probes=StubProbes(checkout, ci="passed"), requeue=False)
    rerun = _view(engine, pid)
    assert {i["key"]: i for i in rerun["checklist"]}["deploy_target"]["state"] == "done"

    # The next free port on that host skips the one staging now holds.
    with world.acting_as(world.ctx(world.owner)):
        nxt = client.get(f"/v1/projects/{pid}/hosts/{shared}/free-port")
    assert nxt.status_code == 200, nxt.text
    assert nxt.json()["port"] == 8001


def test_a_viewer_cannot_add_the_staging_target(world, client, engine, checkout):
    shared = _host(engine, world.project_a)
    pid = _onboarded(world, client, engine, checkout)
    with world.acting_as(world.ctx(world.viewer)):
        refused = client.post(f"/v1/projects/{pid}/onboarding/deploy-target", json={"host_id": shared})
    assert refused.status_code in (403, 404), refused.text
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM deployment_targets WHERE project_id = :p"),
                          {"p": pid}).scalar() == 0


def test_a_refusal_is_headline_and_action_in_words(world, client, engine, checkout):
    pid = _onboarded(world, client, engine, checkout)
    pending = _host(engine, world.project_a, status="pending")
    with world.acting_as(world.ctx(world.owner)):
        refused = client.post(f"/v1/projects/{pid}/onboarding/deploy-target", json={"host_id": pending})
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert "not verified" in detail["headline"] and detail["action"]
    assert detail["message"] == f"{detail['headline']} {detail['action']}"
    # Another organisation's machine is not even named: identity answers 404 before the route.
    theirs = _host(engine, _foreign_project(engine))
    with world.acting_as(world.ctx(world.owner)):
        hidden = client.post(f"/v1/projects/{pid}/onboarding/deploy-target", json={"host_id": theirs})
    assert hidden.status_code == 404, hidden.text


def test_a_stack_target_goes_on_a_shared_verified_host_and_nowhere_it_may_not(world, client,
                                                                             engine):
    with engine.begin() as db:
        sibling = world.project_b
        own_unverified = _host(engine, sibling, status="pending")
        shared = _host(engine, world.project_a)
        shared_unverified = _host(engine, world.project_a, status="pending")
        foreign = _host(engine, _foreign_project(engine))

        made = create_stack_target(db, project_id=sibling, host_id=shared, app_name="hd",
                                   repository="paramg/helpdesk-ai-v3", environment="staging",
                                   port=8005, actor="test")
        assert made["host_id"] == shared and made["project_id"] == sibling
        # The project's own host keeps working while it is being set up, as before.
        assert create_stack_target(db, project_id=sibling, host_id=own_unverified, app_name="own",
                                   repository="paramg/helpdesk-ai-v3", actor="test")["id"]

        with pytest.raises(StackDeployError) as unverified:
            create_stack_target(db, project_id=sibling, host_id=shared_unverified, app_name="u",
                                repository="paramg/helpdesk-ai-v3", actor="test")
        assert "not verified" in unverified.value.headline and unverified.value.status_code == 422
        with pytest.raises(StackDeployError) as stranger:
            create_stack_target(db, project_id=sibling, host_id=foreign, app_name="f",
                                repository="paramg/helpdesk-ai-v3", actor="test")
        assert stranger.value.status_code == 404
        # A port another app holds on the machine is refused, in words.
        with pytest.raises(StackDeployError) as clash:
            create_stack_target(db, project_id=sibling, host_id=shared, app_name="hd2",
                                repository="paramg/helpdesk-ai-v3", port=8005, actor="test")
        assert clash.value.status_code == 409 and "8005" in clash.value.headline

        # Every create/attach path asks the same rule.
        assert fleet.usable_host(db, shared, sibling)["id"] == shared
        assert not fleet.host_usable_by(db, shared_unverified, sibling)
        assert not fleet.host_usable_by(db, foreign, sibling)
        assert shared_unverified not in {h["id"] for h in fleet.list_hosts(db, sibling)}
        lonely = fleet.create_deployment_target(db, project_id=sibling, app_name="Lonely",
                                                host_id=foreign)
        assert lonely["ok"] is False and "organisation" in lonely["error"]
