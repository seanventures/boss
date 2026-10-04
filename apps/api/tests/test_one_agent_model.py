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

        def list_tools(self, url, headers=None):
            return [{"name": "post", "description": "post"},
                    {"name": "delete_channel", "description": "delete"}]

    recorder = _Recorder()
    monkeypatch.setattr(agent_loop, "MCPClient", _Mcp)
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: recorder)
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
    # 2. Delete a built-in MCP server. The shipped cast holds some (Agent World: role specs
    #    name their servers), and a held server is not deleted out from under its agents —
    #    so the operator takes it off them first, which is a removal the seed must keep too.
    with engine.connect() as c:
        holders = c.execute(text("SELECT role_key FROM role_mcp_servers WHERE project_id=:p "
                                 "AND mcp_server_id=:s"), {"p": PROJECT, "s": builtin}).scalars().all()
    for holder in holders:
        assert client.delete("/v1/role-mcp-servers", params={
            "project_id": PROJECT, "role_key": holder, "mcp_server_id": builtin,
            "actor": "ceo"}).status_code == 200
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


# --------------------------------------------------------------------------- #
# Review of milestone/agents, round 2: the version that runs, not the newest one
# --------------------------------------------------------------------------- #
def _audit_spec(captain: str) -> dict:
    return {"entry_step": "audit", "steps": [{
        "id": "audit", "label": "Audit the release", "kind": "capability", "captain": captain,
        "binding": {"agent_work": True, "inputs": {"instructions": "check the release"}}}]}


def _able(client, key: str):
    """An agent that HOLDS a skill. The steps stream refuses, at publish, an agent step given to
    an agent holding nothing ("a step naming an agent that holds nothing is refused here"), so
    the agent these delete-guard tests put on a step must be able to do something (integration
    of the agents and steps streams; what is tested — the guard — is unchanged)."""
    return _create(client, role_key=key, skills=_builtin_skills(client, 1))


def _publish(client, definition: str, captain: str) -> dict:
    r = client.post(f"/v1/workflows/{definition}/versions", json={
        "project_id": PROJECT, "actor": "ceo", "spec": _audit_spec(captain)})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _definition(client) -> str:
    r = client.post("/v1/workflows", json={
        "project_id": PROJECT, "workflow_key": _key("wf"), "name": "Ship it", "actor": "ceo"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _delete(client, key: str, **params):
    return client.delete(f"/v1/agents/{key}",
                         params={"project_id": PROJECT, "actor": "ceo", **params})


def _role_count(engine, key: str) -> int:
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM roles WHERE project_id=:p AND role_key=:k"),
                         {"p": PROJECT, "k": key}).scalar()


def test_an_agent_named_only_in_the_reactivated_older_version_cannot_be_deleted(client, engine):
    """THE PROBE (review round 2): v1 names K, v2 names qa, re-saving v1's spec makes v1 active
    again (duplicate: true). The guard read only the newest version, v2, so DELETE K answered
    200 and tombstoned K, and the next run of v1 was refused with "not a role this workforce
    has" — for good, and with no warning at delete time."""
    key = _key()
    assert _able(client, key).status_code == 201
    definition = _definition(client)
    v1 = _publish(client, definition, key)
    _publish(client, definition, "qa")
    again = _publish(client, definition, key)
    assert again["duplicate"] and again["id"] == v1["id"]
    assert client.get(f"/v1/workflows/{definition}",
                      params={"project_id": PROJECT}).json()["active_version_id"] == v1["id"]

    r = _delete(client, key)
    assert r.status_code == 409, r.text
    assert "v1, the version it runs" in r.json()["detail"]
    assert "another agent" in r.json()["detail"], "the refusal must say what to do"
    # Retiring older versions is no way past the version that runs.
    assert _delete(client, key, retire_older_versions="true").status_code == 409
    assert _role_count(engine, key) == 1

    started = client.post("/v1/workflow-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_version_id": v1["id"],
        "dedupe_key": _key("run"), "input": {}})
    assert "not a role this workforce has" not in started.text


def test_the_active_version_is_guarded_under_a_newer_unactivated_publish(client, engine):
    """publish_version(activate=False) leaves the active version where it was; the newest one
    (which the old guard read) need not name the agent the active one runs."""
    from boss_engine.services import workflows

    key = _key()
    assert _able(client, key).status_code == 201
    definition = _definition(client)
    _publish(client, definition, key)
    with engine.begin() as c:
        workflows.publish_version(c, project_id=PROJECT, definition_id=definition,
                                  spec=_audit_spec("qa"), actor="ceo", activate=False)
    r = _delete(client, key)
    assert r.status_code == 409 and "the version it runs" in r.json()["detail"], r.text


def test_an_older_version_alone_blocks_until_the_delete_retires_it(client, engine):
    """Every startable version is guarded, so an agent once named in v1 of a workflow now at v2
    would otherwise be impossible to delete. The refusal names the way through; taking it
    retires exactly that version, keeps its history, and a start on it then says why not."""
    key = _key()
    assert _able(client, key).status_code == 201
    definition = _definition(client)
    v1 = _publish(client, definition, key)
    v2 = _publish(client, definition, "qa")

    r = _delete(client, key)
    assert r.status_code == 409, r.text
    assert "older version still startable by id" in r.json()["detail"]
    assert "retire those versions" in r.json()["detail"], "the refusal must say what to do"
    named = client.get(f"/v1/console/v2/agents/{key}",
                       params={"project_id": PROJECT}).json()["named_on_steps"]
    assert [(n["version"], n["because"], n["retirable"]) for n in named] == [(1, "older", True)]

    gone = _delete(client, key, retire_older_versions="true")
    assert gone.status_code == 200, gone.text
    assert gone.json()["workflow_versions_retired"] == [v1["id"]]
    assert _role_count(engine, key) == 0
    with engine.connect() as c:
        statuses = dict(c.execute(text(
            "SELECT id, status FROM workflow_versions WHERE workflow_definition_id=:d"),
            {"d": definition}).all())
    assert statuses == {v1["id"]: "retired", v2["id"]: "published"}

    refused = client.post("/v1/workflow-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_version_id": v1["id"],
        "dedupe_key": _key("run"), "input": {}})
    assert refused.status_code in (409, 422), refused.text
    assert "was retired" in refused.text and "current version" in refused.text
    # Re-saving v1 is checked against the roster first, so it cannot bring back a step naming
    # an agent that is gone — and the refusal says what to do.
    again = client.post(f"/v1/workflows/{definition}/versions", json={
        "project_id": PROJECT, "actor": "ceo", "spec": _audit_spec(key)})
    assert again.status_code in (400, 409, 422) and "Agents page" in again.text, again.text


def test_a_retired_version_whose_agent_is_back_is_published_again_on_resave(client, engine):
    """Re-saving a retired version's content, once what it names exists again, must leave the
    workflow on a version a run can start — not active-but-retired."""
    key = _key()
    assert _able(client, key).status_code == 201
    definition = _definition(client)
    v1 = _publish(client, definition, key)
    _publish(client, definition, "qa")
    assert _delete(client, key, retire_older_versions="true").status_code == 200
    assert _able(client, key).status_code == 201

    again = _publish(client, definition, key)
    assert again["id"] == v1["id"] and again["status"] == "published"
    detail = client.get(f"/v1/workflows/{definition}", params={"project_id": PROJECT}).json()
    assert detail["active_version_id"] == v1["id"]


@pytest.mark.parametrize("pointer, said", [("mapping", "mapped"), ("subworkflow", "subworkflow")])
def test_an_older_version_that_work_still_starts_is_never_retired(client, engine, pointer, said):
    """A mapping or a parent workflow's subworkflow step starts that exact version; retiring it
    would break them, so the delete stays refused whatever the caller asks.

    And the refusal says how to move THAT pointer (review of milestone/agents, round 3). It
    told the operator to "save the workflow, then delete again" — which moves neither a mapping
    nor a parent's binding — so doing what it said was refused again, for good. This test only
    checked that the word 'mapped' appeared; it now follows the remedy the refusal gives and
    proves the delete then goes through."""
    from boss_engine.services import workflows

    key = _key()
    assert _able(client, key).status_code == 201
    definition = _definition(client)
    v1 = _publish(client, definition, key)
    v2 = _publish(client, definition, "qa")
    with engine.begin() as c:
        if pointer == "mapping":
            mapping = workflows.create_mapping(
                c, project_id=PROJECT, workflow_version_id=v1["id"],
                selector_kind="work_class", selector_value="bugfix", scope_type="project",
                scope_id=PROJECT, priority=100, actor="ceo")
        else:
            parent = workflows.create_definition(c, project_id=PROJECT, workflow_key=_key("wf"),
                                                 name="Parent", description="", actor="ceo")
            child_step = {"id": "child", "label": "Run the audit", "kind": "subworkflow"}
            parent_v1 = workflows.publish_version(c, project_id=PROJECT, definition_id=parent["id"],
                                      actor="ceo", spec={"entry_step": "child", "steps": [{
                                          **child_step,
                                          "binding": {"workflow_version_id": v1["id"]}}]})
    r = _delete(client, key, retire_older_versions="true")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "save the workflow" not in detail, f"saving does not move a {pointer}: {detail}"
    named = client.get(f"/v1/console/v2/agents/{key}",
                       params={"project_id": PROJECT}).json()["named_on_steps"]
    assert [n["because"] for n in named] == [said]
    # The API and the console say the same thing: the console renders these two fields.
    assert named[0]["where"] in detail and named[0]["remedy"] in detail
    if pointer == "mapping":
        assert "where a mapping still sends bug fixes" in detail
        assert (f"point bug fixes at the current version of 'Ship it' (Point at the current "
                f"version on this agent's page, or POST /v1/workflow-mappings/{mapping['id']}"
                "/repoint)") in detail
    else:
        assert "step 'Run the audit' of 'Parent' v1 starts as a subworkflow" in detail
        assert ("re-bind step 'Run the audit' of 'Parent' to the current version of 'Ship it' "
                "and save 'Parent'") in detail
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM workflow_versions WHERE id=:v"),
                         {"v": v1["id"]}).scalar() == "published"

    # DO WHAT IT SAYS, and the delete goes through.
    if pointer == "mapping":
        moved = client.post(f"/v1/workflow-mappings/{mapping['id']}/repoint",
                            json={"project_id": PROJECT, "actor": "ceo"})
        assert moved.status_code == 200, moved.text
        assert moved.json()["workflow_version_id"] == v2["id"] and moved.json()["moved"]
    else:
        again = client.post(f"/v1/workflows/{parent['id']}/versions", json={
            "project_id": PROJECT, "actor": "ceo", "spec": {"entry_step": "child", "steps": [{
                **child_step, "binding": {"workflow_version_id": v2["id"]}}]}})
        assert again.status_code in (200, 201), again.text
        # Parent v1 is older now and still binds Ship it v1: it is retired WITH it, and the
        # page says so before the delete is pressed, rather than left to fail at that step.
        older = client.get(f"/v1/console/v2/agents/{key}",
                           params={"project_id": PROJECT}).json()["named_on_steps"]
        assert [(n["because"], n["retirable"], n["also_retires"]) for n in older] == [
            ("older", True, [{"workflow": "Parent", "version": 1}])]
    gone = _delete(client, key, retire_older_versions="true")
    assert gone.status_code == 200, gone.text
    expected = [v1["id"]] + ([parent_v1["id"]] if pointer == "subworkflow" else [])
    assert gone.json()["workflow_versions_retired"] == expected
    with engine.connect() as c:
        assert {s for (s,) in c.execute(text(
            "SELECT status FROM workflow_versions WHERE id = ANY(:ids)"),
            {"ids": expected}).all()} == {"retired"}


def test_a_run_stopped_at_a_failed_step_holds_its_version(client, engine):
    """Review of milestone/agents, round 3: only runs in flight held their version, so a run
    stopped at a failed step — still offering Try again — did not. The agent was deleted and its
    older version retired under the run, and Try again carried the run on to a later step
    naming the agent, which failed with "not a role this workforce has"."""
    key = _key()
    assert _able(client, key).status_code == 201
    definition = _definition(client)
    v1 = _publish(client, definition, key)
    _publish(client, definition, "qa")
    run = _key("wfr")
    with engine.begin() as c:
        c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                       "workflow_version_id, actor, status) "
                       "VALUES (:id, :p, :d, :v, 'ceo', 'failed')"),
                  {"id": run, "p": PROJECT, "d": definition, "v": v1["id"]})

    r = _delete(client, key, retire_older_versions="true")
    assert r.status_code == 409, r.text
    assert "stopped at a failed step" in r.json()["detail"]
    assert "stop the run of 'Ship it' v1" in r.json()["detail"], "the refusal must say what to do"
    assert _role_count(engine, key) == 1

    # Stopped by the operator, nothing in it runs again: the version is older and nothing else.
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_runs SET status='cancelled' WHERE id=:id"), {"id": run})
    gone = _delete(client, key, retire_older_versions="true")
    assert gone.status_code == 200, gone.text
    assert gone.json()["workflow_versions_retired"] == [v1["id"]]


def test_an_agent_delete_holds_off_a_publish_until_it_commits(client, engine):
    """Review of milestone/agents, round 3: delete_agent read what names the agent, then
    retired versions and deleted the role, with nothing held. A publish committing in between
    (re-saving an older version makes it active again) left the agent deleted under the version
    that now runs. The delete now holds the project's workflow reach; a publish waits for it."""
    from sqlalchemy.exc import OperationalError

    from boss_engine.services import workflows

    definition = _definition(client)
    deleting = engine.connect()
    tx = deleting.begin()
    try:
        workflows.hold_workflow_reach(deleting, PROJECT, exclusive=True)
        with pytest.raises(OperationalError, match="lock timeout"):
            with engine.begin() as c:
                c.execute(text("SET LOCAL lock_timeout = '300ms'"))
                workflows.publish_version(c, project_id=PROJECT, definition_id=definition,
                                          spec=_audit_spec("qa"), actor="ceo")
    finally:
        tx.rollback()
        deleting.close()
    # Writers hold it SHARED: two at once never wait for each other.
    first, second = engine.connect(), engine.connect()
    try:
        with first.begin(), second.begin():
            second.execute(text("SET LOCAL lock_timeout = '300ms'"))
            workflows.hold_workflow_reach(first, PROJECT)
            workflows.hold_workflow_reach(second, PROJECT)
    finally:
        first.close()
        second.close()
    assert _publish(client, definition, "qa")["version"] == 1


#: Its own project, so deleting a bundled agent here cannot change the roster or capabilities
#: any other test in this module reads (review of milestone/agents, round 3).
CAPTAIN_PROJECT = "project_deleted_bundled_captain"


def test_a_deleted_bundled_agent_is_refused_as_a_captain_at_publish(client, engine):
    """Review round 2 (minor): validate_captain unioned the bundled roles back in, so after
    DELETE /v1/agents/<bundled> a workflow could still be published naming it, and the gap
    only showed at start or minutes into a run.

    Rewritten in round 3: it deleted a bundled agent in the module-wide project — for good,
    since the roster is not reset between tests — and skipped itself when no candidate could be
    deleted, so the guarantee could go unchecked. It now installs the bundled agent in a project
    of its own and always runs."""
    from apps.api.app.config import get_settings
    from boss_engine.services.workflow_capabilities import REGISTERED_ROLES

    victim = "design_critic"
    assert victim in REGISTERED_ROLES, "pick another bundled agent; this one is no longer one"
    with engine.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:id, :w, 'Deleted captain', :id) ON CONFLICT (id) DO NOTHING"),
                  {"id": CAPTAIN_PROJECT, "w": get_settings().default_workspace_id})
        api_main.members.seed_default_members(c, CAPTAIN_PROJECT, "test")
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                       "VALUES (:id, :p, :k, 'Design critic') "
                       "ON CONFLICT (project_id, role_key) DO NOTHING"),
                  {"id": _key("role"), "p": CAPTAIN_PROJECT, "k": victim})
    gone = client.delete(f"/v1/agents/{victim}",
                         params={"project_id": CAPTAIN_PROJECT, "actor": "ceo"})
    assert gone.status_code == 200, gone.text

    definition = client.post("/v1/workflows", json={
        "project_id": CAPTAIN_PROJECT, "workflow_key": _key("wf"), "name": "Ship it",
        "actor": "ceo"})
    assert definition.status_code == 201, definition.text
    r = client.post(f"/v1/workflows/{definition.json()['id']}/versions", json={
        "project_id": CAPTAIN_PROJECT, "actor": "ceo", "spec": _audit_spec(victim)})
    assert r.status_code in (400, 409, 422), r.text
    assert "Agents page" in r.text, "the refusal must say what to do"
    offered = client.get("/v1/delivery-workflows/capabilities",
                         params={"project_id": CAPTAIN_PROJECT}).json()["roles"]
    assert victim not in offered, "the builder must not offer a captain publish refuses"
    assert "qa" in offered and "human" in offered
    # The shared project still has it: nothing here reached past its own project.
    still = client.get("/v1/delivery-workflows/capabilities",
                       params={"project_id": PROJECT}).json()["roles"]
    assert victim in still
