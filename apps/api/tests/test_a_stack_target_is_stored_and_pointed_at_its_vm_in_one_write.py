"""A stack target is one write, through one route: what deploys, where, and its stack facts.

POST /v1/deployments/stack/targets creates the target bound to its host in one transaction and
stores the stack facts in their canonical columns (target_kind='stack', manifest_path,
public_url). The target list returns those columns, which is what the console reads. The generic
POST /v1/deployment-targets refuses anything stack-shaped, so there is no second write path that
could store the facts somewhere the deploy never reads.
"""

from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main


HOME = "boss-dogfood"


def _host(client, suffix: str) -> dict:
    created = client.post(
        "/v1/deployment-hosts",
        json={"project_id": HOME, "name": f"stack-{suffix}", "address": "203.0.113.41",
              "ssh_user": "deploy", "actor": "ceo"},
    )
    assert created.status_code == 200, created.text
    return created.json()


def _listed(client, target_id: str) -> dict:
    listed = client.get(f"/v1/deployment-targets?project_id={HOME}&limit=200")
    assert listed.status_code == 200, listed.text
    return next(t for t in listed.json()["items"] if t["id"] == target_id)


def _count(engine, app_name: str) -> int:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT count(*) FROM deployment_targets WHERE project_id=:p AND app_name=:a"),
            {"p": HOME, "a": app_name}).scalar_one()


def test_a_stack_target_round_trips_its_facts_and_its_host(client):
    suffix = uuid4().hex[:10]
    host = _host(client, suffix)
    created = client.post("/v1/deployments/stack/targets", json={
        "project_id": HOME, "actor": "ceo", "host_id": host["id"],
        "repository": "seanventures/boss", "app_name": f"stack-{suffix}",
        "environment": "production", "manifest_path": "deploy/boss.deploy.yaml",
        "public_url": "https://boss.example.test",
    })
    assert created.status_code == 201, created.text

    target = _listed(client, created.json()["id"])
    assert target["host_id"] == host["id"]
    # Derived from the host row, never typed.
    assert target["target_ref"] == "deploy@203.0.113.41:22"
    assert target["target_kind"] == "stack"
    assert target["repo"] == "seanventures/boss"
    assert target["manifest_path"] == "deploy/boss.deploy.yaml"
    assert target["public_url"] == "https://boss.example.test"
    kinds = {r.get("kind") for r in target["required_resources"]}
    assert not kinds & {"deploy_kind", "manifest_path", "public_url"}


def test_a_retired_host_refuses_the_target_and_leaves_no_orphan(client, engine):
    suffix = uuid4().hex[:10]
    host = _host(client, suffix)
    with engine.begin() as connection:
        connection.execute(text("UPDATE deployment_hosts SET status='retired' WHERE id=:i"),
                           {"i": host["id"]})
    name = f"orphan-{suffix}"
    refused = client.post("/v1/deployments/stack/targets", json={
        "project_id": HOME, "actor": "ceo", "host_id": host["id"],
        "repository": "seanventures/boss", "app_name": name})
    assert refused.status_code == 422, refused.text
    assert "retired" in refused.text
    assert _count(engine, name) == 0


def test_a_bad_stack_fact_is_refused_at_write(client, engine):
    host = _host(client, uuid4().hex[:10])
    name = f"bad-{uuid4().hex[:10]}"
    refused = client.post("/v1/deployments/stack/targets", json={
        "project_id": HOME, "actor": "ceo", "host_id": host["id"],
        "repository": "seanventures/boss", "app_name": name,
        "public_url": "boss.example.test"})
    assert refused.status_code == 422, refused.text
    assert "public_url" in refused.text
    assert _count(engine, name) == 0


def test_the_generic_target_route_refuses_anything_stack_shaped(client, engine):
    host = _host(client, uuid4().hex[:10])
    for extra in ({"target_kind": "stack"}, {"deploy_kind": "stack"},
                  {"manifest_path": "boss.deploy.yaml"},
                  {"public_url": "https://boss.example.test"}):
        name = f"generic-{uuid4().hex[:10]}"
        refused = client.post("/v1/deployment-targets", json={
            "project_id": HOME, "actor": "ceo", "app_name": name,
            "environment": "production", "repo": "seanventures/boss",
            "host_id": host["id"], **extra})
        assert refused.status_code == 422, (extra, refused.text)
        assert "/v1/deployments/stack/targets" in refused.text
        assert _count(engine, name) == 0


def test_an_unknown_host_refuses_the_target(client):
    refused = client.post("/v1/deployment-targets", json={
        "project_id": HOME, "actor": "ceo", "app_name": f"nohost-{uuid4().hex[:10]}",
        "environment": "production", "repo": "seanventures/boss", "host_id": "dh_missing"})
    assert refused.status_code == 422, refused.text


def test_default_settings_let_the_hosts_panel_add_and_verify_a_vm(client, monkeypatch):
    for edition in ("community", "enterprise_self_hosted"):
        monkeypatch.setattr(api_main.settings, "boss_edition", edition)
        monkeypatch.setattr(api_main.settings, "ssh_host_deploy_allowed", False)
        monkeypatch.setattr(api_main.settings, "ssh_outward_allowed", True)
        listed = client.get(f"/v1/deployment-hosts?project_id={HOME}")
        assert listed.status_code == 200, listed.text
        pre = listed.json()["preconditions"]
        assert pre["blocker"] is None
        assert pre["stack_deploy_allowed"] is True
        assert pre["host_writes_allowed"] is False

    monkeypatch.setattr(api_main.settings, "ssh_outward_allowed", False)
    pre = client.get(f"/v1/deployment-hosts?project_id={HOME}").json()["preconditions"]
    assert pre["blocker"]["setting"] == "BOSS_SSH_OUTWARD_ALLOWED"
    assert pre["stack_deploy_allowed"] is False
