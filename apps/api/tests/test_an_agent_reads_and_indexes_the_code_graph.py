"""The code graph of another project's repositories, as the agent behind the host's own key.

BOSS's own agent (Metatron, from boss-dogfood, as the host service account "Host tools") asked
about the three banking projects and got `kg_cypher — forbidden` three times, then found
kg_nodes empty because nothing indexed a registered repository. Two causes, both fixed here as
classes, against a real database with authz enforced:

* The Host tools role is made ONCE from a code set and was never touched again, so a permission
  the catalogue gained later (knowledge:read) never reached it — nor the key's frozen scopes.
  `host_tools.follow_catalogue` adds what the code offers that the role never held; a
  permission an Owner took away is not given back.
* `code_graph_status` / `index_repository_code` (boss.fundamentals) and the
  /v1/knowledge/repositories routes read and queue the same service, in any project the caller
  may read the graph of.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from boss_engine.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.identity import api_keys, authn, host_tools, migration
from boss_engine.tools import build_discuss_read_registry

ROLE_LOST = "knowledge:read"


@pytest.fixture
def enforced():
    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    yield
    settings.authz_mode, settings.authz_router_modes = saved


@pytest.fixture
def projects(engine):
    """Two projects in the install's org, the second with a registered repository."""
    tag = uuid.uuid4().hex[:8]
    with engine.begin() as db:
        org = migration.default_org(db)
        ws = db.execute(text("SELECT id FROM workspaces WHERE org_id = :o ORDER BY id LIMIT 1"), {"o": org}).scalar()
        a, b = f"cg-a-{tag}", f"cg-b-{tag}"
        for pid in (a, b):
            db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                       {"p": pid, "w": ws})
        db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                        "credential_ref, status, created_by) VALUES (:c, :p, 'token', 'personal_access_token', "
                        "'secret:none', 'active', 'test')"), {"c": f"ghconn_{tag}", "p": b})
        db.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                        "full_name, default_branch, created_by) VALUES (:i, :p, :c, 'bank', 'core', 'bank/core', "
                        "'main', 'test')"), {"i": f"ghrepo_{tag}", "p": b, "c": f"ghconn_{tag}"})
    return {"org": org, "a": a, "b": b}


def _host_key_made_before_knowledge_read(engine, org) -> str:
    """The live install's state: the role and the key hold the set of the day they were made,
    from before the catalogue had knowledge:read."""
    with engine.begin() as db:
        created = host_tools.issue_host_key(db, org_id=org)
        role = db.execute(text("SELECT id FROM identity_access_roles WHERE org_id = :o AND key = :k"),
                          {"o": org, "k": host_tools.HOST_TOOLS_ROLE_KEY}).scalar()
        db.execute(text("DELETE FROM identity_access_role_permissions WHERE role_id = :r AND permission_key = :p"),
                   {"r": role, "p": ROLE_LOST})
        db.execute(text("UPDATE identity_access_role_versions SET permissions = array_remove(permissions, :p) "
                        "WHERE role_id = :r"), {"r": role, "p": ROLE_LOST})
        db.execute(text("UPDATE identity_api_credentials SET scopes = array_remove(scopes, :p) "
                        "WHERE principal_id = :s"), {"p": ROLE_LOST, "s": host_tools.HOST_TOOLS_PRINCIPAL})
        migration.bump_epoch(db, org, "test")
    return created.key


def _auth(engine, key):
    with engine.begin() as db:
        return api_keys.authenticate_key(db, key, authn.parse_api_key(key), ip=None, user_agent=None,
                                         request_id=None)


def _call(engine, auth, session_project, tool, **args):
    return build_discuss_read_registry().invoke(
        tool, EngineContext(settings=get_settings(), engine=engine), project_id=session_project,
        actor="metatron", args=args, auth=auth)


def test_the_host_key_reaches_the_graph_once_its_role_follows_the_catalogue(engine, projects, enforced):
    key = _host_key_made_before_knowledge_read(engine, projects["org"])
    before = _call(engine, _auth(engine, key), projects["a"], "kg_cypher", project=projects["b"],
                   query="MATCH (n) RETURN n LIMIT 1", columns=["n"])
    assert before["ok"] is False and before["error"] == "forbidden" and before["permission"] == ROLE_LOST, before

    with engine.begin() as db:
        grown = host_tools.follow_catalogue(db, org_id=projects["org"])
    assert any(ROLE_LOST in added for added in grown.values()), grown
    after = _call(engine, _auth(engine, key), projects["a"], "kg_cypher", project=projects["b"],
                  query="MATCH (n) RETURN n LIMIT 1", columns=["n"])
    assert after.get("error") != "forbidden", after

    status = _call(engine, _auth(engine, key), projects["a"], "code_graph_status", project=projects["b"])
    assert status["ok"] is True and [r["repo"] for r in status["repositories"]] == ["bank/core"], status
    assert status["repositories"][0]["status"] in ("pending", "queued") and status["project_id"] == projects["b"]

    # Again: nothing new to add, nothing changes.
    with engine.begin() as db:
        assert host_tools.follow_catalogue(db, org_id=projects["org"]) == {}


def test_a_permission_an_owner_took_away_is_not_given_back(engine, projects):
    with engine.begin() as db:
        host_tools.issue_host_key(db, org_id=projects["org"])
        role = db.execute(text("SELECT id, version FROM identity_access_roles WHERE org_id = :o AND key = :k"),
                          {"o": projects["org"], "k": host_tools.HOST_TOOLS_ROLE_KEY}).one()
        db.execute(text("DELETE FROM identity_access_role_permissions WHERE role_id = :r AND permission_key = :p"),
                   {"r": role[0], "p": ROLE_LOST})
        kept = db.execute(text("SELECT array_agg(permission_key) FROM identity_access_role_permissions "
                               "WHERE role_id = :r"), {"r": role[0]}).scalar()
        db.execute(text("UPDATE identity_access_roles SET version = version + 1 WHERE id = :r"), {"r": role[0]})
        db.execute(text("INSERT INTO identity_access_role_versions (role_id, version, permissions, changed_by) "
                        "VALUES (:r, :v, :p, 'usr_owner')"), {"r": role[0], "v": role[1] + 1, "p": kept})
        assert host_tools.follow_catalogue(db, org_id=projects["org"]) == {}
        assert db.execute(text("SELECT count(*) FROM identity_access_role_permissions WHERE role_id = :r "
                               "AND permission_key = :p"), {"r": role[0], "p": ROLE_LOST}).scalar() == 0


def test_index_now_queues_one_job_from_the_tool_and_the_route(engine, projects, enforced):
    key = _host_key_made_before_knowledge_read(engine, projects["org"])
    with engine.begin() as db:
        host_tools.follow_catalogue(db, org_id=projects["org"])
    asked = _call(engine, _auth(engine, key), projects["a"], "index_repository_code", project=projects["b"])
    assert asked["ok"] is True and asked["queued"] == ["bank/core"], asked

    client = TestClient(main.app)
    headers = {"Authorization": f"Bearer {key}"}
    again = client.post("/v1/knowledge/repositories/index", headers=headers,
                        json={"project_id": projects["b"], "actor": "metatron"})
    assert again.status_code == 200, again.text
    assert again.json()["queued"] == [] and again.json()["in_flight"] == ["bank/core"]
    listed = client.get("/v1/knowledge/repositories", headers=headers,
                        params={"project_id": projects["b"], "actor": "metatron"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["repositories"][0]["status"] == "queued"
    graph = client.get("/v1/knowledge/graph-status", headers=headers,
                       params={"project_id": projects["b"], "actor": "metatron"})
    assert graph.status_code == 200 and graph.json()["repositories"][0]["repo"] == "bank/core"
    with engine.begin() as db:
        jobs = db.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND kind = 'knowledge.repository.index'"),
                          {"p": projects["b"]}).scalar()
    assert jobs == 1

    # The worker's route is the worker's alone.
    refused = client.post("/v1/knowledge/repositories/index/execute", headers=headers,
                          json={"job_id": "job_x", "worker_id": "w", "lease_token": "t"})
    assert refused.status_code in (401, 403), refused.text
