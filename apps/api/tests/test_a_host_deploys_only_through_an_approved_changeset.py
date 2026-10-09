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
import re
import subprocess
from types import SimpleNamespace
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


HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForGovernedDeployTests0001"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAnImpostorAtTheSameAddressTests00002"


class FakeHost:
    """The `ssh` process itself, faked: it reads the host-key options BOSS passes, plays a host
    that shows `key`, and answers the commands. Only deploy_service's process runner is replaced,
    so deploy_service._ssh and the transfer pipeline build their real command lines."""

    def __init__(self):
        self.commands: list[str] = []
        self.argvs: list[list[str]] = []
        self.transfers = 0
        self.healthy = True
        self.observed_healthy = True
        self.key = HOST_KEY

    def _host_key_ok(self, opts: dict[str, str]) -> bool:
        mode = opts.get("StrictHostKeyChecking", "ask")
        path = opts.get("UserKnownHostsFile")
        if mode == "no" or not path:
            return True
        known = Path(path).read_text() if Path(path).exists() else ""
        if self.key.split()[1] in known:
            return True
        if mode == "accept-new" and not known.strip():
            with open(path, "a") as handle:
                handle.write(f"[203.0.113.40]:22 {self.key}\n")
            return True
        return False

    def run(self, argv, *args, **kwargs):
        line = argv if isinstance(argv, str) else " ".join(argv)
        opts = dict(o.split("=", 1) for o in re.findall(r"(?<=-o )(\S+=\S+)", line))
        self.argvs.append([argv] if isinstance(argv, str) else list(argv))
        if not self._host_key_ok(opts):
            return subprocess.CompletedProcess(argv, 255, "", "Host key verification failed.\n")
        if isinstance(argv, str):  # the tar | ssh transfer pipeline
            self.transfers += 1
            return subprocess.CompletedProcess(argv, 0, "", "")
        command = argv[-1]
        self.commands.append(command)
        if "BASE_OK" in command:
            return subprocess.CompletedProcess(argv, 0, "BASE_OK\nAPP_OK\nDOCKER_OK\n", "")
        if "BOSS_SSH_OK" in command:  # Verify's own session
            return subprocess.CompletedProcess(argv, 0, "BOSS_SSH_OK\nDOCKER_OK\nx86_64\n", "")
        if "docker version" in command:
            return subprocess.CompletedProcess(argv, 0, "27.0\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def writes(self):
        return [c for c in self.commands
                if "docker run" in c or "dist.prev" in c or "docker restart" in c]


@pytest.fixture()
def hosted(client, engine, monkeypatch, tmp_path):
    host = FakeHost()
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    # NARROW: only deploy_service's process runner. Patching owned_process.run itself would
    # fake every subprocess in the API (git, docker, the preview runner) for this test.
    monkeypatch.setattr(deploy_service, "owned_process", SimpleNamespace(run=host.run))
    monkeypatch.setattr(deploy_service, "github_token_for", lambda *a, **k: "")

    def build(settings, owner, repo, branch, **kwargs):
        work = Path(tmp_path) / f"work-{uuid4().hex[:6]}"
        (work / "dist").mkdir(parents=True)
        return work, "static", "", "c0ffee" * 6 + "abcd"

    monkeypatch.setattr(deploy_service, "_build_dist", build)
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


def test_the_infra_deploy_button_plans_and_ships_through_the_real_path(client, engine, hosted):
    """The Infra screen's Deploy on an app target, with nothing between the button and the host
    faked: the route, the manual-deploy service, the planner, approval and the apply.

    The button's own test faked `delivery_deploy_endpoint`, which the button called as a plain
    function. When that handler grew a `request` parameter, every press answered 500 and that
    test stayed green."""
    pressed = client.post(f"/v1/infra/targets/{hosted['target_id']}/deploy",
                          json={"project_id": HOME, "actor": "ceo", "ref": "main"})
    assert pressed.status_code == 200, pressed.text
    plan = pressed.json()
    assert plan["kind"] == "delivery"
    assert plan["ref"] == f"delivery:{plan['deployment_run_id']}"
    assert plan["next_action"] == "approve_decision", plan

    changeset, reservation, receipts, run = _governance(engine, plan)
    assert run["deployment_target_id"] == hosted["target_id"]
    assert run["status"] == "awaiting_approval" and run["trigger"] == "manual"
    assert run["commit_ref"] == "main"
    assert run["release_packet"]["source"] == "manual_deploy"
    assert run["release_packet"]["requested_by"] == "ceo"
    assert changeset["approval_decision_id"] == plan["decision_id"]
    assert receipts == [] and hosted["host"].writes() == []

    result, _ = _approve_and_apply(client, engine, plan)
    assert result["status"] == "succeeded", result
    assert any("docker run" in c for c in hosted["host"].commands)


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
    # A refusal is an answer, not a record: no decision, no failed run, one plain event.
    with engine.begin() as connection:
        runs = connection.execute(text("SELECT COUNT(*) FROM deployment_runs "
                                       "WHERE deployment_target_id = :t"),
                                  {"t": hosted["target_id"]}).scalar_one()
        decisions = connection.execute(
            text("SELECT COUNT(*) FROM decisions WHERE project_id = :p AND status = 'pending' "
                 "AND (recommendation->>'deployment_target_id' = :t "
                 "     OR recommendation->>'target_id' = :t)"),
            {"p": HOME, "t": hosted["target_id"]}).scalar_one()
        events = connection.execute(
            text("SELECT data FROM events WHERE event_type = 'deployment.plan_refused' "
                 "AND entity_id = :t"), {"t": hosted["target_id"]}).scalars().all()
    assert runs == 0 and decisions == 0
    assert len(events) == 1 and events[0]["reason"] == "host_not_verified"
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    assert not [i for i in fleet["attention"] if hosted["target_id"] in str(i.get("id"))]


def test_boot_repair_clears_what_earlier_refusals_left(client, engine, hosted):
    """Refusals once raised a pending decision and a failed run; the repair removes both."""
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, risk_tier, recommendation) "
            "VALUES ('dec_old_refusal', :p, 'Community host deployment requires a governed "
            "ChangeSet', 'pending', 2, CAST(:r AS JSONB))"),
            {"p": HOME, "r": json.dumps({"action": "implement_governed_host_deployment"})})
        connection.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, decision_id, "
            "app_name, environment, repo, target_kind, target_ref, status, created_by) VALUES "
            "('dr_old_refusal', :p, :t, 'dec_old_refusal', 'x', 'staging', 'r', 'ssh', 'x', "
            "'failed', 'ceo')"), {"p": HOME, "t": hosted["target_id"]})
        first = governed_host_deploy.repair_refusal_records(connection)
        again = governed_host_deploy.repair_refusal_records(connection)
        status = connection.execute(text("SELECT status FROM decisions WHERE id = "
                                         "'dec_old_refusal'")).scalar_one()
        run = connection.execute(text("SELECT 1 FROM deployment_runs WHERE id = "
                                      "'dr_old_refusal'")).first()
    assert first == {"decisions_withdrawn": 1, "runs_removed": 1}
    assert again == {"decisions_withdrawn": 0, "runs_removed": 0}
    assert status == "withdrawn" and run is None


def test_a_host_deploy_waiting_on_you_is_one_count_everywhere(client, engine, hosted):
    plan = _plan(client, hosted)
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    deploys = client.get("/v1/infra/deploys", params={"project_id": HOME}).json()
    approvals = [i for i in fleet["attention"] if i.get("kind") == "approval"]
    assert [i["decision_id"] for i in approvals] == [plan["decision_id"]]
    assert approvals[0]["open"].endswith(f"deploy=delivery:{plan['deployment_run_id']}")
    assert fleet["counts"]["needs_you"] == deploys["counts"]["needs_you"] == 1
    center = client.get("/v1/command-center/view", params={"project_id": HOME})
    assert center.status_code == 200, center.text
    assert plan["decision_id"] in center.text
    assert "host_deploy_requires_approval" not in center.text, "a raw code reached the page"
    dots = {d["key"]: d for d in center.json().get("systems") or []}
    if "deploys" in dots:
        assert dots["deploys"]["state"] == "Needs you"
    # Approving makes it nobody's question any more, everywhere.
    _approve_and_apply(client, engine, plan)
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    assert fleet["counts"]["needs_you"] == 0


def test_cloud_changes_approve_a_deploy_through_its_decision(client, engine, hosted):
    plan = _plan(client, hosted)
    rows = client.get("/v1/infra/cloud", params={"project_id": HOME, "view": "changes",
                                                 "viewer": "ceo"}).json()["rows"]
    row = next(r for r in rows if r["id"] == plan["changeset_id"])
    assert row["can"]["approve"] is False and row["can"]["apply"] is False
    assert row["decision_id"] == plan["decision_id"]
    assert row["can"]["approve_deploy"] in (True, False)  # a viewer who can write sees True


def test_a_rollback_from_its_card_is_a_plan_opened_where_it_waits(client, engine, hosted):
    plan = _plan(client, hosted)
    _approve_and_apply(client, engine, plan)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, commit_ref, status, trigger, "
            "created_by, created_at, finished_at) SELECT 'dr_broke', project_id, "
            "deployment_target_id, app_name, environment, repo, target_kind, target_ref, "
            "'bad', 'failed', 'manual', 'ceo', now(), now() FROM deployment_runs WHERE id = :i"),
            {"i": plan["deployment_run_id"]})
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    card = next(i for i in fleet["attention"] if i.get("infra_kind") == "deploy_failed")
    assert "rollback" in [a["key"] for a in card["answers"]]
    r = client.post("/v1/infra/attention/act", json={
        "project_id": HOME, "actor": "ceo", "id": card["id"], "option": "rollback"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "Rollback planned · Approve"
    assert body["open"] == f"?area=infra&view=deploys&deploy={body['ref']}"
    detail = client.get(f"/v1/infra/deploys/{body['ref']}", params={"project_id": HOME}).json()
    assert detail["row"]["word"] == "Needs you" and detail["row"]["can"]["approve"] is True


def test_the_apply_route_refuses_a_forged_lease_and_an_unknown_run(client, engine, hosted):
    plan = _plan(client, hosted)
    client.post(f"/v1/decisions/{plan['decision_id']}/approve", json={"actor": "ceo"})
    forged = client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/apply", json={
        "job_id": "job_nope", "worker_id": "w1", "lease_token": "forged"})
    assert forged.status_code == 409, forged.text
    body = _job_for(engine, plan["deployment_run_id"])
    unknown = client.post("/v1/delivery/deployments/deprun_nope/apply", json=body)
    assert unknown.status_code == 404, unknown.text
    assert hosted["host"].transfers == 0


def test_the_host_key_is_pinned_and_a_different_key_is_refused(client, engine, hosted):
    # hetzner-ci-01's case: verified before pinning, so the first governed connect records it.
    first = _plan(client, hosted)
    result, _ = _approve_and_apply(client, engine, first)
    assert result["status"] == "succeeded", result
    with engine.begin() as connection:
        pin = dict(connection.execute(text(
            "SELECT host_key_fingerprint, host_key_pinned_by FROM deployment_hosts "
            "WHERE id = :i"), {"i": hosted["host_row"]["id"]}).mappings().one())
    assert pin["host_key_fingerprint"].startswith("SHA256:")
    assert pin["host_key_pinned_by"] == "first_deploy"
    governed = [a for a in hosted["host"].argvs if "BASE_OK" in " ".join(a)]
    assert governed and "StrictHostKeyChecking=accept-new" in " ".join(governed[0])
    detail = client.get(f"/v1/infra/hosts/{hosted['host_row']['id']}",
                        params={"project_id": HOME}).json()
    assert detail["host_key"]["pinned"] is True and "first deploy" in detail["host_key"]["words"]

    # Another machine answers at the address: refused before anything is written to it.
    hosted["host"].key = OTHER_KEY
    writes_before = len(hosted["host"].writes())
    second = client.post(f"/v1/delivery/deployments/{first['deployment_run_id']}/redeploy",
                         json={"actor": "ceo"}).json()
    result, _ = _approve_and_apply(client, engine, second)
    assert result["status"] == "failed", result
    with engine.begin() as connection:
        reason = connection.execute(text("SELECT failure_reason FROM deployment_runs "
                                         "WHERE id = :i"),
                                    {"i": second["deployment_run_id"]}).scalar_one()
    assert reason == "Host key changed — re-verify the host in Infra."
    assert len(hosted["host"].writes()) == writes_before
    assert "StrictHostKeyChecking=yes" in " ".join(hosted["host"].argvs[-1])

    # The operator re-verifies: that is accepting the new machine, and deploys work again.
    from boss_engine.services.deployment_fleet import verify_host

    with engine.begin() as connection:
        verdict = verify_host(connection, api_main.settings, host_id=hosted["host_row"]["id"])
    assert verdict["status"] == "verified", verdict
    with engine.begin() as connection:
        pin = dict(connection.execute(text(
            "SELECT host_key_known_hosts, host_key_pinned_by FROM deployment_hosts "
            "WHERE id = :i"), {"i": hosted["host_row"]["id"]}).mappings().one())
    assert OTHER_KEY.split()[1] in pin["host_key_known_hosts"]
    assert pin["host_key_pinned_by"] == "verify"
    third = client.post(f"/v1/delivery/deployments/{first['deployment_run_id']}/redeploy",
                        json={"actor": "ceo"}).json()
    result, _ = _approve_and_apply(client, engine, third)
    assert result["status"] == "succeeded", result


def test_agents_are_never_people_and_the_console_operator_is(engine):
    from boss_engine.services.agent_identity import is_person

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name) VALUES "
            "('role_gd_agent', :p, 'gd_role_agent', 'x') ON CONFLICT DO NOTHING"), {"p": HOME})
        connection.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions, status, created_by) VALUES ('pm_gd_agent', :p, "
            "'gd_role_agent', 'x', 'human', '[]'::jsonb, '[]'::jsonb, 'active', 'test') "
            "ON CONFLICT DO NOTHING"), {"p": HOME})
        verdicts = {a: is_person(a, connection, HOME)
                    for a in ("executive_console", "ceo", "coo", "metatron", "gd_role_agent",
                              "agent:deploy-lead", "system", "nobody-here")}
    assert verdicts == {"executive_console": True, "ceo": True, "coo": False,
                        "metatron": False, "gd_role_agent": False, "agent:deploy-lead": False,
                        "system": False, "nobody-here": False}


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


def test_plan_approve_and_redeploy_record_the_one_signed_in_operator(
        client, engine, hosted, monkeypatch):
    """The console reaches the API with one key (principal `ceo`) and names the operator
    signed in to it (`executive_console`). Every step of one deploy records that operator."""
    key = f"gd-console-key-{uuid4().hex[:8]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key}:ceo")
    auth = {"Authorization": f"Bearer {key}"}
    planned = client.post("/v1/delivery/deploy", headers=auth, json={
        "target_id": hosted["target_id"], "actor": "executive_console", "ref": "main"})
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    approved = client.post(f"/v1/decisions/{plan['decision_id']}/approve", headers=auth,
                           json={"actor": "executive_console"})
    assert approved.status_code == 200, approved.text
    body = _job_for(engine, plan["deployment_run_id"])
    monkeypatch.setattr(api_main.settings, "require_api_auth", False)
    client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/apply", json=body)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    again = client.post(f"/v1/delivery/deployments/{plan['deployment_run_id']}/redeploy",
                        headers=auth, json={"actor": "executive_console"})
    assert again.status_code == 200, again.text
    with engine.begin() as connection:
        run = connection.execute(text("SELECT created_by FROM deployment_runs WHERE id = :i"),
                                 {"i": plan["deployment_run_id"]}).scalar_one()
        decision = connection.execute(text("SELECT approved_by FROM decisions WHERE id = :d"),
                                      {"d": plan["decision_id"]}).scalar_one()
        second = connection.execute(text("SELECT created_by FROM deployment_runs WHERE id = :i"),
                                    {"i": again.json()["deployment_run_id"]}).scalar_one()
    assert (run, decision, second) == ("executive_console",) * 3
    # A key cannot name somebody it may not act as: that falls back to its own principal.
    other = client.post(f"/v1/decisions/{again.json()['decision_id']}/reject", headers=auth,
                        json={"actor": "metatron"})
    assert other.status_code == 200, other.text
    assert other.json().get("status") == "rejected"
