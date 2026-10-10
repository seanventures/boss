"""Real Postgres admission transitions with an inert provider-contact probe.

Repository/PR records are owned fixtures. No GitHub or model API is contacted.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import consent, lineage
from boss_engine.services import code_task_service as tasks
from boss_engine.services import software_factory
from boss_engine.services.jobs import JobLeaseService


@pytest.fixture()
def parent(engine, monkeypatch):
    world = IdentityWorld.create(engine)
    owner = world.person("Owned retry operator")
    world.bind(owner, "owner", project=world.project_a)
    run_id, reservation = "crun_" + uuid4().hex, "bres_" + uuid4().hex
    builder = world.agent("owned_builder", ceiling=tuple(consent.CODE_TASK_PERMISSIONS), sponsor=owner)
    spec = {"role_key": "owned_builder", "instructions": "Export the selected records.", "acceptance_criteria": ["Include column names"],
            "test_commands": ["pytest owned"], "owner": "owned-owner", "repo": "owned-repository",
            "base_branch": "main", "profile": "balanced"}
    with engine.begin() as c:
        # Owned authority fixture, matching an Owner-created standing grant. The real
        # consent resolver still validates the builder, project, repo and permissions.
        c.execute(text("INSERT INTO roles(id,project_id,role_key,display_name,agent_id) "
                       "VALUES(:id,:p,'owned_builder','Owned builder',:a)"),
                  {"id": uuid4().hex, "p": world.project_a, "a": builder.id})
        c.execute(text("INSERT INTO identity_delegation_grants(id,org_id,user_principal_id,agent_ids,"
                       "project_ids,status,resources,expires_at) VALUES(:id,:o,:u,:agents,:projects,"
                       "'active',CAST(:resources AS JSONB),now()+interval '1 day')"),
                  {"id": "dlg_" + run_id, "o": world.org, "u": owner.id, "agents": [builder.id],
                   "projects": [world.project_a], "resources": json.dumps({"github": {
                       "repos": ["owned-owner/owned-repository"], "contents": "write",
                       "pull_requests": "write", "branch_pattern": "boss/*"}})})
        for permission in consent.CODE_TASK_PERMISSIONS:
            c.execute(text("INSERT INTO identity_delegation_grant_permissions(grant_id,permission_key) "
                           "VALUES(:g,:k)"), {"g": "dlg_" + run_id, "k": permission})
        c.execute(text("INSERT INTO budget_reservations(id,project_id,category,amount_usd_micros,expires_at) "
                       "VALUES(:id,:p,'owned-fixture',80000,now()+interval '1 hour')"),
                  {"id": reservation, "p": world.project_a})
        c.execute(text("INSERT INTO code_task_runs(id,project_id,title,status,phase,spec,finalize_ctx,"
                       "pull_request_url,reservation_id) VALUES(:id,:p,'Owned CSV fixture','completed','done',"
                       "CAST(:spec AS JSONB),CAST(:ctx AS JSONB),:url,:b)"),
                  {"id": run_id, "p": world.project_a, "spec": json.dumps(spec), "b": reservation,
                   "ctx": json.dumps({"root_run_id": run_id, "attempt": 1, "app_id": "owned-app"}),
                   "url": "https://example.test/owned-owner/owned-repository/pull/7"})
    contacts = []

    class ProviderContactProbe:
        def __init__(self, *_args, **_kwargs):
            contacts.append("factory constructed")
            self.github = self

        def update_pull_request_branch(self, number):
            contacts.append(("branch write", number))
            return True

    monkeypatch.setattr(software_factory, "SoftwareFactoryService", ProviderContactProbe)
    monkeypatch.setattr(tasks, "SoftwareFactoryService", ProviderContactProbe)
    return world, owner, run_id, reservation, contacts


def children(engine, project):
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text("SELECT id,status,spec,finalize_ctx,reservation_id "
                                               "FROM code_task_runs WHERE project_id=:p AND finalize_ctx ? 'parent_run_id'"),
                                           {"p": project}).mappings()]


def test_retry_queues_linked_child_without_admission_provider_contact(engine, parent):
    world, owner, run_id, reservation, contacts = parent
    settings = get_settings()
    with lineage.acting(world.ctx(owner)):
        result = tasks.retry_code_task(engine, settings, JobLeaseService(settings), run_id,
                                       fix_note="Only add the CSV header.")
    assert contacts == [], "admission must not construct or call a repository provider"
    child, = children(engine, world.project_a)
    assert child["id"] == result["run_id"] and child["id"] != run_id
    assert child["finalize_ctx"]["parent_run_id"] == run_id
    assert child["finalize_ctx"]["root_run_id"] == run_id
    assert child["finalize_ctx"]["attempt"] == 2
    assert child["finalize_ctx"]["app_id"] == "owned-app"
    assert child["spec"]["owner"] == "owned-owner" and child["spec"]["repo"] == "owned-repository"
    assert "Only add the CSV header." in child["spec"]["instructions"]
    assert child["reservation_id"] is None  # parent authority is never reused for new work
    with engine.begin() as c:
        job = c.execute(text("SELECT status,payload,on_behalf_of,grant_id FROM jobs WHERE project_id=:p"),
                        {"p": world.project_a}).mappings().one()
        assert job["status"] == "queued" and job["payload"]["run_id"] == child["id"]
        assert job["on_behalf_of"] == owner.id
        assert job["grant_id"] == "dlg_" + run_id
        assert c.execute(text("SELECT status FROM budget_reservations WHERE id=:id"),
                         {"id": reservation}).scalar_one() == "active"
        assert c.execute(text("SELECT count(*) FROM budget_reservations WHERE project_id=:p"),
                         {"p": world.project_a}).scalar_one() == 1
        event = c.execute(text("SELECT data FROM events WHERE project_id=:p "
                               "AND event_type='code_task.branch_refresh_not_requested'"),
                          {"p": world.project_a}).scalar_one()
        assert event["parent_run_id"] == run_id and event["child_run_id"] == child["id"]
        assert event["branch"] == "boss/code/" + run_id
        assert c.execute(text("SELECT count(*) FROM receipts WHERE project_id=:p"),
                         {"p": world.project_a}).scalar_one() == 0


def test_failed_job_admission_leaves_no_child_or_provider_write(engine, parent, monkeypatch):
    world, owner, run_id, _, contacts = parent
    settings = get_settings()
    jobs = JobLeaseService(settings)

    def fail_job(*_args, **_kwargs):
        raise RuntimeError("owned job admission failure")

    monkeypatch.setattr(jobs, "create_job", fail_job)
    with lineage.acting(world.ctx(owner)), pytest.raises(RuntimeError, match="owned job admission"):
        tasks.retry_code_task(engine, settings, jobs, run_id, fix_note="Only add the CSV header.")
    assert contacts == []
    assert children(engine, world.project_a) == []
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id=:p"),
                         {"p": world.project_a}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id=:p AND event_type "
                              "IN ('code_task.branch_refreshed','code_task.branch_refresh_not_requested')"),
                         {"p": world.project_a}).scalar_one() == 0


def test_feedback_proposal_then_explicit_confirmation_queues_once_without_provider_contact(client, engine, parent):
    world, owner, run_id, _, contacts = parent
    with world.acting_as(world.ctx(owner)):
        proposed = client.post(f"/v1/code-tasks/{run_id}/feedback", json={
            "message": "Only add a header row to the CSV export.", "expected": "The CSV begins with column names."})
        assert proposed.status_code == 200, proposed.text
        assert proposed.json()["dispatched"] is False
        assert children(engine, world.project_a) == [] and contacts == []
        path = f"/v1/feedback-threads/{proposed.json()['thread_id']}/confirm"
        confirmed = client.post(path, json={"actor": owner.id})
        assert confirmed.status_code == 200, confirmed.text
        duplicate = client.post(path, json={"actor": owner.id})
        assert duplicate.status_code == 409, duplicate.text
    assert contacts == []
    child, = children(engine, world.project_a)
    assert child["id"] == confirmed.json()["run_id"]
    assert child["finalize_ctx"]["parent_run_id"] == run_id
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id=:p"),
                         {"p": world.project_a}).scalar_one() == 1


def test_revoked_consent_refuses_retry_without_provider_contact(client, engine, parent):
    world, owner, run_id, _, contacts = parent
    with engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status='revoked' WHERE id=:id"),
                  {"id": "dlg_" + run_id})
    with world.acting_as(world.ctx(owner)):
        proposed = client.post(f"/v1/code-tasks/{run_id}/feedback", json={
            "message": "Only add a header row to the CSV export.", "expected": "The CSV begins with column names."})
        assert proposed.status_code == 200, proposed.text
        path = f"/v1/feedback-threads/{proposed.json()['thread_id']}/confirm"
        refused = client.post(path, json={"actor": owner.id})
        assert refused.status_code == 409 and refused.json()["error"] == "consent_required", refused.text
    assert contacts == [] and children(engine, world.project_a) == []
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id=:p"),
                         {"p": world.project_a}).scalar_one() == 0
        assert c.execute(text("SELECT status FROM feedback_threads WHERE id=:id"),
                         {"id": proposed.json()["thread_id"]}).scalar_one() == "awaiting_confirmation"
