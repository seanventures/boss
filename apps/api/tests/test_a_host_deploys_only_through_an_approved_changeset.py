"""An app deploys to a verified SSH host through BOSS's own governance, end to end.

Plan → a person approves the decision → the worker's leased job applies the ChangeSet through
the SSH connector (the September deploy path) → health check → receipt. SSH, the build and the
external health probe are fakes that record what they were asked; everything BOSS decides is
real: the ChangeSet, the zero-cost reservation, the decision, the job, the phased apply, the
ResourceReceipt, the rollback and the refusals of every other way in.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import (
    delivery_pipeline, deploy_service, deployment_connectors, governed_host_deploy,
    secret_backends,
)
from boss_engine.services.decisions import InvalidDecisionTransitionError
from boss_engine.services.jobs import JobLeaseService

HOME = "boss-dogfood"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETKEYMATERIAL-9f8e7d\n-----END OPENSSH PRIVATE KEY-----"


class FakeHost:
    """Every ssh command BOSS sends, and canned answers. `healthy` drives the health gates."""

    def __init__(self):
        self.commands: list[str] = []
        self.transfers = 0
        self.healthy = True
        self.observed_healthy = True

    def ssh(self, key_path, host, user, port, command, timeout=180):
        self.commands.append(command)
        if "BASE_OK" in command:
            return subprocess.CompletedProcess([], 0, "BASE_OK\nAPP_OK\nDOCKER_OK\n", "")
        if "docker version" in command:
            return subprocess.CompletedProcess([], 0, "27.0\n", "")
        return subprocess.CompletedProcess([], 0, "", "")

    def writes(self):
        return [c for c in self.commands
                if "docker run" in c or "dist.prev" in c or "docker restart" in c]


@pytest.fixture()
def hosted(client, engine, monkeypatch, tmp_path):
    host = FakeHost()
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    monkeypatch.setattr(deploy_service, "_ssh", host.ssh)
    monkeypatch.setattr(deploy_service, "github_token_for", lambda *a, **k: "")

    def build(settings, owner, repo, branch, **kwargs):
        work = Path(tmp_path) / f"work-{uuid4().hex[:6]}"
        (work / "dist").mkdir(parents=True)
        return work, "static", "", "c0ffee" * 6 + "abcd"

    monkeypatch.setattr(deploy_service, "_build_dist", build)

    def transfer(*args, **kwargs):
        host.transfers += 1
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(deploy_service.owned_process, "run", transfer)
    monkeypatch.setattr(deploy_service, "_await_healthy",
                        lambda *a, **k: ("200" if host.healthy else "500", 1))
    monkeypatch.setattr(
        deployment_connectors.SSHContainerConnector, "observe",
        lambda self, target, settings: (
            {"healthy": True, "checks": [{"url": "http://203.0.113.40:3010/", "ok": True}]}
            if host.observed_healthy else
            {"healthy": False, "checks": [{"url": "http://203.0.113.40:3010/",
                                            "status": 502, "expected": 200, "ok": False}]}))
    monkeypatch.setattr(secret_backends, "resolve_secret_ref", lambda *a, **k: KEY)

    created = client.post("/v1/deployment-hosts", json={
        "project_id": HOME, "name": f"hetzner-ci-{uuid4().hex[:4]}", "address": "203.0.113.40",
        "ssh_user": "deploy", "actor": "ceo"})
    assert created.status_code == 200, created.text
    host_row = created.json()
    key_ref = f"sref_{uuid4().hex[:10]}"
    target_id = f"deptgt_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                 "ref_key, secret_kind, storage_backend, created_by) VALUES "
                 "(:i, :p, 'deployment_host', :h, 'database', :k, 'private_key', 'database', "
                 "'test')"), {"i": key_ref, "p": HOME, "h": host_row["id"], "k": f"K_{key_ref}"})
        connection.execute(text("UPDATE deployment_hosts SET status = 'verified', "
                                "key_secret_ref = :r WHERE id = :i"),
                           {"i": host_row["id"], "r": key_ref})
        connection.execute(
            text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                 "target_kind, target_ref, host_id, status, required_resources, domains, "
                 "health_checks, created_by) VALUES (:id, :p, :app, 'acme/lead-triage', "
                 "'staging', 'ssh', 'deploy@203.0.113.40:22', :h, 'active', "
                 "CAST(:rr AS JSONB), CAST(:d AS JSONB), CAST(:hc AS JSONB), 'ceo')"),
            {"id": target_id, "p": HOME, "app": f"Lead-Triage Inbox {uuid4().hex[:4]}",
             "h": host_row["id"],
             "rr": json.dumps([{"kind": "app_port", "name": "3010"}]),
             "d": json.dumps(["203.0.113.40:3010"]),
             "hc": json.dumps([{"kind": "http", "path": "/", "expected_status": 200}])})
    return {"host": host, "host_row": host_row, "target_id": target_id, "key_ref": key_ref}


def _plan(client, hosted, ref="main"):
    response = client.post("/v1/delivery/deploy", json={
        "target_id": hosted["target_id"], "actor": "ceo", "ref": ref})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_action"] == "approve_decision", body
    return body


def _job_for(engine, run_id):
    with engine.begin() as connection:
        job = connection.execute(
            text("SELECT id FROM jobs WHERE kind = 'deployment.host_apply' "
                 "AND payload->>'deployment_run_id' = :d"), {"d": run_id}).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=HOME, job_id=job, worker_id="w1", lease_ttl_seconds=600)
    return {"job_id": job, "worker_id": "w1", "lease_token": lease["lease_token"]}


def _approve_and_apply(client, engine, plan):
    approved = client.post(f"/v1/decisions/{plan['decision_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text
    body = _job_for(engine, plan["deployment_run_id"])
    applied = client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/apply",
                          json=body)
    assert applied.status_code == 200, applied.text
    return applied.json(), body


def _governance(engine, plan):
    with engine.begin() as connection:
        changeset = connection.execute(
            text("SELECT status, reservation_id, approval_decision_id, risk_tier, operations, "
                 "rollback_plan FROM changesets WHERE id = :c"),
            {"c": plan["changeset_id"]}).mappings().one()
        reservation = connection.execute(
            text("SELECT amount_usd_micros, status FROM budget_reservations WHERE id = :r"),
            {"r": changeset["reservation_id"]}).mappings().one()
        receipts = connection.execute(
            text("SELECT status, provider, error FROM resource_receipts WHERE changeset_id = :c"),
            {"c": plan["changeset_id"]}).mappings().all()
        run = connection.execute(text("SELECT * FROM deployment_runs WHERE id = :i"),
                                 {"i": plan["deployment_run_id"]}).mappings().one()
    return dict(changeset), dict(reservation), [dict(r) for r in receipts], dict(run)


def test_plan_records_a_changeset_a_reservation_and_a_decision_and_touches_no_host(
        client, engine, hosted):
    plan = _plan(client, hosted)

    changeset, reservation, receipts, run = _governance(engine, plan)
    assert run["status"] == "awaiting_approval"
    assert changeset["status"] == "pending_approval" and changeset["risk_tier"] == 2
    assert changeset["approval_decision_id"] == plan["decision_id"]
    operation = changeset["operations"][0]
    assert operation["skill_id"] == "ssh_host.app.deploy"
    desired = operation["desired_state"]
    assert desired["host"] == hosted["host_row"]["name"] and desired["app_port"] == 3010
    assert desired["commit_ref"] == "main"
    # The host key is carried as a reference, never as material.
    assert {"name": "ssh_key", "ref": hosted["key_ref"], "from": "host"} in desired["secret_refs"]
    assert changeset["rollback_plan"], "a host deployment needs its rollback plan"
    assert reservation == {"amount_usd_micros": 0, "status": "active"}
    assert receipts == []
    review = plan["plan_review"]
    assert review["host"] == hosted["host_row"]["name"]
    assert review["cost"]["expected_cost_usd_micros"] == 0
    with engine.begin() as connection:
        decision = connection.execute(
            text("SELECT question, status, recommendation FROM decisions WHERE id = :d"),
            {"d": plan["decision_id"]}).mappings().one()
    assert decision["status"] == "pending"
    assert decision["question"] == (
        f"Deploy {run['app_name']} to {hosted['host_row']['name']} · staging")
    assert decision["recommendation"]["action"] == "deploy_host"
    # Approve / Reject on the card, and nothing ran.
    card = client.get(f"/v1/decisions/{plan['decision_id']}").json()
    assert {"approve", "reject"} <= {a["action"] for a in card.get("allowed_actions") or []}
    assert hosted["host"].commands == [] and hosted["host"].transfers == 0
    # Planning the same commit again is the same deployment, not a second one.
    again = _plan(client, hosted)
    assert again["deployment_run_id"] == plan["deployment_run_id"]


def test_without_approval_nothing_can_apply_it(client, engine, hosted):
    plan = _plan(client, hosted)

    # The generic ChangeSet routes refuse a host deployment and point at its decision.
    approved = client.post(f"/v1/changesets/{plan['changeset_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 422, approved.text
    assert plan["decision_id"] in approved.json()["detail"]
    # No job exists to lease, and a forged governance proof is refused before the host.
    with pytest.raises(PermissionError):
        governed_host_deploy.check_governed(engine, {
            "deployment_run_id": plan["deployment_run_id"],
            "changeset_id": plan["changeset_id"], "dedupe_key": "x", "apply_token": "forged"})
    with engine.begin() as connection:
        target = dict(connection.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                                         {"t": hosted["target_id"]}).mappings().one())
    blocked = deployment_connectors.SSHContainerConnector().apply(
        engine, api_main.settings, target, "", {}, ref="main",
        governance={"deployment_run_id": plan["deployment_run_id"],
                    "changeset_id": plan["changeset_id"], "dedupe_key": "x",
                    "apply_token": "forged"})
    assert blocked["status"] == "blocked"
    # The direct execute route keeps refusing, and says where the way in is.
    direct = client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/execute",
                         json={"actor": "ceo"})
    assert direct.status_code == 409 and "Approvals" in direct.json()["detail"]
    assert hosted["host"].commands == [] and hosted["host"].transfers == 0
    _, _, _, run = _governance(engine, plan)
    assert run["status"] == "awaiting_approval"


def test_an_agent_cannot_approve_a_host_deployment(client, engine, hosted):
    plan = _plan(client, hosted)
    with engine.begin() as connection:
        decision = dict(connection.execute(
            text("SELECT * FROM decisions WHERE id = :d"),
            {"d": plan["decision_id"]}).mappings().one())
        with pytest.raises(InvalidDecisionTransitionError, match="person approves"):
            governed_host_deploy.on_decision_approved(
                connection, api_main.settings, decision, "agent:deploy-lead")
    _, _, _, run = _governance(engine, plan)
    assert run["status"] == "awaiting_approval"


def test_approval_applies_through_the_connector_and_writes_the_receipt(
        client, engine, hosted):
    plan = _plan(client, hosted)
    result, body = _approve_and_apply(client, engine, plan)

    assert result["status"] == "succeeded", result
    assert hosted["host"].transfers == 1
    assert any("docker run" in c for c in hosted["host"].commands)
    changeset, reservation, receipts, run = _governance(engine, plan)
    assert changeset["status"] == "applied"
    assert receipts[0]["status"] == "succeeded" and receipts[0]["provider"] == "ssh_host"
    assert reservation["status"] == "consumed"
    assert run["status"] == "succeeded" and run["commit_ref"].startswith("c0ffee")
    assert run["release_receipt_ids"] == [result["resource_receipt_id"]]
    steps = [s["step"] for s in run["steps"]]
    assert steps == ["resolve", "preflight", "build", "transfer", "observe"]
    assert all(s["status"] == "ok" for s in run["steps"])
    fetched = client.get(f"/v1/delivery/deployments/{plan['deployment_run_id']}").json()
    assert fetched["status"] == "succeeded" and fetched["url"] == "203.0.113.40:3010"
    assert fetched["decision_id"] == plan["decision_id"]
    # The job replayed after the verdict does nothing to the host.
    count = len(hosted["host"].commands)
    again = client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/apply",
                        json=body)
    assert again.json()["status"] == "succeeded"
    assert len(hosted["host"].commands) == count

    # The deploy is over, so its surface lease is too: the next person may plan.
    with engine.begin() as connection:
        held = connection.execute(
            text("SELECT COUNT(*) FROM surface_ownerships WHERE surface_key = :s "
                 "AND status = 'active' AND (expires_at IS NULL OR expires_at > now())"),
            {"s": f"deployment_target:{hosted['target_id']}"}).scalar_one()
    assert held == 0, "a finished deploy still holds its target's surface"

    # A redeploy plans a NEW run whose rollback is the run that just succeeded.
    redeploy = client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/redeploy",
                           json={"actor": "ceo"})
    assert redeploy.status_code == 200, redeploy.text
    second = redeploy.json()
    assert second["next_action"] == "approve_decision"
    assert plan["deployment_run_id"] in second["plan_review"]["rollback"][0]


def test_a_failed_health_check_rolls_the_host_back_and_says_so(client, engine, hosted):
    hosted["host"].observed_healthy = False
    plan = _plan(client, hosted)
    result, _ = _approve_and_apply(client, engine, plan)

    assert result["status"] == "rolled_back", result
    restore = [c for c in hosted["host"].commands if "mv " in c and "dist.prev" in c]
    assert restore, "the previous release was not restored"
    changeset, reservation, receipts, run = _governance(engine, plan)
    assert changeset["status"] == "deadlettered"
    assert receipts[0]["status"] == "failed"
    assert receipts[0]["error"]["error_type"] == "HostDeployRolledBack"
    assert reservation["status"] == "released"
    assert run["status"] == "rolled_back" and "rolled the host back" in run["failure_reason"]
    assert [s["step"] for s in run["steps"]][-2:] == ["observe", "rollback"]
    assert run["steps"][-1]["status"] == "ok"


def test_a_failed_health_gate_on_the_host_rolls_back_too(client, engine, hosted):
    hosted["host"].healthy = False
    plan = _plan(client, hosted)
    result, _ = _approve_and_apply(client, engine, plan)

    assert result["status"] == "rolled_back", result
    _, _, receipts, run = _governance(engine, plan)
    assert receipts[0]["status"] == "failed"
    assert run["steps"][-1]["step"] == "rollback"


def test_rejecting_leaves_the_host_untouched_and_releases_the_reservation(
        client, engine, hosted):
    plan = _plan(client, hosted)
    rejected = client.post(f"/v1/decisions/{plan['decision_id']}/reject",
                           json={"actor": "ceo", "note": "not today"})
    assert rejected.status_code == 200, rejected.text

    changeset, reservation, receipts, run = _governance(engine, plan)
    assert run["status"] == "rejected"
    assert changeset["status"] == "rejected"
    assert reservation["status"] == "released"
    assert receipts == []
    with engine.begin() as connection:
        held = connection.execute(
            text("SELECT COUNT(*) FROM surface_ownerships WHERE surface_key = :s "
                 "AND status = 'active' AND (expires_at IS NULL OR expires_at > now())"),
            {"s": f"deployment_target:{hosted['target_id']}"}).scalar_one()
    assert held == 0, "a rejected deploy still holds its target's surface"
    with engine.begin() as connection:
        jobs = connection.execute(
            text("SELECT COUNT(*) FROM jobs WHERE kind = 'deployment.host_apply' "
                 "AND payload->>'deployment_run_id' = :d"),
            {"d": plan["deployment_run_id"]}).scalar_one()
    assert jobs == 0
    assert hosted["host"].commands == [] and hosted["host"].transfers == 0


def test_ungoverned_direct_calls_stay_refused(client, engine, hosted):
    with engine.begin() as connection:
        target = dict(connection.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                                         {"t": hosted["target_id"]}).mappings().one())
    blocked = deployment_connectors.SSHContainerConnector().apply(
        engine, api_main.settings, target, "", {}, ref="main")
    assert blocked["status"] == "blocked"
    assert "approved Resource Plane ChangeSet" in blocked["problems"][0]
    assert "Approvals" in blocked["problems"][0]
    with pytest.raises(PermissionError, match="Approvals"):
        deploy_service.deploy_ssh(engine, api_main.settings, "", host="203.0.113.40")
    back = deployment_connectors.SSHContainerConnector().rollback(
        engine, api_main.settings, target, {"remote": "/opt/boss-apps/x"})
    assert back["status"] == "failed"
    # A planned run is never executed by the legacy pipeline either.
    plan = _plan(client, hosted)
    replay = delivery_pipeline.execute_deployment(
        engine, api_main.settings, plan["deployment_run_id"], actor="ceo")
    assert replay["status"] == "blocked"
    assert replay["reason_code"] == "governed_host_deployment_applies_on_approval"
    assert hosted["host"].commands == [] and hosted["host"].transfers == 0


def test_a_target_off_a_verified_host_is_refused_with_the_next_step(client, engine, hosted):
    with engine.begin() as connection:
        connection.execute(text("UPDATE deployment_hosts SET status = 'registered' "
                                "WHERE id = :i"), {"i": hosted["host_row"]["id"]})
    response = client.post("/v1/delivery/deploy", json={
        "target_id": hosted["target_id"], "actor": "ceo", "ref": "main"})
    assert response.status_code == 409, response.text
    assert "not verified" in response.json()["detail"]
    assert "Verify" in response.json()["detail"]


def test_no_secret_reaches_a_response_a_record_or_a_log(client, engine, hosted, caplog):
    caplog.set_level(logging.DEBUG)
    plan = _plan(client, hosted)
    hosted["host"].observed_healthy = False  # exercise the rollback's logs as well
    result, _ = _approve_and_apply(client, engine, plan)
    fetched = client.get(f"/v1/delivery/deployments/{plan['deployment_run_id']}")
    listed = client.get("/v1/delivery/deployments", params={"project_id": HOME})
    card = client.get(f"/v1/decisions/{plan['decision_id']}")

    needle = "SECRETKEYMATERIAL"
    for blob in (json.dumps(plan), json.dumps(result), fetched.text, listed.text, card.text,
                 caplog.text):
        assert needle not in blob
    with engine.begin() as connection:
        stored = [
            *connection.execute(text("SELECT row_to_json(d)::text FROM deployment_runs d "
                                     "WHERE project_id = :p"), {"p": HOME}).scalars(),
            *connection.execute(text("SELECT data::text FROM events WHERE project_id = :p"),
                                {"p": HOME}).scalars(),
            *connection.execute(text("SELECT row_to_json(c)::text FROM changesets c "
                                     "WHERE project_id = :p"), {"p": HOME}).scalars(),
            *connection.execute(text("SELECT row_to_json(r)::text FROM resource_receipts r "
                                     "WHERE project_id = :p"), {"p": HOME}).scalars(),
            *connection.execute(text("SELECT row_to_json(r)::text FROM receipts r "
                                     "WHERE project_id = :p"), {"p": HOME}).scalars(),
            *connection.execute(text("SELECT row_to_json(d)::text FROM decisions d "
                                     "WHERE project_id = :p"), {"p": HOME}).scalars(),
        ]
    assert stored and not [s for s in stored if needle in s]
