"""A stack target is one write: what deploys, where, and the facts that make it a stack.

The console's "Add stack target" sent deploy_kind, manifest_path and public_url, and the create
endpoint dropped all three — so the target reloaded as an ordinary one and vanished from the
stack panel. Pointing it at its VM was a second call; when that failed the target existed with
a typed address and no host, a resubmit hit 409, and the orphan still offered Deploy.
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


def _resources(target: dict) -> dict:
    return {r["kind"]: r["name"] for r in target["required_resources"]}


def test_a_stack_target_round_trips_its_facts_and_its_host(client):
    suffix = uuid4().hex[:10]
    host = _host(client, suffix)
    created = client.post("/v1/deployment-targets", json={
        "project_id": HOME, "actor": "ceo", "app_name": f"stack-{suffix}",
        "environment": "production", "repo": "seanventures/boss",
        "deploy_kind": "stack", "manifest_path": "deploy/boss.deploy.yaml",
        "public_url": "https://boss.example.test", "host_id": host["id"],
    })
    assert created.status_code == 201, created.text

    target = _listed(client, created.json()["id"])
    assert target["host_id"] == host["id"]
    # Derived from the host row, never typed.
    assert target["target_ref"] == "deploy@203.0.113.41:22"
    assert target["target_kind"] == "ssh"
    resources = _resources(target)
    assert resources["deploy_kind"] == "stack"
    assert resources["manifest_path"] == "deploy/boss.deploy.yaml"
    assert resources["public_url"] == "https://boss.example.test"

    patched = client.patch(f"/v1/deployment-targets/{target['id']}", json={
        "actor": "ceo", "public_url": "https://boss2.example.test"})
    assert patched.status_code == 200, patched.text
    resources = _resources(_listed(client, target["id"]))
    assert resources["public_url"] == "https://boss2.example.test"
    assert resources["deploy_kind"] == "stack"
    assert resources["manifest_path"] == "deploy/boss.deploy.yaml"


def test_a_retired_host_refuses_the_target_and_leaves_no_orphan(client, engine):
    suffix = uuid4().hex[:10]
    host = _host(client, suffix)
    with engine.begin() as connection:
        connection.execute(text("UPDATE deployment_hosts SET status='retired' WHERE id=:i"),
                           {"i": host["id"]})
    body = {"project_id": HOME, "actor": "ceo", "app_name": f"orphan-{suffix}",
            "environment": "production", "repo": "seanventures/boss",
            "deploy_kind": "stack", "host_id": host["id"]}
    refused = client.post("/v1/deployment-targets", json=body)
    assert refused.status_code == 422, refused.text
    assert "retired" in refused.text
    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM deployment_targets WHERE project_id=:p AND app_name=:a"),
            {"p": HOME, "a": body["app_name"]}).scalar_one()
    assert count == 0


def test_an_unknown_host_refuses_the_target(client):
    refused = client.post("/v1/deployment-targets", json={
        "project_id": HOME, "actor": "ceo", "app_name": f"nohost-{uuid4().hex[:10]}",
        "environment": "production", "repo": "seanventures/boss", "host_id": "dh_missing"})
    assert refused.status_code == 422, refused.text


def test_a_bad_stack_fact_is_refused_at_write(client):
    refused = client.post("/v1/deployment-targets", json={
        "project_id": HOME, "actor": "ceo", "app_name": f"bad-{uuid4().hex[:10]}",
        "environment": "production", "repo": "seanventures/boss", "target_kind": "ssh",
        "target_ref": "root@203.0.113.42", "public_url": "boss.example.test"})
    assert refused.status_code == 422, refused.text
    assert "public_url" in refused.text


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
