"""BOSSAPP-1408: changing a target's port updates open_url from the first poll of a redeploy."""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (
    FakeGitHub,
    FakeHost,
    HOME,
    _approve_and_execute,
)
from boss_engine.services import deploy_truth, deployment_fleet, infra_view, stack_deploy


def _setup_target_with_healthy_deploy(client, engine, monkeypatch, *, port: int = 8011):
    journal: list[str] = []
    host = FakeHost(journal)
    github = FakeGitHub()
    monkeypatch.setattr(stack_deploy, "source_for", lambda *a, **k: github)
    monkeypatch.setattr(
        stack_deploy, "host_access_for",
        lambda connection, settings, row, adapter=None: stack_deploy.HostAccess(
            host=row["address"], user=row.get("ssh_user") or "root", port=22,
            key_material="KEYMATERIAL", adapter=host))

    host_id = f"dh_{uuid4().hex[:10]}"
    key_ref = f"sref_{uuid4().hex[:10]}"
    target_id = f"deptgt_{uuid4().hex[:10]}"
    addr = "198.51.100.42"
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, "
                 "status, created_by) VALUES (:i, :p, :n, :a, 'deploy', 'verified', 'ceo')"),
            {"i": host_id, "p": HOME, "n": f"gw-{host_id[-4:]}", "a": addr})
        c.execute(
            text("INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                 "ref_key, secret_kind, storage_backend, created_by) VALUES "
                 "(:i, :p, 'deployment_host', :h, 'database', :k, 'private_key', 'database', 'test')"),
            {"i": key_ref, "p": HOME, "h": host_id, "k": f"K_{key_ref}"})
        c.execute(
            text("UPDATE deployment_hosts SET key_secret_ref = :r WHERE id = :i"),
            {"i": key_ref, "r": key_ref if False else key_ref} if False else {"i": host_id, "r": key_ref})
        c.execute(
            text("INSERT INTO deployment_targets "
                 "(id, project_id, app_name, repo, environment, target_kind, target_ref, "
                 " domains, public_url, health_checks, required_resources, status, host_id, "
                 " created_by) VALUES "
                 "(:id, :p, 'gateway-staging', 'acme/shop', 'staging', 'stack', :ref, "
                 " CAST(:dom AS JSONB), :pub, CAST(:hc AS JSONB), CAST(:rr AS JSONB), "
                 " 'active', :hid, 'ceo')"),
            {"id": target_id, "p": HOME, "ref": f"deploy@{addr}:22",
             "dom": json.dumps([f"{addr}:{port}"]),
             "pub": f"http://{addr}:{port}/",
             "hc": json.dumps([{"kind": "http", "scheme": "http", "path": "/healthz",
                                "expect_status": 200, "port": port}]),
             "rr": json.dumps([{"kind": "app_port", "name": str(port)}]),
             "hid": host_id})

    planned = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": target_id, "ref": "main", "actor": "ceo"})
    assert planned.status_code == 200, planned.text
    first = planned.json()["deployment"]
    assert first["open_url"] == f"http://{addr}:{port}"
    done, _ = _approve_and_execute(client, engine, first)
    assert done["status"] == "healthy"
    return target_id, addr, github


def test_port_update_reflects_in_open_url_from_first_poll_of_running_redeploy(
        client, engine, monkeypatch):
    target_id, addr, _github = _setup_target_with_healthy_deploy(
        client, engine, monkeypatch, port=8011)

    with engine.begin() as c:
        updated = deployment_fleet.update_deployment_target(
            c, project_id=HOME, target_id=target_id, port=8080, actor="ceo")
        assert updated["ok"] is True and updated["changed"]["port"] == 8080
        trow = c.execute(
            text("SELECT public_url, domains, health_checks, required_resources "
                 "FROM deployment_targets WHERE id = :i"),
            {"i": target_id}).mappings().one()
    assert trow["public_url"] == f"http://{addr}:8080/"
    assert trow["domains"] == [f"{addr}:8080"]
    assert trow["health_checks"][0]["port"] == 8080

    # Plan and approve the redeploy -> immediately transitions to status='running' (queued).
    redeploy = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": target_id, "ref": "next", "actor": "ceo"})
    assert redeploy.status_code == 200, redeploy.text
    dep = redeploy.json()["deployment"]
    assert dep["open_url"] == f"http://{addr}:8080"

    approved = client.post(f"/v1/decisions/{dep['decision_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text

    # First poll while status == 'running' (before worker executes/finalizes):
    with engine.connect() as c:
        polled = stack_deploy.get_stack_deployment(c, dep["id"])
        listed = stack_deploy.list_stack_deployments(c, HOME, target_id=target_id)
        targets = deploy_truth.project_targets(c, HOME)
        tr = deploy_truth.truth(c, targets)[target_id]
        detail = infra_view.deploy_detail(c, api_main.settings, HOME, f"stack:{dep['id']}")

    assert polled["status"] == "running"
    assert polled["open_url"] == f"http://{addr}:8080"
    assert listed[0]["open_url"] == f"http://{addr}:8080"
    assert tr["address"] == f"http://{addr}:8080/"
    assert tr["live_url"] == f"http://{addr}:8080/"
    assert detail["open_url"] == f"http://{addr}:8080"


def test_updating_target_port_while_redeploy_already_running_updates_open_url_immediately(
        client, engine, monkeypatch):
    target_id, addr, _github = _setup_target_with_healthy_deploy(
        client, engine, monkeypatch, port=8011)

    redeploy = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": target_id, "ref": "next", "actor": "ceo"})
    assert redeploy.status_code == 200, redeploy.text
    dep = redeploy.json()["deployment"]
    approved = client.post(f"/v1/decisions/{dep['decision_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text

    # Port changes while the redeploy is already in flight (running).
    with engine.begin() as c:
        updated = deployment_fleet.update_deployment_target(
            c, project_id=HOME, target_id=target_id, port=8080, actor="ceo")
        assert updated["ok"] is True

    with engine.connect() as c:
        polled = stack_deploy.get_stack_deployment(c, dep["id"])
        detail = infra_view.deploy_detail(c, api_main.settings, HOME, f"stack:{dep['id']}")
    assert polled["status"] == "running"
    assert polled["open_url"] == f"http://{addr}:8080"
    assert detail["open_url"] == f"http://{addr}:8080"


def test_infra_view_detail_and_rows_prefer_updated_target_port_over_stale_db_open_url(
        client, engine, monkeypatch):
    target_id, addr, _github = _setup_target_with_healthy_deploy(
        client, engine, monkeypatch, port=8011)

    with engine.connect() as c:
        first_id = c.execute(
            text("SELECT id FROM stack_deployments WHERE target_id = :t AND status = 'healthy' "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"t": target_id}).scalar_one()

    redeploy = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": target_id, "ref": "next", "actor": "ceo"})
    assert redeploy.status_code == 200, redeploy.text
    dep = redeploy.json()["deployment"]
    approved = client.post(f"/v1/decisions/{dep['decision_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text

    with engine.begin() as c:
        updated = deployment_fleet.update_deployment_target(
            c, project_id=HOME, target_id=target_id, port=8080, actor="ceo")
        assert updated["ok"] is True
        # Force stale :8011 into the running row's DB column as well to verify read-time resolution.
        c.execute(
            text("UPDATE stack_deployments SET open_url = :stale WHERE id = :i"),
            {"stale": f"http://{addr}:8011", "i": dep["id"]})

    with engine.connect() as c:
        running_detail = infra_view.deploy_detail(
            c, api_main.settings, HOME, f"stack:{dep['id']}")
        live_detail = infra_view.deploy_detail(
            c, api_main.settings, HOME, f"stack:{first_id}")
        rows = {
            r["ref"]: r
            for r in infra_view.deploy_rows(c, api_main.settings, HOME)["rows"]
        }

    assert running_detail["open_url"] == f"http://{addr}:8080"
    assert ":8011" not in (running_detail["open_url"] or "")
    assert ":8080" in (live_detail["open_url"] or "")
    assert ":8011" not in (live_detail["open_url"] or "")

    live_row = rows[f"stack:{first_id}"]
    assert ":8080" in (live_row["open_url"] or "")
    assert ":8011" not in (live_row["open_url"] or "")
    assert ":8011" not in (live_row["sentence"] or "")
