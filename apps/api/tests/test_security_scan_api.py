"""Security scan API keeps reads while refusing ungoverned host scans."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import security_scan as sec


PROJECT_ID = "boss-dogfood"


@pytest.fixture(autouse=True)
def _clear_scan_store():
    sec.reset_store()
    yield
    sec.reset_store()


def test_scanners_catalog_stays_readable_but_execution_is_unavailable(client):
    r = client.get("/v1/security-scan/scanners")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert {"semgrep", "trivy", "osv-scanner", "gitleaks", "checkov"} <= ids
    assert "snyk" in ids
    assert "snyk" not in body["supported"]
    assert body["execution_available"] is False
    assert "workspace" in body["notes"]["execution"]

    supported = client.get(
        "/v1/security-scan/scanners", params={"include_planned": False}
    )
    assert supported.status_code == 200, supported.text
    assert "snyk" not in {item["id"] for item in supported.json()["items"]}


def test_run_requires_authenticated_member_before_any_scanner_effect(
    client, monkeypatch,
):
    effects = []
    monkeypatch.setattr(sec, "run_scan", lambda **_kw: effects.append("scan"))
    monkeypatch.setattr(sec.subprocess, "run", lambda *_a, **_kw: effects.append("subprocess"))

    for body in (
        {"project_id": PROJECT_ID, "actor": "ceo", "path": "/etc"},
        {"project_id": PROJECT_ID, "actor": "ceo", "simulated": True},
    ):
        r = client.post("/v1/security-scan/run", json=body)
        assert r.status_code == 401, r.text
    assert effects == []
    assert sec.list_runs(PROJECT_ID) == []


def test_run_checks_requested_project_and_actor_then_blocks_every_mode(
    client, engine, monkeypatch,
):
    suffix = uuid4().hex[:10]
    operator = f"scan_operator_{suffix}"
    reader = f"scan_reader_{suffix}"
    foreign = f"scan_foreign_{suffix}"
    operator_key = f"scan-key-{suffix}"
    reader_key = f"scan-reader-key-{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", f"{operator_key},{reader_key}")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals",
        f"{operator_key}:{operator},{reader_key}:{reader}",
    )
    operator_headers = {"Authorization": f"Bearer {operator_key}"}
    reader_headers = {"Authorization": f"Bearer {reader_key}"}

    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"),
            {"id": PROJECT_ID},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :workspace_id, 'Foreign scanner', :id)"),
            {"id": foreign, "workspace_id": workspace_id},
        )
        for member_id, permissions in (
            (operator, ["providerops_manage"]), (reader, []),
        ):
            api_main.members.upsert_member(
                connection, project_id=PROJECT_ID, member_id=member_id,
                display_name=member_id, member_type="human", role_keys=[],
                permissions=permissions, actor="ceo",
            )

    effects = []
    monkeypatch.setattr(sec, "run_scan", lambda **_kw: effects.append("scan"))
    monkeypatch.setattr(sec.subprocess, "run", lambda *_a, **_kw: effects.append("subprocess"))
    path = "/v1/security-scan/run"
    denied = client.post(
        path, headers=operator_headers,
        json={"project_id": foreign, "actor": operator, "path": "/etc"},
    )
    assert denied.status_code == 403, denied.text
    denied = client.post(
        path, headers=reader_headers,
        json={"project_id": PROJECT_ID, "actor": reader, "path": "/etc"},
    )
    assert denied.status_code == 403, denied.text
    denied = client.post(
        path, headers=operator_headers,
        json={"project_id": PROJECT_ID, "actor": "ceo", "path": "/etc"},
    )
    assert denied.status_code == 403, denied.text

    for body in (
        {"project_id": PROJECT_ID, "actor": operator, "path": "/etc"},
        {"project_id": PROJECT_ID, "actor": operator},
        {"project_id": PROJECT_ID, "actor": operator, "simulated": True},
    ):
        blocked = client.post(path, headers=operator_headers, json=body)
        assert blocked.status_code == 409, blocked.text
        assert "project workspace" in blocked.text
        assert "receipt" in blocked.text
    assert effects == []
    assert sec.list_runs(PROJECT_ID) == []
    assert sec.list_runs(foreign) == []


def test_prior_runs_and_gate_remain_readable(client):
    run = sec.run_scan(
        project_id=PROJECT_ID, actor="test_operator", path="/tmp/fixture",
        scanners=["gitleaks"], simulated=True, engine=None,
    )
    assert run["gate"]["decision"] == "no-go"

    listed = client.get("/v1/security-scan/runs", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["id"] == run["id"]
    latest = client.get("/v1/security-scan/latest", params={"project_id": PROJECT_ID})
    assert latest.status_code == 200, latest.text
    assert latest.json()["run"]["id"] == run["id"]
    got = client.get(f"/v1/security-scan/runs/{run['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["findings"]
    gate = client.post(f"/v1/security-scan/runs/{run['id']}/gate", json={})
    assert gate.status_code == 200, gate.text
    assert gate.json()["gate"]["decision"] == "no-go"
    assert client.get("/v1/security-scan/runs/secscan_missing").status_code == 404


def test_latest_empty_and_engine_fixture_behavior(client):
    latest = client.get("/v1/security-scan/latest", params={"project_id": PROJECT_ID})
    assert latest.status_code == 200, latest.text
    assert latest.json()["status"] == "not_configured"
    assert latest.json()["run"] is None
    with pytest.raises(ValueError, match="unknown|unsupported"):
        sec.run_scanner("snyk", "/tmp", simulated=True)
    result = sec.run_scanner("semgrep", "/tmp/project", simulated=True)
    assert result.status == "simulated"
    assert result.findings[0].to_dict()["scanner"] == "semgrep"
