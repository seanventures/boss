"""One agent model: a role, what it holds (skills and MCP servers), and optionally a profile.

THE INCIDENTS THIS FILE PINS
----------------------------
* Creating an agent never granted its skills. The wizard wrote them to `roles.spec.allowed_skills`
  and `agent_versions.requested_skills`, which the runtime never reads; the new agent held
  nothing, was left out of auto-pick and refused by every step naming it.
* Naming a new agent "QA" silently overwrote the bundled qa role (register_role's UPDATE path).
* There was no way to give an agent an MCP server, and a bounded agent step got no MCP tool.
* POST /v1/roles used as an update reset autonomy to 0 and detached the reporting line.
* No edit for mission/name/boundaries, and no delete — and deleting under a workflow step must be
  refused with the step named.
* The install/upgrade seed put back what an operator removed: unassigned skills, deleted
  built-in MCP servers, deleted bundled agents, and edits to a bundled agent's spec.
* The Create-agent wizard posted as the literal 'vp_agents', which a 'ceo' API key may not act
  as, so on an authed install it 403'd after registering an orphan role.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main

PROJECT = "boss-dogfood"


def _key(prefix: str = "agent") -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _builtin_skills(client, n: int = 2) -> list[str]:
    items = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"]
    ids = [i["id"] for i in items]
    assert len(ids) >= n, "this project has too few built-in skills; the fixture changed"
    return ids[:n]


def _mcp_server(engine, name: str | None = None, url: str = "http://127.0.0.1:9/mcp") -> dict:
    from boss_engine.services.mcp_registry import register_mcp_server

    with engine.begin() as c:
        return register_mcp_server(c, PROJECT, name or _key("srv"), url, actor="ceo")


def _create(client, **body):
    payload = {"project_id": PROJECT, "actor": "ceo", "name": "Agent " + uuid4().hex[:6]}
    payload.update(body)
    return client.post("/v1/agents", json=payload)


def _holds(client, role_key: str) -> tuple[set[str], set[str]]:
    detail = client.get(f"/v1/console/v2/agents/{role_key}",
                        params={"project_id": PROJECT}).json()
    return ({s["skill_id"] for s in detail["skills"]["holds"]},
            {m["mcp_server_id"] for m in detail["mcp"]["holds"]})


# --------------------------------------------------------------------------- #
# R2: create with holdings
# --------------------------------------------------------------------------- #
def test_a_created_agent_holds_its_skills_and_mcp_servers_at_once(client, engine):
    """The agent the wizard made held nothing until a re-seed; this one holds what it was
    given the moment the request returns, and its own page says so."""
    skills = _builtin_skills(client)
    server = _mcp_server(engine)
    key = _key()
    r = _create(client, name="Release Auditor", role_key=key, purpose="Audits releases.",
                skills=skills, mcp_servers=[{"server_id": server["id"], "tools": ["post"]}])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role_key"] == key and body["mission"] == "Audits releases."
    assert body["autonomy_level"] == 0, "an agent made by hand has earned no autonomy"
    held_skills, held_mcp = _holds(client, key)
    assert held_skills == set(skills)
    assert held_mcp == {server["id"]}
    assert body["mcp"]["holds"][0]["tools"] == ["post"]

    # And it is somebody BOSS can give a step to.
    from boss_engine.config import Settings
    from boss_engine.services import agent_steps

    with engine.connect() as c:
        assert key in {cand["role_key"] for cand in agent_steps.candidates(c, Settings(), PROJECT)}


def test_creating_never_overwrites_an_existing_agent(client, engine):
    """Naming a new agent 'QA' replaced the bundled qa role's mission, skills and boundaries."""
    with engine.connect() as c:
        before = c.execute(text("SELECT spec, autonomy_level FROM roles "
                                "WHERE project_id=:p AND role_key='qa'"), {"p": PROJECT}).one()
    r = _create(client, name="QA", purpose="overwrite me")
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]
    with engine.connect() as c:
        after = c.execute(text("SELECT spec, autonomy_level FROM roles "
                               "WHERE project_id=:p AND role_key='qa'"), {"p": PROJECT}).one()
    assert tuple(after) == tuple(before)


def test_a_create_that_cannot_grant_leaves_nothing_behind(client, engine):
    """All or nothing: a typo in one skill must not leave a role holding the others."""
    key = _key()
    r = _create(client, role_key=key, skills=[_builtin_skills(client, 1)[0], "no.such_skill"])
    assert r.status_code == 422
    assert "no.such_skill" in r.json()["detail"]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM roles WHERE project_id=:p AND role_key=:k"),
                         {"p": PROJECT, "k": key}).scalar() == 0


def test_an_unknown_mcp_server_names_the_ones_that_exist(client, engine):
    server = _mcp_server(engine)
    r = _create(client, role_key=_key(), mcp_servers=["mcp_nope"])
    assert r.status_code == 422
    assert server["name"] in r.json()["detail"], "the refusal must list what is registered"


# --------------------------------------------------------------------------- #
# R2: update
# --------------------------------------------------------------------------- #
def test_an_edit_changes_only_what_it_names(client, engine):
    key = _key()
    assert _create(client, role_key=key, reports_to="cto").status_code == 201
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET autonomy_level = 2 WHERE project_id=:p AND role_key=:k"),
                  {"p": PROJECT, "k": key})
    r = client.patch(f"/v1/agents/{key}", json={
        "project_id": PROJECT, "actor": "ceo", "mission": "New mission",
        "boundaries": {"must": ["cite sources"], "must_not": ["publish"]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mission"] == "New mission"
    assert body["boundaries"] == {"must": ["cite sources"], "must_not": ["publish"]}
    assert body["autonomy_level"] == 2 and body["reports_to"] == "cto"

    cleared = client.patch(f"/v1/agents/{key}", json={
        "project_id": PROJECT, "actor": "ceo", "reports_to": None}).json()
    assert cleared["reports_to"] is None


def test_the_whole_holding_can_be_replaced_or_added_to(client, engine):
    a, b = _builtin_skills(client)
    server = _mcp_server(engine)
    key = _key()
    assert _create(client, role_key=key, skills=[a]).status_code == 201

    added = client.patch(f"/v1/agents/{key}", json={
        "project_id": PROJECT, "actor": "ceo", "grant_skills": [b],
        "grant_mcp_servers": [server["id"]]})
    assert added.status_code == 200, added.text
    assert _holds(client, key) == ({a, b}, {server["id"]})

    replaced = client.patch(f"/v1/agents/{key}", json={
        "project_id": PROJECT, "actor": "ceo", "skills": [b], "mcp_servers": []})
    assert replaced.status_code == 200, replaced.text
    assert _holds(client, key) == ({b}, set())


def test_a_reporting_loop_is_refused_with_the_reason(client):
    one, two = _key(), _key()
    assert _create(client, role_key=one).status_code == 201
    assert _create(client, role_key=two, reports_to=one).status_code == 201
    r = client.patch(f"/v1/agents/{one}", json={
        "project_id": PROJECT, "actor": "ceo", "reports_to": two})
    assert r.status_code == 422
    assert "loop" in r.json()["detail"]


def test_re_registering_a_role_keeps_its_autonomy_and_manager(client, engine):
    """POST /v1/roles as an update wrote autonomy 0 and reports_to NULL when left out."""
    key = _key()
    assert _create(client, role_key=key, reports_to="cto").status_code == 201
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET autonomy_level = 3 WHERE project_id=:p AND role_key=:k"),
                  {"p": PROJECT, "k": key})
    r = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": key,
                                       "display_name": "Renamed"})
    assert r.status_code == 201, r.text
    with engine.connect() as c:
        row = c.execute(text("SELECT display_name, autonomy_level, reports_to FROM roles "
                             "WHERE project_id=:p AND role_key=:k"),
                        {"p": PROJECT, "k": key}).one()
    assert tuple(row) == ("Renamed", 3, "cto")


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #
def _workflow_naming(engine, role_key: str, step: dict | None = None) -> str:
    """A published workflow whose step names `role_key` (as its captain unless `step` says)."""
    definition, version = _key("wfd"), _key("wfv")
    step = step or {"captain": role_key, "binding": {"agent_work": True}}
    spec = {"steps": [{"id": "audit", "label": "Audit the release", "kind": "capability",
                       **step}]}
    with engine.begin() as c:
        c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                       "created_by) VALUES (:id, :p, :k, 'Ship it', 'test')"),
                  {"id": definition, "p": PROJECT, "k": _key("wf")})
        c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                       "version, spec, content_hash, created_by) VALUES (:id, :p, :d, 1, "
                       "CAST(:s AS JSONB), :h, 'test')"),
                  {"id": version, "p": PROJECT, "d": definition, "s": json.dumps(spec),
                   "h": uuid4().hex})
    return definition


def test_delete_is_refused_while_a_workflow_step_names_the_agent(client, engine):
    key = _key()
    assert _create(client, role_key=key, skills=_builtin_skills(client, 1)).status_code == 201
    definition = _workflow_naming(engine, key)

    r = client.delete(f"/v1/agents/{key}", params={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "Ship it" in detail and "Audit the release" in detail
    assert "another agent" in detail, "the refusal must say what to do"
    # The page knows before anyone presses.
    named = client.get(f"/v1/console/v2/agents/{key}", params={"project_id": PROJECT}).json()
    assert named["named_on_steps"][0]["step"] == "Audit the release"

    with engine.begin() as c:
        c.execute(text("UPDATE workflow_definitions SET status='retired' WHERE id=:d"),
                  {"d": definition})
    gone = client.delete(f"/v1/agents/{key}", params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200, gone.text
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM roles WHERE project_id=:p AND role_key=:k"),
                         {"p": PROJECT, "k": key}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM role_skills WHERE project_id=:p "
                              "AND role_key=:k"), {"p": PROJECT, "k": key}).scalar() == 0


@pytest.mark.parametrize("how, step", [
    ("approves it", lambda key: {"binding": {"capability": "approve"},
                                 "approval": {"mode": "manual", "approver_role": key}}),
    ("runs it", lambda key: {"binding": {"capability": "deploy", "role_key": key}}),
])
def test_a_gate_approver_or_a_binding_role_also_blocks_the_delete(client, engine, how, step):
    """Review of milestone/agents, round 1: the guard read only captain and binding.agent(s), so
    an agent named as a gate's approver (approval.approver_role) or a binding's role_key was
    deleted with 200 and tombstoned — the gate broken for good, and no seed to bring it back."""
    key = _key()
    assert _create(client, role_key=key).status_code == 201
    _workflow_naming(engine, key, step(key))

    r = client.delete(f"/v1/agents/{key}", params={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 409, r.text
    assert how in r.json()["detail"] and "Audit the release" in r.json()["detail"]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM roles WHERE project_id=:p AND role_key=:k"),
                         {"p": PROJECT, "k": key}).scalar() == 1


def test_deleting_moves_its_reports_up_and_retires_its_studio_profiles(client, engine):
    boss, report = _key(), _key()
    assert _create(client, role_key=boss, reports_to="cto").status_code == 201
    assert _create(client, role_key=report, reports_to=boss).status_code == 201
    from boss_engine.config import Settings
    from boss_engine.services.agent_studio import AgentStudioService

    studio = AgentStudioService(Settings())
    with engine.begin() as c:
        definition = studio.create_agent(c, project_id=PROJECT, agent_key=_key("prof"),
                                         name="Profile", description="", owner="ceo",
                                         actor="ceo")
        c.execute(text(
            "INSERT INTO agent_versions (id, project_id, agent_definition_id, version, role_key, "
            "content_hash, created_by) VALUES (:id, :p, :d, 1, :r, :h, 'test')"),
            {"id": _key("av"), "p": PROJECT, "d": definition["id"], "r": boss, "h": uuid4().hex})

    r = client.delete(f"/v1/agents/{boss}", params={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 200, r.text
    assert r.json()["reports_moved"] == [report]
    with engine.connect() as c:
        assert c.execute(text("SELECT reports_to FROM roles WHERE project_id=:p AND role_key=:k"),
                         {"p": PROJECT, "k": report}).scalar() == "cto"
        assert c.execute(text("SELECT status FROM agent_definitions WHERE id=:d"),
                         {"d": definition["id"]}).scalar() == "retired"


# --------------------------------------------------------------------------- #
# MCP grants
# --------------------------------------------------------------------------- #
def test_an_mcp_server_is_given_and_taken_back_independently(client, engine):
    server = _mcp_server(engine)
    key = _key()
    assert _create(client, role_key=key).status_code == 201
    given = client.post("/v1/role-mcp-servers", json={
        "project_id": PROJECT, "actor": "ceo", "role_key": key,
        "mcp_server_id": server["name"], "tools": ["post", "read"]})
    assert given.status_code == 201, given.text
    assert given.json()["tools"] == ["post", "read"], "a server may be named by its unique name"
    listed = client.get("/v1/role-mcp-servers", params={"project_id": PROJECT,
                                                        "role_key": key}).json()
    assert [a["mcp_server_id"] for a in listed["assignments"]] == [server["id"]]
    taken = client.delete("/v1/role-mcp-servers", params={
        "project_id": PROJECT, "role_key": key, "mcp_server_id": server["id"], "actor": "ceo"})
    assert taken.status_code == 200
    again = client.delete("/v1/role-mcp-servers", params={
        "project_id": PROJECT, "role_key": key, "mcp_server_id": server["id"], "actor": "ceo"})
    assert again.status_code == 404 and "does not hold" in again.json()["detail"]


class _Recorder:
    def __init__(self) -> None:
        self.offered: list[list[str]] = []

    def create_chat_completion(self, messages, tools=None, **kwargs):
        self.offered.append(sorted(str((t.get("function") or {}).get("name") or "")
                                   for t in (tools or [])))
        return ({"role": "assistant", "content": "Done.",
                 "raw": {"role": "assistant", "content": "Done."}, "tool_calls": []},
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


def test_a_bounded_agent_step_is_offered_exactly_its_granted_mcp_tools(
        client, engine, monkeypatch):
    """Registered is not granted. Of two servers in the project, the agent holds one, and of
    that one's tools only `post`; the step is offered `post` and nothing else from MCP."""
    from boss_engine.config import Settings
    from boss_engine.services import agent_loop, agent_steps

    held = _mcp_server(engine, name=_key("chat"), url="http://held.invalid/mcp")
    _mcp_server(engine, name=_key("other"), url="http://other.invalid/mcp")
    key = _key()
    assert _create(client, role_key=key, mcp_servers=[
        {"server_id": held["id"], "tools": ["post"]}]).status_code == 201

    class _Mcp:
        def __init__(self, _settings):
            pass

        def list_tools(self, url):
            return [{"name": "post", "description": "post"},
                    {"name": "delete_channel", "description": "delete"}]

    recorder = _Recorder()
    monkeypatch.setattr(agent_loop, "MCPClient", _Mcp)
    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _s: recorder)
    with engine.begin() as c:
        out = agent_steps.run(c, Settings(), PROJECT, instructions="Post the notes.",
                              role_key=key, actor="test")
    offered = recorder.offered[0]
    mcp_tools = [t for t in offered if t.startswith("mcp_")]
    assert len(mcp_tools) == 1 and mcp_tools[0].endswith("_post"), offered
    assert out["mcp_servers_offered"] == [held["name"]]
    for forbidden in ("run_shell", "query_db"):
        assert forbidden not in offered


# --------------------------------------------------------------------------- #
# The seed is additive only
# --------------------------------------------------------------------------- #
def _reseed(engine) -> None:
    from boss_engine.bootstrap import seed_project_defaults
    from boss_engine.config import get_settings
    from boss_engine.services.entitlements import EntitlementBrokerService
    from boss_engine.services.members import MemberDirectoryService
    from boss_engine.services.specpack import SpecPackService

    settings = get_settings()
    with engine.begin() as c:
        seed_project_defaults(c, settings, PROJECT,
                              entitlements=EntitlementBrokerService(settings),
                              members=MemberDirectoryService(settings),
                              specpack=SpecPackService(settings), actor="test-reseed")


def test_a_reseed_never_puts_back_what_an_operator_removed(client, engine):
    """Run the whole install seed after four operator decisions; none is undone."""
    from boss_engine.bootstrap import BUILTIN_MCP_SERVERS

    _reseed(engine)  # converge first, so what follows is a removal, not a never-had
    with engine.connect() as c:
        spec_grant = c.execute(text(
            "SELECT role_key, skill_id FROM role_skills WHERE project_id=:p "
            "AND source='builtin' ORDER BY role_key, skill_id LIMIT 1"), {"p": PROJECT}).one()
        builtin = c.execute(text("SELECT id FROM mcp_servers WHERE project_id=:p AND name=:n"),
                            {"p": PROJECT, "n": BUILTIN_MCP_SERVERS[0]["name"]}).scalar()
    assert builtin, "the built-in MCP server was never seeded; the fixture changed"
    role, skill = spec_grant

    # 1. Take a spec-named skill off its agent.
    assert client.delete("/v1/role-skills", params={
        "project_id": PROJECT, "role_key": role, "skill_id": skill,
        "actor": "ceo"}).status_code == 200
    # 2. Delete a built-in MCP server.
    assert client.delete(f"/v1/mcp/servers/{builtin}",
                         params={"project_id": PROJECT}).status_code == 204
    # 3. Edit a bundled agent's mission.
    assert client.patch("/v1/agents/qa", json={
        "project_id": PROJECT, "actor": "ceo", "mission": "Operator's own words"}).status_code == 200
    # 4. Delete a bundled agent nothing names.
    victim = "design_critic"
    deleted = client.delete(f"/v1/agents/{victim}", params={"project_id": PROJECT, "actor": "ceo"})
    if deleted.status_code == 409:
        pytest.skip(f"{victim} is named or has history on this fixture: {deleted.json()['detail']}")
    assert deleted.status_code == 200, deleted.text

    _reseed(engine)

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM role_skills WHERE project_id=:p "
                              "AND role_key=:r AND skill_id=:s"),
                         {"p": PROJECT, "r": role, "s": skill}).scalar() == 0, \
            "the seed re-granted a skill the operator took away"
        assert c.execute(text("SELECT count(*) FROM mcp_servers WHERE project_id=:p AND name=:n"),
                         {"p": PROJECT, "n": BUILTIN_MCP_SERVERS[0]["name"]}).scalar() == 0, \
            "the seed re-registered an MCP server the operator deleted"
        assert c.execute(text("SELECT spec->>'mission' FROM roles WHERE project_id=:p "
                              "AND role_key='qa'"), {"p": PROJECT}).scalar() == \
            "Operator's own words", "the seed rewrote an operator's edit"
        assert c.execute(text("SELECT count(*) FROM roles WHERE project_id=:p AND role_key=:r"),
                         {"p": PROJECT, "r": victim}).scalar() == 0, \
            "the seed reinstalled an agent the operator deleted"

    # And giving it back by hand is a new decision that wins.
    assert client.post("/v1/role-skills", json={
        "project_id": PROJECT, "role_key": role, "skill_id": skill,
        "actor": "ceo"}).status_code == 201
    _reseed(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM role_skills WHERE project_id=:p "
                              "AND role_key=:r AND skill_id=:s"),
                         {"p": PROJECT, "r": role, "s": skill}).scalar() == 1


# --------------------------------------------------------------------------- #
# Studio: the grant is the permission
# --------------------------------------------------------------------------- #
def test_a_studio_profile_may_request_what_its_agent_holds(client, engine):
    """Studio refused a profile asking for a skill the agent had just been GIVEN
    ('exceeds RoleSpec permissions') because it read only spec.allowed_skills."""
    skill = _builtin_skills(client, 1)[0]
    key = _key()
    assert _create(client, role_key=key, skills=[skill]).status_code == 201
    from boss_engine.config import Settings
    from boss_engine.services.agent_studio import AgentStudioService

    studio = AgentStudioService(Settings())
    with engine.begin() as c:
        definition = studio.create_agent(c, project_id=PROJECT, agent_key=_key("prof"),
                                         name="P", description="", owner="ceo", actor="ceo")
        version = studio.create_agent_version(
            c, definition["id"], project_id=PROJECT, actor="ceo", role_key=key,
            model_policy={"execution_mode": "deterministic_brief", "selection_mode": "router"},
            prompts={"system": "s", "job": "j"}, rules=["r"], requested_skills=[skill],
            requested_integrations=[], scopes={"project": "current", "data_boundary": "internal"},
            limits={"max_steps": 2, "max_calls": 2, "timeout_seconds": 60, "max_retries": 0},
            output_contract={"artifact_type": "brief", "sections": []},
            a2a_policy={"publish": [], "subscribe": []}, provenance={})
        result = studio.validate_agent_version(c, PROJECT, version["id"], actor="ceo")
    assert not any("exceed" in e for e in result["errors"]), result["errors"]


# --------------------------------------------------------------------------- #
# The actor is the signed-in principal
# --------------------------------------------------------------------------- #
def test_on_an_authed_install_the_principal_is_the_actor(client, engine, monkeypatch):
    """The wizard posted 'vp_agents'; a 'ceo' key may not act as it. The agent resource binds
    to the principal, so the body's claim cannot make a create 403 — or impersonate."""
    key_value = f"ceo-{uuid4().hex[:10]}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", key_value)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{key_value}:ceo")
    key = _key()
    r = client.post("/v1/agents", headers={"Authorization": f"Bearer {key_value}"},
                    json={"project_id": PROJECT, "actor": "vp_agents", "name": "Bound",
                          "role_key": key})
    assert r.status_code == 201, r.text
    with engine.connect() as c:
        who = c.execute(text("SELECT actor FROM events WHERE project_id=:p "
                             "AND event_type='agent.created' AND entity_id=:k"),
                        {"p": PROJECT, "k": key}).scalar()
    assert who == "ceo"


# --------------------------------------------------------------------------- #
# The chat can do it too
# --------------------------------------------------------------------------- #
def test_the_chat_gives_an_agent_a_skill_and_an_mcp_server(client, engine):
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    registry = build_default_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    key = _key()
    assert _create(client, role_key=key).status_code == 201
    skill = _builtin_skills(client, 1)[0]
    server = _mcp_server(engine)

    given = registry.invoke("assign_skill_to_agent", ctx, project_id=PROJECT, actor="ceo",
                            args={"agent": key, "skill": skill})
    assert given.get("assigned") is True, given
    # AN MCP GRANT IS OFFERED, NOT PRESSED: it hands the agent a server's credentials. The
    # model's own call comes back as a proposal and changes nothing; the operator's click runs it.
    args = {"agent": key, "server": server["name"], "tools": ["post"]}
    offered = registry.invoke("assign_mcp_to_agent", ctx, project_id=PROJECT, actor="ceo",
                              args=args)
    assert offered.get("requires_confirmation") is True, offered
    assert _holds(client, key) == ({skill}, set()), "a proposal must not grant anything"
    mcp = registry.invoke("assign_mcp_to_agent", ctx, project_id=PROJECT, actor="ceo",
                          args=offered["proposed_args"], operator_confirmed=True)
    assert mcp.get("assigned") is True, mcp
    assert _holds(client, key) == ({skill}, {server["id"]})

    wrong = registry.invoke("assign_skill_to_agent", ctx, project_id=PROJECT, actor="ceo",
                            args={"agent": "nobody_here", "skill": skill})
    assert "Available:" in str(wrong), "a wrong agent must be answered with the ones that exist"


# --------------------------------------------------------------------------- #
# Review of milestone/agents, round 1
# --------------------------------------------------------------------------- #
def test_an_agent_made_here_can_captain_a_step_the_moment_it_exists(client):
    """Quick create promised the new agent could be put on a step; validate_captain knew only
    the bundled roles, so publishing a workflow that named it was refused."""
    key = _key()
    assert _create(client, role_key=key, skills=_builtin_skills(client, 1)).status_code == 201
    offered = client.get("/v1/delivery-workflows/capabilities",
                         params={"project_id": PROJECT}).json()["roles"]
    assert key in offered, "the builder must offer the new agent as a captain"

    definition = client.post("/v1/workflows", json={
        "project_id": PROJECT, "workflow_key": _key("wf"), "name": "Audit", "actor": "ceo"})
    assert definition.status_code == 201, definition.text
    version = client.post(f"/v1/workflows/{definition.json()['id']}/versions", json={
        "project_id": PROJECT, "actor": "ceo",
        "spec": {"entry_step": "audit", "steps": [{
            "id": "audit", "kind": "capability", "captain": key,
            "binding": {"agent_work": True, "inputs": {"instructions": "check the release"}}}]}})
    assert version.status_code == 201, version.text

    typo = client.post(f"/v1/workflows/{definition.json()['id']}/versions", json={
        "project_id": PROJECT, "actor": "ceo",
        "spec": {"entry_step": "audit", "steps": [{
            "id": "audit", "kind": "capability", "captain": key + "_typo",
            "binding": {"agent_work": True, "inputs": {"instructions": "check"}}}]}})
    assert typo.status_code in (400, 409, 422), typo.text
    assert "Agents page" in typo.text, "a refused captain must say what to do"


def test_a_name_with_no_english_letters_still_makes_an_agent(client):
    """'காணொளி தொகுப்பாளர்' slugged to '' and was refused as "'' cannot be an agent key"."""
    r = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                        "name": "காணொளி தொகுப்பாளர்"})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "காணொளி தொகுப்பாளர்"
    assert r.json()["role_key"].startswith("agent_")
    bad = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                          "name": "X", "role_key": "9x"})
    assert bad.status_code == 422
    assert "leave the key empty" in bad.json()["detail"]


def test_an_edit_through_post_roles_and_a_remade_bundled_agent_survive_a_reseed(client, engine):
    """POST /v1/roles recorded no edit, so the seed put the pack's YAML back; and an agent made
    by hand under a deleted bundled key was overwritten with the pack's spec."""
    _reseed(engine)
    r = client.post("/v1/roles", json={
        "project_id": PROJECT, "role_key": "security_reviewer",
        "spec": {"mission": "Edited through POST /v1/roles", "display_name": "Sec"}})
    assert r.status_code == 201, r.text

    victim = "ux_engineer"
    deleted = client.delete(f"/v1/agents/{victim}", params={"project_id": PROJECT,
                                                             "actor": "ceo"})
    if deleted.status_code == 409:
        pytest.skip(f"{victim} is named or has history here: {deleted.json()['detail']}")
    remade = _create(client, role_key=victim, name="UX by hand", purpose="Mine now")
    assert remade.status_code == 201, remade.text

    _reseed(engine)
    with engine.connect() as c:
        missions = dict(c.execute(text(
            "SELECT role_key, spec->>'mission' FROM roles WHERE project_id=:p "
            "AND role_key IN ('security_reviewer', :v)"), {"p": PROJECT, "v": victim}).all())
    assert missions == {"security_reviewer": "Edited through POST /v1/roles",
                        victim: "Mine now"}


def test_a_non_human_caller_is_held_on_every_agent_write(client, engine, monkeypatch):
    """POST /v1/roles was held for a person's confirmation; POST /v1/agents, PATCH
    /v1/agents/{key} and POST /v1/role-mcp-servers were not, so an MCP client could strip an
    agent's must_not boundaries with nobody confirming."""
    suffix = uuid4().hex[:10]
    bot_key, bot = f"bot-{suffix}", f"bot_member_{suffix}"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", bot_key)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{bot_key}:{bot}")
    with engine.begin() as c:
        api_main.members.upsert_member(
            c, project_id=PROJECT, member_id=bot, display_name="A bot", member_type="agent",
            role_keys=[], permissions=["role_review", "workflow_manage"], actor="ceo")
    headers = {"Authorization": f"Bearer {bot_key}"}
    server = _mcp_server(engine)
    calls = [
        ("POST", "/v1/agents", {"project_id": PROJECT, "name": "Sneaky"}),
        ("PATCH", "/v1/agents/qa", {"project_id": PROJECT,
                                    "boundaries": {"must": [], "must_not": []}}),
        ("POST", "/v1/role-mcp-servers", {"project_id": PROJECT, "role_key": "qa",
                                          "mcp_server_id": server["id"]}),
    ]
    for method, path, body in calls:
        r = client.request(method, path, json=body, headers=headers)
        assert r.status_code == 409, f"{method} {path}: {r.status_code} {r.text}"
        assert r.json()["detail"] == "mcp_confirmation_required", (method, path)
