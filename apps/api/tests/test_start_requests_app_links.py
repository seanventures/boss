"""A NEW app's "Talks to" (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): links asked for at its start.

The app does not exist until its first build, so `POST /v1/delivery-workflows/start` takes
`input.app_links`, checks them THEN (app_link:manage on top of workflow_run:start; each provider
an app of this project, D7; the aliases), records them as `app_link_requests` (never in the run's
input), and the run's first build makes them (`app_links.service.create_requested_links`, called
by `run_env.for_run` before the build's link environment is computed). Each request is resolved
once: a link revoked later is not re-made by the next build.
"""
from __future__ import annotations

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.app_links import run_env
from boss_engine.app_links import service as links
from boss_engine.identity import vault
from boss_engine.services import workflows


@pytest.fixture(scope="module", autouse=True)
def master_key():
    import os

    saved = os.environ.get("BOSS_MASTER_KEY")
    os.environ["BOSS_MASTER_KEY"] = base64.b64encode(b"q-talks-to-test-only-master-key!").decode()
    vault.reset_master_key_cache()
    yield
    if saved is None:
        os.environ.pop("BOSS_MASTER_KEY", None)
    else:
        os.environ["BOSS_MASTER_KEY"] = saved
    vault.reset_master_key_cache()


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.bank = f"ctr_{uuid.uuid4().hex[:12]}"
    w.elsewhere = f"ctr_{uuid.uuid4().hex[:12]}"
    w.operator = w.person("talks-operator")
    w.bind(w.operator, "operator", project=w.project_a)
    w.starter = w.person("talks-starter")       # may start work, may not link apps
    w.bind(w.starter, w.custom_role("starter", ["workflow_run:start", "workflow:read", "project:read",
                                                 "workflow_run:read"]), project=w.project_a)
    return w


@pytest.fixture(autouse=True)
def apps_exist(world, engine):
    """Per test: the suite's clean_tables empties runs and workflows between tests."""
    with engine.begin() as c:
        if not c.execute(text("SELECT 1 FROM workflow_definitions WHERE project_id = :p "
                              "AND workflow_key = 'talks_notify'"), {"p": world.project_a}).first():
            definition = workflows.create_definition(
                c, project_id=world.project_a, workflow_key="talks_notify", name="Notify", description=None,
                actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
            workflows.publish_version(
                c, project_id=world.project_a, definition_id=definition["id"], actor="ceo",
                spec={"entry_step": "say", "steps": [
                    {"id": "say", "kind": "capability",
                     "binding": {"capability": "notify", "inputs": {"message": "on"}}, "next": []}]})
    with engine.begin() as db:
        for rid, project, title in ((world.bank, world.project_a, "Banking"),
                                    (world.elsewhere, world.project_b, "Other banking")):
            db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) "
                            "VALUES (:i, :p, :t, 'completed') ON CONFLICT (id) DO NOTHING"),
                       {"i": rid, "p": project, "t": title})


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def no_agent_delegation(monkeypatch):
    """This world seeds no agents, so the start's delegation step (consent._agents) would refuse
    with 409 agent_without_passport under enforce. Not what these tests are about: the links are
    checked BEFORE start_run. "No agent applies" is what that function answers for such a project."""
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "_agents", lambda *a, **k: None)


def _start(client, world, who, app_links):
    with world.acting_as(world.ctx(who)):
        return client.post("/v1/delivery-workflows/start", json={
            "project_id": world.project_a, "workflow_key": "talks_notify",
            "input": {"directive": "A tester for the banking API", "title": "Bank tester",
                      "app_links": app_links}})


def _runs(engine, project):
    with engine.begin() as db:
        return db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"), {"p": project}).scalar()


BANK = {"provider_app_key": "Banking", "alias": "BANK", "env": "preview", "credentials": "test_users"}


def _first_build(engine, world, wf_run_id: str, title: str = "Bank tester") -> str:
    run_id = f"ctr_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, finalize_ctx) "
                        "VALUES (:i, :p, :t, 'running', CAST(:f AS JSONB))"),
                   {"i": run_id, "p": world.project_a, "t": title,
                    "f": json.dumps({"workflow_run_id": wf_run_id})})
    return run_id


def test_a_start_records_the_links_and_the_first_build_makes_them(client, world, engine):
    r = _start(client, world, world.operator, [BANK])
    assert r.status_code == 200, r.text
    body = r.json()
    wf = body["run"]["id"]
    assert body["app_links"] == [{"provider_app_key": "banking", "alias": "BANK", "env": "preview",
                                  "credentials": "test_users"}]
    with engine.begin() as db:
        stored = db.execute(text("SELECT input FROM workflow_runs WHERE id = :w"), {"w": wf}).scalar()
        asked = links.requests_of(db, wf)
    assert "app_links" not in stored                      # never kept in the run's input
    assert [(a["alias"], a["outcome"]) for a in asked] == [("BANK", None)]

    build = _first_build(engine, world, wf)
    with run_env.for_run(engine, build, world.project_a) as env:
        assert {"BOSS_LINK_BANK_URL", "BOSS_LINK_BANK_TOKEN", "BOSS_LINK_BANK_USERS"} <= set(env)
    with engine.begin() as db:
        made = db.execute(text("SELECT * FROM app_links WHERE project_id = :p AND alias = 'BANK' "
                               "AND status = 'active'"), {"p": world.project_a}).mappings().all()
        asked = links.requests_of(db, wf)
        audited = db.execute(text("SELECT actor_principal_id, outcome, details FROM identity_security_events "
                                  "WHERE event_type = 'app_link.created' AND target_id = :t"),
                             {"t": made[0]["id"]}).mappings().all()
    assert len(made) == 1 and made[0]["consumer_app_key"] == "bank tester"
    assert made[0]["provider_app_key"] == "banking" and made[0]["created_by"] == world.operator.id
    assert [(a["outcome"], a["link_id"]) for a in asked] == [("created", made[0]["id"])]
    assert len(audited) == 1 and audited[0]["actor_principal_id"] == world.operator.id
    assert audited[0]["details"]["requested_at_start"] == wf

    # Resolved once: revoked, the next build of the run does not make it again.
    with engine.begin() as db:
        links.revoke_link(db, project_id=world.project_a, link_id=made[0]["id"], revoked_by="test")
        assert links.create_requested_links(db, project_id=world.project_a, run_id=build) == []
        again = _first_build(engine, world, wf)
        assert links.create_requested_links(db, project_id=world.project_a, run_id=again) == []
        assert db.execute(text("SELECT count(*) FROM app_links WHERE project_id = :p AND alias = 'BANK' "
                               "AND status = 'active'"), {"p": world.project_a}).scalar() == 0


def test_without_app_link_manage_the_start_is_refused_and_nothing_runs(client, world, engine):
    before = _runs(engine, world.project_a)
    r = _start(client, world, world.starter, [BANK])
    assert r.status_code == 403, r.text
    assert _runs(engine, world.project_a) == before
    # The same person may start the same work without links.
    with world.acting_as(world.ctx(world.starter)):
        ok = client.post("/v1/delivery-workflows/start", json={
            "project_id": world.project_a, "workflow_key": "talks_notify",
            "input": {"directive": "A tester for the banking API", "title": "Bank tester"}})
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("asked, status, error", [
    ([{**BANK, "provider_app_key": "Other banking"}], 422, "provider_not_in_project"),   # D7
    ([{**BANK, "provider_app_key": "no such app"}], 422, "provider_not_in_project"),
    ([{**BANK, "alias": "bank-1"}], 422, "bad_alias"),
    ([{**BANK, "env": "prod"}], 422, "bad_env"),
    ([{**BANK, "credentials": "client"}], 422, "client_credentials_not_available"),
    ([BANK, BANK], 409, "alias_taken"),
    ("BANK", 422, "bad_app_links"),
    ([BANK] * 9, 422, "too_many_links"),
])
def test_bad_links_are_refused_before_anything_runs(client, world, engine, asked, status, error):
    before = _runs(engine, world.project_a)
    r = _start(client, world, world.operator, asked)
    assert r.status_code == status, r.text
    assert error in r.text
    assert _runs(engine, world.project_a) == before


def test_a_link_that_cannot_be_made_at_the_build_is_recorded_and_the_build_runs(client, world, engine):
    gone = f"ctr_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) "
                        "VALUES (:i, :p, 'Ledger', 'completed')"), {"i": gone, "p": world.project_a})
    r = _start(client, world, world.operator,
               [{"provider_app_key": "Ledger", "alias": "LEDGER", "env": "preview", "credentials": "none"}])
    assert r.status_code == 200, r.text
    wf = r.json()["run"]["id"]
    with engine.begin() as db:
        db.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": gone})   # the provider is gone
    build = _first_build(engine, world, wf, title="Ledger tester")
    with run_env.for_run(engine, build, world.project_a) as env:
        assert not any(k.startswith("BOSS_LINK_LEDGER_") for k in env)
    with engine.begin() as db:
        asked = links.requests_of(db, wf)
        refused = db.execute(text("SELECT outcome, details FROM identity_security_events "
                                  "WHERE event_type = 'app_link.created' AND target_id = :t"),
                             {"t": asked[0]["id"]}).mappings().all()
    assert [(a["outcome"], a["outcome_detail"]) for a in asked] == [("refused", "provider_not_in_project")]
    assert refused and refused[0]["outcome"] == "failure"
