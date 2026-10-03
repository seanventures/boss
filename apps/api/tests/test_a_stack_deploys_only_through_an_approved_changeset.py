"""A stack deploys to a VM through BOSS's own governance, from the console, end to end.

Plan → approve the decision → the worker's leased job applies the ChangeSet → healthy. The host
is a fake that records every command; GitHub is a fake that serves one manifest. What is real is
everything BOSS decides: the ChangeSet, the reservation, the decision, the job, the receipt, the
secret store, the idempotency, and the order — governance first, host second.
"""
from __future__ import annotations

import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import stack_deploy
from boss_engine.services.jobs import JobLeaseService

HOME = "boss-dogfood"
MANIFEST = """
version: 1
name: shop
env:
  PUBLIC_URL: "{target.public_url}"
  DB_PASSWORD: {generate: password}
install: ["./install.sh"]
update: ["./upgrade.sh"]
health: [{url: "http://127.0.0.1:8000/health", timeout_seconds: 30}]
open: {url: "{target.public_url}"}
"""


class FakeGitHub:
    def __init__(self):
        self.refs = {"main": "a" * 40, "next": "b" * 40, "pull/7": "e" * 40}

    def resolve_commit(self, owner, repo, ref):
        if ref in self.refs:
            return self.refs[ref]
        return ref

    def read_file(self, owner, repo, path, commit):
        return MANIFEST if path == "boss.deploy.yaml" else None

    def download_tarball(self, owner, repo, commit, dest):
        with open(dest, "wb") as handle:
            handle.write(b"tar")


class FakeHost:
    def __init__(self, journal):
        self.calls = []
        self.journal = journal
        self.installed = False

    def stack_exec(self, **kwargs):
        self.calls.append(kwargs)
        command = kwargs["command"]
        if kwargs["write"]:
            self.journal.append("write")
        if "BOSS_SSH_OK" in command:
            return subprocess.CompletedProcess(
                [], 0, "BOSS_SSH_OK\nDIR_CREATABLE\nSUDO_OK\nDOCKER_PRESENT\nDOCKER_USABLE\n"
                       "TAR_OK\nCURL_OK\n", "")
        out = ""
        if "BOSS_PREPARED" in command:
            out += ("BOSS_MODE=update\n" if self.installed else "BOSS_MODE=install\n")
            out += "BOSS_PREPARED\n"
        for marker in ("BOSS_UPLOADED", "BOSS_ENV_WRITTEN", "BOSS_SWITCHED", "BOSS_FINALIZED"):
            if f"echo {marker}" in command:
                out += marker + "\n"
        if "BOSS_FINALIZED" in command:
            self.installed = True
        if "BOSS_HEALTHY" in command:
            out += "BOSS_HEALTHY 200\n"
        return subprocess.CompletedProcess([], 0, out, "")

    def env_files(self):
        return [c["stdin_text"] for c in self.calls if "BOSS_ENV_WRITTEN" in c["command"]]

    def writes(self):
        return [c for c in self.calls if c["write"]]


@pytest.fixture()
def stack(client, engine, monkeypatch):
    journal: list[str] = []
    host = FakeHost(journal)
    github = FakeGitHub()
    monkeypatch.setattr(stack_deploy, "github_source_for", lambda *a, **k: github)
    monkeypatch.setattr(
        stack_deploy, "host_access_for",
        lambda connection, settings, row, adapter=None: stack_deploy.HostAccess(
            host=row["address"], user=row.get("ssh_user") or "root", port=22,
            key_material="KEYMATERIAL", adapter=host))
    governed = stack_deploy._require_governed

    def watched(*args, **kwargs):
        governed(*args, **kwargs)
        journal.append("governed")

    monkeypatch.setattr(stack_deploy, "_require_governed", watched)
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")

    created = client.post("/v1/deployment-hosts", json={
        "project_id": HOME, "name": f"vm-{uuid4().hex[:8]}", "address": "203.0.113.40",
        "ssh_user": "deploy", "actor": "ceo"})
    assert created.status_code == 200, created.text
    host_id = created.json()["id"]
    key_ref = f"sref_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                 "ref_key, secret_kind, storage_backend, created_by) VALUES "
                 "(:i, :p, 'deployment_host', :h, 'database', :k, 'private_key', 'database', "
                 "'test')"), {"i": key_ref, "p": HOME, "h": host_id, "k": f"K_{key_ref}"})
        connection.execute(text("UPDATE deployment_hosts SET status = 'verified', "
                                "key_secret_ref = :r WHERE id = :i"),
                           {"i": host_id, "r": key_ref})
    target = client.post("/v1/deployments/stack/targets", json={
        "project_id": HOME, "host_id": host_id, "repository": "acme/shop",
        "app_name": f"shop-{uuid4().hex[:6]}", "public_url": "https://shop.example.test",
        "actor": "ceo"})
    assert target.status_code == 201, target.text
    return {"host": host, "journal": journal, "target": target.json(), "github": github}


def _plan(client, stack, ref="main"):
    response = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": stack["target"]["id"], "ref": ref, "actor": "ceo"})
    assert response.status_code == 200, response.text
    return response.json()["deployment"]


def _approve_and_execute(client, engine, deployment):
    approved = client.post(f"/v1/decisions/{deployment['decision_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text
    with engine.begin() as connection:
        job = connection.execute(
            text("SELECT id FROM jobs WHERE kind = 'deployment.stack_apply' "
                 "AND payload->>'deployment_id' = :d"), {"d": deployment["id"]}).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=HOME, job_id=job, worker_id="w1", lease_ttl_seconds=600)
    body = {"job_id": job, "worker_id": "w1", "lease_token": lease["lease_token"]}
    executed = client.post(f"/v1/deployments/stack/{deployment['id']}/execute", json=body)
    assert executed.status_code == 200, executed.text
    return executed.json(), body


def test_planning_records_a_changeset_reservation_and_decision_and_writes_nothing(
        client, engine, stack):
    deployment = _plan(client, stack)

    assert deployment["status"] == "awaiting_approval"
    assert deployment["commit"] == "a" * 40
    assert deployment["manifest"]["env_keys"] == ["PUBLIC_URL", "DB_PASSWORD"]
    assert stack["host"].writes() == [], "planning wrote to the host"
    with engine.begin() as connection:
        changeset = connection.execute(
            text("SELECT status, risk_tier, reservation_id, approval_decision_id, operations "
                 "FROM changesets WHERE id = :c"),
            {"c": deployment["changeset_id"]}).mappings().one()
        reservation = connection.execute(
            text("SELECT amount_usd_micros, status FROM budget_reservations WHERE id = :r"),
            {"r": changeset["reservation_id"]}).mappings().one()
        decision = connection.execute(
            text("SELECT status, risk_tier, recommendation FROM decisions WHERE id = :d"),
            {"d": deployment["decision_id"]}).mappings().one()
    assert changeset["status"] == "pending_approval" and changeset["risk_tier"] == 2
    assert changeset["approval_decision_id"] == deployment["decision_id"]
    assert changeset["operations"][0]["skill_id"] == "ssh_host.stack.deploy"
    assert reservation["amount_usd_micros"] == 0 and reservation["status"] == "active"
    assert decision["status"] == "pending" and decision["risk_tier"] == 2
    assert decision["recommendation"]["action"] == "deploy_stack"


def test_the_same_commit_planned_twice_is_one_deployment(client, stack):
    first = _plan(client, stack)
    second = _plan(client, stack)
    assert second["id"] == first["id"]


def test_approval_queues_the_apply_and_the_host_is_written_only_after_governance(
        client, engine, stack):
    deployment = _plan(client, stack)
    result, _ = _approve_and_execute(client, engine, deployment)

    assert result["status"] == "healthy", result
    final = result["deployment"]
    assert final["open_url"] == "https://shop.example.test"
    assert [s["status"] for s in final["steps"]].count("failed") == 0
    journal = stack["journal"]
    assert "governed" in journal and "write" in journal
    assert journal.index("governed") < journal.index("write"), journal
    with engine.begin() as connection:
        changeset = connection.execute(text("SELECT status FROM changesets WHERE id = :c"),
                                       {"c": deployment["changeset_id"]}).scalar_one()
        receipt = connection.execute(
            text("SELECT status, provider FROM resource_receipts WHERE changeset_id = :c"),
            {"c": deployment["changeset_id"]}).mappings().one()
        reservation = connection.execute(
            text("SELECT r.status FROM budget_reservations r JOIN changesets c "
                 "ON c.reservation_id = r.id WHERE c.id = :c"),
            {"c": deployment["changeset_id"]}).scalar_one()
    assert changeset == "applied"
    assert receipt["status"] == "succeeded" and receipt["provider"] == "ssh_host"
    assert reservation == "consumed"


def test_a_generated_secret_is_minted_once_reused_and_never_returned(client, engine, stack):
    first, _ = _approve_and_execute(client, engine, _plan(client, stack, "main"))
    second, _ = _approve_and_execute(client, engine, _plan(client, stack, "next"))
    assert first["status"] == second["status"] == "healthy"

    env_files = stack["host"].env_files()
    passwords = {line.split("=", 1)[1] for env in env_files for line in env.splitlines()
                 if line.startswith("DB_PASSWORD=")}
    assert len(env_files) == 2 and len(passwords) == 1, "the password changed between deploys"
    value = passwords.pop().strip("'")
    assert second["deployment"]["steps"][0]["name"] == "prepare"
    assert "update: update 1" in [s["name"] for s in second["deployment"]["steps"]]
    listed = client.get("/v1/deployments/stack", params={"project_id": HOME})
    fetched = client.get(f"/v1/deployments/stack/{second['deployment']['id']}")
    for response in (listed, fetched):
        assert response.status_code == 200
        assert value not in response.text
    assert value not in str(first) and value not in str(second)
    with engine.begin() as connection:
        events = connection.execute(text("SELECT data::text FROM events WHERE project_id = :p"),
                                    {"p": HOME}).scalars().all()
        metadata = connection.execute(
            text("SELECT metadata::text FROM secret_refs WHERE scope_id = :t"),
            {"t": stack["target"]["id"]}).scalars().all()
    assert len(metadata) == 1 and "last4" not in metadata[0]
    assert not any(value in e for e in events)


def test_replaying_the_job_never_installs_twice(client, engine, stack):
    deployment = _plan(client, stack)
    result, body = _approve_and_execute(client, engine, deployment)
    writes = len(stack["host"].writes())
    again = client.post(f"/v1/deployments/stack/{deployment['id']}/execute", json=body)
    assert again.json()["status"] == "healthy"
    assert len(stack["host"].writes()) == writes


def test_a_job_replayed_after_a_crash_fails_cleanly_without_touching_the_host(
        client, engine, stack):
    deployment = _plan(client, stack)
    client.post(f"/v1/decisions/{deployment['decision_id']}/approve", json={"actor": "ceo"})
    with engine.begin() as connection:
        connection.execute(text("UPDATE stack_deployments SET apply_started_at = now() "
                                "WHERE id = :d"), {"d": deployment["id"]})
        job = connection.execute(
            text("SELECT id FROM jobs WHERE payload->>'deployment_id' = :d"),
            {"d": deployment["id"]}).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=HOME, job_id=job, worker_id="w1", lease_ttl_seconds=600)
    response = client.post(f"/v1/deployments/stack/{deployment['id']}/execute", json={
        "job_id": job, "worker_id": "w1", "lease_token": lease["lease_token"]})
    body = response.json()
    assert body["status"] == "failed"
    assert "interrupted" in body["deployment"]["error"]["headline"]
    assert stack["host"].writes() == []


def test_an_apply_outside_an_approved_changeset_is_refused_before_the_host(
        client, engine, stack):
    deployment = _plan(client, stack)
    with engine.begin() as connection:
        operation = dict(connection.execute(
            text("SELECT * FROM changeset_operations WHERE changeset_id = :c"),
            {"c": deployment["changeset_id"]}).mappings().one())
        with pytest.raises(PermissionError, match="approved ChangeSet"):
            stack_deploy.apply_stack_operation(
                connection, api_main.settings, operation, receipt_dedupe_key="x")
    assert stack["host"].writes() == []


def test_a_rejected_decision_rejects_the_deployment_and_releases_the_reservation(
        client, engine, stack):
    deployment = _plan(client, stack)
    rejected = client.post(f"/v1/decisions/{deployment['decision_id']}/reject",
                           json={"actor": "ceo", "note": "not today"})
    assert rejected.status_code == 200, rejected.text
    fetched = client.get(f"/v1/deployments/stack/{deployment['id']}").json()["deployment"]
    assert fetched["status"] == "rejected"
    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT r.status FROM budget_reservations r JOIN changesets c "
                 "ON c.reservation_id = r.id WHERE c.id = :c"),
            {"c": deployment["changeset_id"]}).scalar_one()
    assert status == "released"
    assert stack["host"].writes() == []


def test_rollback_plans_the_previous_healthy_commit_through_the_same_path(
        client, engine, stack):
    _approve_and_execute(client, engine, _plan(client, stack, "main"))
    second, _ = _approve_and_execute(client, engine, _plan(client, stack, "next"))
    response = client.post(f"/v1/deployments/stack/{second['deployment']['id']}/rollback",
                           json={"actor": "ceo"})
    assert response.status_code == 200, response.text
    rollback = response.json()["deployment"]
    assert rollback["commit"] == "a" * 40
    assert rollback["status"] == "awaiting_approval" and rollback["decision_id"]
    assert rollback["rollback_of"] == second["deployment"]["id"]


def test_a_manifest_error_refuses_the_plan_with_a_headline_and_an_action(client, stack):
    stack["github"].read_file = lambda *a: "version: 1\nname: shop\ninstall: ['{oops}']\n"
    response = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": stack["target"]["id"], "ref": "main",
        "actor": "ceo"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "{oops}" in detail["headline"] and "boss.deploy.yaml" in detail["action"]


def test_the_workflow_deploy_step_plans_a_stack_without_a_preview(
        client, engine, stack, monkeypatch):
    from types import SimpleNamespace

    from boss_engine.services import workflow_capabilities
    from boss_engine.services import workflow_capability_runner as runner

    # The run HAS a preview step and nothing is previewable: the legacy path would refuse.
    monkeypatch.setattr(runner, "_preview_missing_for_run",
                        lambda *a, **k: "the preview step is 'failed'")

    run_id = f"crun_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO code_task_runs (id, project_id, status, title, spec, "
                 "pull_request_number, pull_request_url) VALUES (:i, :p, 'completed', "
                 "'Add a shop feature', CAST(:s AS JSONB), 7, "
                 "'https://github.com/acme/shop/pull/7')"),
            {"i": run_id, "p": HOME, "s": '{"owner": "acme", "repo": "shop"}'})
    with engine.begin() as connection:
        ctx = SimpleNamespace(engine=engine, settings=api_main.settings)
        result = runner._perform_capability(
            connection, ctx, project_id=HOME, actor="ceo",
            step={"id": "deploy", "kind": "capability"},
            context={"run_id": None}, key="deploy",
            capability=workflow_capabilities.get("deploy"), inputs={"run_id": run_id})

    assert result["status"] == "awaiting", result
    assert result["await"]["kind"] == "stack_deployment"
    deployment = client.get(
        f"/v1/deployments/stack/{result['stack_deployment_id']}").json()["deployment"]
    assert deployment["commit"] == "e" * 40 and deployment["ref"] == "pull/7"
    assert deployment["code_task_run_id"] == run_id
