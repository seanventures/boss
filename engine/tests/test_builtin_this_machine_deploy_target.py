"""BOSSAPP-1440: unit/engine checks for built-in `'this machine'` local deploy target."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import (
    deployment_connectors,
    deployment_fleet,
    governed_host_deploy,
    infra_view,
    production,
    stack_deploy,
)

from ._obs_seed import pg_engine


@pytest.fixture()
def world():
    eng = pg_engine()
    conn = eng.connect()
    tx = conn.begin()
    org = f"org_{uuid4().hex[:8]}"
    pid = f"p_{uuid4().hex[:8]}"
    conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
    conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :o)"),
                 {"w": f"ws_{org}", "o": org})
    conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                 {"p": pid, "w": f"ws_{org}"})
    yield eng, conn, pid
    tx.rollback()
    conn.close()


def test_local_only_install_auto_ensures_verified_this_machine_host(world, monkeypatch):
    _eng, conn, pid = world
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    host = deployment_fleet.ensure_local_host(conn, pid, actor="bootstrap")
    assert host is not None
    assert host["name"] == "this machine"
    assert host["address"] == "127.0.0.1"
    assert host["status"] == "verified"
    assert (host["port_min"], host["port_max"]) == (8000, 8999)
    assert deployment_fleet.is_builtin_local_host(host) is True
    # Idempotent: calling again returns the same host.
    host2 = deployment_fleet.ensure_local_host(conn, pid, actor="bootstrap")
    assert host2["id"] == host["id"]

    # Infra word & stage read Ready without SSH or Docker.
    h_ready = infra_view._host_ready(host)
    assert infra_view.host_word(h_ready) == {"word": "Ready", "tone": "green"}
    assert infra_view.host_stage(h_ready)["stage"] == "ready"


def test_this_machine_target_counts_as_dev_and_validates_without_ssh_or_docker(world, monkeypatch, tmp_path):
    eng, conn, pid = world
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    monkeypatch.setattr(deployment_fleet, "_first_free_port", lambda *a, **k: 8005)
    made = deployment_fleet.ensure_this_machine_target(
        conn, pid, app_name="Sample App", repo=f"local_{pid}/sample",
        environment="staging", health_path="/healthz")
    assert made["ok"] is True
    assert made["target_kind"] == "local"
    assert made["address"] == "127.0.0.1"
    assert made["port"] == 8005

    verdict = production.resolve_environment("", host="127.0.0.1")
    assert verdict.tier == "dev" and verdict.prod is False and verdict.source == "local_host"
    assert governed_host_deploy.is_host_kind("local") is True

    connector = deployment_connectors.connector_for({"target_kind": "local"})
    assert isinstance(connector, deployment_connectors.LocalHostConnector)
    assert connector.required_secrets() == []

    settings = Settings(artifact_dir=str(tmp_path / "artifacts"))
    target = {
        "id": made["target_id"], "project_id": pid,
        "app_name": "Sample App", "repo": "sample", "target_kind": "local",
        "target_ref": made["target_ref"], "domains": ["127.0.0.1:8005"],
        "health_checks": [{"kind": "http", "scheme": "http", "path": "/healthz",
                           "expect_status": 200, "port": 8005}],
        "required_resources": [{"kind": "app_port", "name": "8005"}],
    }
    assert connector.validate(target, settings) == []
    # Ungoverned apply must remain refused.
    blocked = connector.apply(eng, settings, target, "", {}, governance=None)
    assert blocked["status"] == "blocked"


def test_local_host_adapter_runs_without_ssh_or_docker(world, tmp_path):
    _eng, conn, pid = world
    settings = Settings(artifact_dir=str(tmp_path / "artifacts"))
    host = deployment_fleet.ensure_local_host(conn, pid, actor="test")
    access = stack_deploy.host_access_for(conn, settings, host)
    assert isinstance(access.adapter, stack_deploy.LocalHostAdapter)
    res = access.run(
        "mkdir -p /opt/boss-apps/demo && echo LOCAL_OK > /opt/boss-apps/demo/ok.txt "
        "&& cat /opt/boss-apps/demo/ok.txt")
    assert res.returncode == 0
    assert "LOCAL_OK" in res.stdout
    assert (settings.artifact_root / "local-deploys" / host["id"] / "demo" / "ok.txt").is_file()


def test_prod_install_tier_does_not_auto_create_local_host(world, monkeypatch):
    _eng, conn, pid = world
    monkeypatch.setenv("BOSS_INSTALL_TIER", "prod")
    assert deployment_fleet.ensure_local_host(conn, pid) is None
