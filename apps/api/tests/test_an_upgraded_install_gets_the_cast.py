"""An upgraded install gets the shipped cast, in every project, with full identities.

On boss-1 (boss-dogfood, made before the Agent World) Atlas and the rest of the new cast were in
the YAMLs but not in the project: the Passport read "No registered role", every identity field
"—", and Ask failed with "this project has no agent chief_architect". Roles arrived only through
the install seed; the API's boot synced nothing. These tests drive the boot path
(`main._install_shipped_cast` → shipped_cast.sync_all) against a project that predates the cast.
"""
from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"
ACTOR = "ceo"
AGENT_ID = re.compile(r"^agt_[0-9a-hjkmnp-tv-z]{10}$")


def _settings():
    from apps.api.app import main as api_main

    return api_main.settings


def _shipped_keys(exclude: set[str] = frozenset()) -> dict[str, tuple[str, str]]:
    """{role_key: (pack_id, version)} for the core pack and the archetypes, first pack wins."""
    from boss_engine.services.specpack import SpecPackService

    specpack = SpecPackService(_settings())
    out: dict[str, tuple[str, str]] = {}
    for pack_id in (_settings().core_pack_id, "archetypes_pack"):
        pack = specpack.load_pack_spec(pack_id)
        for key in pack["installs"]["roles"]:
            out.setdefault(key, (pack_id, str(pack["version"])))
    # Named by the core pack's coding workflows and the "engineer" seat, shipped by the
    # software factory pack: installed with the cast (shipped_cast.referenced_roles).
    if "software_engineer" not in out:
        out["software_engineer"] = specpack_home("software_engineer")
    return {k: v for k, v in out.items() if k not in exclude}


def specpack_home(key: str) -> tuple[str, str]:
    from boss_engine.services.shipped_cast import home_pack
    from boss_engine.services.specpack import SpecPackService

    return home_pack(SpecPackService(_settings()), key, _settings())


def _coo_mission() -> str:
    from boss_engine.services.specpack import SpecPackService

    return SpecPackService(_settings()).load_role_spec("coo")["mission"]


def _yaml_persona(key: str) -> dict:
    from boss_engine.services.specpack import SpecPackService

    return SpecPackService(_settings()).load_role_spec(key)["persona"]


@pytest.fixture()
def old_project(engine):
    """A project from before the Agent World: its skills, an operator-edited qa, a legacy coo
    with no persona and no identity, and an sre the operator deleted. No cast."""
    from boss_engine.services import seed_exclusions
    from boss_engine.services.entitlements import EntitlementBrokerService
    from boss_engine.services.skill_registry import seed_system_skills
    from boss_engine.services.specpack import SpecPackService

    project = f"up-{uuid4().hex[:8]}"
    coo = SpecPackService(_settings()).load_role_spec("coo")
    coo.pop("persona")
    with engine.begin() as c:
        workspace = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                              {"p": PROJECT}).scalar_one()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:id, :w, 'Before the cast', :id)"),
                  {"id": project, "w": workspace})
        seed_system_skills(c, project, EntitlementBrokerService(_settings()).list_skills(),
                           "old-install")
        for key, spec in (("qa", {"mission": "The operator's QA.",
                                  "persona": {"callsign": "Gatekeeper", "tagline": "Mine."}}),
                          ("coo", coo)):
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                           "VALUES (:id, :p, :k, :k, CAST(:s AS JSONB))"),
                      {"id": f"role_{uuid4().hex[:16]}", "p": project, "k": key,
                       "s": json.dumps(spec)})
        seed_exclusions.exclude(c, project, "role_spec", "qa", actor=ACTOR,
                                reason="edited by the operator")
        seed_exclusions.exclude(c, project, "role", "sre", actor=ACTOR,
                                reason="deleted by the operator")
    return project


def _roles(engine, project: str) -> dict[str, dict]:
    with engine.connect() as c:
        return {r["role_key"]: dict(r) for r in c.execute(text(
            "SELECT role_key, spec, agent_id, born_by, born_from, fingerprint, identity_version "
            "FROM roles WHERE project_id = :p"), {"p": project}).mappings()}


def _count(engine, sql: str, project: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(sql), {"p": project}).scalar() or 0)


def test_an_existing_project_gets_the_whole_cast_on_boot_with_full_identities(
        engine, old_project):
    from apps.api.app import main as api_main

    results = api_main._install_shipped_cast()
    assert old_project in results, "the boot sync skipped a project that is not the default"
    expected = _shipped_keys(exclude={"sre"})
    roles = _roles(engine, old_project)

    # Every shipped agent is registered, except the one the operator deleted.
    assert set(expected) <= set(roles), sorted(set(expected) - set(roles))
    assert "sre" not in roles, "an agent the operator deleted came back"
    installed = set(results[old_project]["installed"])
    assert installed == set(expected) - {"qa", "coo"}
    for key in installed:
        row = roles[key]
        pack_id, version = expected[key]
        assert AGENT_ID.match(row["agent_id"] or ""), key
        assert row["born_by"] == "spec-pack", key
        assert row["born_from"] == {"kind": "pack", "pack_id": pack_id,
                                    "module_version": version}, key
        assert len(row["fingerprint"] or "") == 64 and row["identity_version"] == 1, key
        assert row["spec"]["persona"]["callsign"] == _yaml_persona(key)["callsign"], key
    with engine.connect() as c:
        born = {r for r in c.execute(text(
            "SELECT data->>'role_key' FROM events WHERE project_id = :p "
            "AND event_type = 'agent.born'"), {"p": old_project}).scalars()}
    assert born == installed, "a newly installed agent is born; an existing one is not re-born"

    # The operator's qa keeps its persona and mission; only its identity is new.
    qa = roles["qa"]
    assert qa["spec"]["mission"] == "The operator's QA."
    assert qa["spec"]["persona"] == {"callsign": "Gatekeeper", "tagline": "Mine."}
    assert AGENT_ID.match(qa["agent_id"] or "") and qa["fingerprint"]
    # The legacy coo had no persona: it gets the shipped one, and keeps everything else.
    assert roles["coo"]["spec"]["persona"]["callsign"] == "Cadence"
    assert roles["coo"]["spec"]["mission"] == _coo_mission()
    assert AGENT_ID.match(roles["coo"]["agent_id"] or "")

    # Holdings: Atlas holds the skills and the MCP servers its role names.
    with engine.connect() as c:
        skills = set(c.execute(text("SELECT skill_id FROM role_skills WHERE project_id = :p "
                                    "AND role_key = 'chief_architect'"),
                               {"p": old_project}).scalars())
        servers = set(c.execute(text(
            "SELECT s.name FROM role_mcp_servers g JOIN mcp_servers s ON s.id = g.mcp_server_id "
            "WHERE g.project_id = :p AND g.role_key = 'chief_architect'"),
            {"p": old_project}).scalars())
    assert "model.generate_text" in skills, sorted(skills)
    assert servers == {"boss-devops-reference", "web-intel"}

    # A passport for a newly installed agent is complete.
    from boss_engine.services import agent_identity

    with engine.begin() as c:
        passport = agent_identity.passport(c, old_project, "chief_architect")
    assert passport["persona"]["callsign"] == "Atlas"
    assert passport["agent_id"] == roles["chief_architect"]["agent_id"]
    assert passport["born"]["by"] == "spec-pack" and passport["born"]["at"]
    assert passport["version"] == 1


def test_the_boot_sync_is_idempotent(engine, old_project):
    from apps.api.app import main as api_main

    api_main._install_shipped_cast()
    before = _roles(engine, old_project)
    counts = [_count(engine, sql, old_project) for sql in (
        "SELECT count(*) FROM events WHERE project_id = :p",
        "SELECT count(*) FROM role_skills WHERE project_id = :p",
        "SELECT count(*) FROM role_mcp_servers WHERE project_id = :p",
        "SELECT count(*) FROM mcp_servers WHERE project_id = :p")]

    again = api_main._install_shipped_cast()[old_project]
    assert again["installed"] == [] and again["filled"] == [] and again["identities"] == 0
    assert _roles(engine, old_project) == before, "a second boot changed an agent"
    assert [_count(engine, sql, old_project) for sql in (
        "SELECT count(*) FROM events WHERE project_id = :p",
        "SELECT count(*) FROM role_skills WHERE project_id = :p",
        "SELECT count(*) FROM role_mcp_servers WHERE project_id = :p",
        "SELECT count(*) FROM mcp_servers WHERE project_id = :p")] == counts, (
        "a second boot wrote something")


def test_a_skill_or_server_the_operator_took_away_stays_away(engine, old_project):
    from boss_engine.services import agents, seed_exclusions
    from boss_engine.services.skill_definitions import unassign_skill_from_role

    from apps.api.app import main as api_main

    api_main._install_shipped_cast()
    with engine.begin() as c:
        unassign_skill_from_role(c, project_id=old_project, actor=ACTOR,
                                 role_key="chief_architect", skill_id="model.generate_text")
        server = c.execute(text("SELECT id FROM mcp_servers WHERE project_id = :p "
                                "AND name = 'web-intel'"), {"p": old_project}).scalar_one()
        agents.revoke_mcp(c, old_project, actor=ACTOR, role_key="chief_architect",
                          mcp_server_id=server)
        assert seed_exclusions.excluded(c, old_project, "role_skill")
    api_main._install_shipped_cast()
    with engine.connect() as c:
        skills = set(c.execute(text("SELECT skill_id FROM role_skills WHERE project_id = :p "
                                    "AND role_key = 'chief_architect'"),
                               {"p": old_project}).scalars())
        held = c.execute(text("SELECT count(*) FROM role_mcp_servers WHERE project_id = :p "
                              "AND role_key = 'chief_architect' AND mcp_server_id = :s"),
                         {"p": old_project, "s": server}).scalar()
    assert "model.generate_text" not in skills and held == 0


# ── Ask works for a newly installed agent ────────────────────────────────────────────────────


class _Model:
    def __init__(self):
        self.calls: list[dict] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.calls.append({"system": system_prompt or "", "prompt": prompt})
        usage = {"model": "stub-1", "prompt_tokens": 100, "completion_tokens": 30,
                 "total_tokens": 130, "cost_usd_micros": 1000}
        return "r", "Atlas here: what did we reject, and why?", usage


@pytest.fixture()
def model(monkeypatch):
    from boss_engine.services import agent_converse

    stub = _Model()
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: stub)
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(agent_converse, "REVEAL_SECONDS", 0)
    return stub


def _forget(engine, key: str) -> None:
    """The default project as boss-1 had it: this agent was never installed."""
    with engine.begin() as c:
        for table in ("role_skills", "role_mcp_servers"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p AND role_key = :k"),
                      {"p": PROJECT, "k": key})
        c.execute(text("DELETE FROM roles WHERE project_id = :p AND role_key = :k"),
                  {"p": PROJECT, "k": key})


def test_ask_works_for_an_agent_the_boot_installed(client, engine, model):
    from apps.api.app import main as api_main

    _forget(engine, "chief_architect")
    refused = client.post("/v1/conversations", json={
        "project_id": PROJECT, "actor": ACTOR, "kind": "talk", "seats": ["chief_architect"]})
    assert refused.status_code == 422 and "no agent chief_architect" in refused.text

    assert "chief_architect" in api_main._install_shipped_cast()[PROJECT]["installed"]
    made = client.post("/v1/conversations", json={
        "project_id": PROJECT, "actor": ACTOR, "kind": "talk", "seats": ["chief_architect"]})
    assert made.status_code == 201, made.text
    reply = client.post(f"/v1/conversations/{made.json()['conversation_id']}/turns", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "Should this be one service or two?"})
    assert reply.status_code == 200, reply.text
    events = [dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
              for block in reply.text.strip().split("\n\n")]
    said = "".join(json.loads(e["data"]).get("text", "") for e in events
                   if e.get("event") == "token")
    end = next(json.loads(e["data"]) for e in events if e.get("event") == "turn_end")
    assert end["status"] == "complete" and said.startswith("Atlas here")
    assert "You are Atlas, the Chief Architect" in model.calls[-1]["system"]


def test_a_core_project_registers_every_shipped_agent_its_work_names(engine, old_project):
    """software_engineer is named by core_v1's coding workflows and the "engineer" seat, but no
    core pack installs it: work done under that name showed as an unregistered shipped agent."""
    from boss_engine.services.agent_identity import resolve
    from boss_engine.services.console_v2_workforce import workforce
    from boss_engine.services.specpack import SpecPackService

    from apps.api.app import main as api_main

    with engine.begin() as c:
        # Work attributed to software_engineer (llm_telemetry.STAGE_ROLES: implement).
        c.execute(text("INSERT INTO llm_calls (id, project_id, stage, model) "
                       "VALUES (:id, :p, 'implement', 'stub-1')"),
                  {"id": f"llm_{uuid4().hex[:16]}", "p": old_project})
    result = api_main._install_shipped_cast()[old_project]
    assert "software_engineer" in result["installed"]
    row = _roles(engine, old_project)["software_engineer"]
    assert row["spec"]["persona"]["callsign"] == _yaml_persona("software_engineer")["callsign"]
    assert row["born_from"]["pack_id"] == "software_factory_pack"

    specpack = SpecPackService(_settings())
    with engine.begin() as c:
        feed = workforce(c, old_project)
    unregistered = set(feed["unregistered"]) | {
        a["id"] for a in (feed.get("roster") or []) if not a.get("registered", True)}
    shipped_unregistered = []
    for name in sorted(unregistered):
        key = resolve(name)
        try:
            if key and specpack.load_role_spec(key):
                shipped_unregistered.append(name)
        except Exception:  # noqa: BLE001 — not a shipped role
            pass
    assert shipped_unregistered == [], shipped_unregistered


def test_a_shipped_role_from_outside_the_cast_packs_gets_its_persona(engine, old_project):
    """A legacy video_editor row in a core_v1 project with no media_pack install — not in any
    cast pack — still gets the persona its YAML ships."""
    from boss_engine.services.specpack import SpecPackService

    from apps.api.app import main as api_main

    if _settings().core_pack_id != "core_v1":
        pytest.skip("the case is a core_v1 project")
    legacy = SpecPackService(_settings()).load_role_spec("video_editor")
    legacy.pop("persona")
    with engine.begin() as c:
        assert not c.execute(text("SELECT 1 FROM pack_installs WHERE project_id = :p "
                                  "AND pack_id = 'media_pack'"), {"p": old_project}).first()
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                       "VALUES (:id, :p, 'video_editor', 'Video Editor', CAST(:s AS JSONB))"),
                  {"id": f"role_{uuid4().hex[:16]}", "p": old_project, "s": json.dumps(legacy)})
    result = api_main._install_shipped_cast()[old_project]
    assert "video_editor" in result["filled"]
    spec = _roles(engine, old_project)["video_editor"]["spec"]
    assert spec["persona"]["callsign"] == "Splice"
    assert spec["persona"]["tagline"] == _yaml_persona("video_editor")["tagline"]
    assert spec["mission"] == legacy["mission"]


def test_register_from_the_roster_installs_the_shipped_agent(client, engine):
    """The roster's Register goes through POST /v1/agents (agent-quick-create). With
    born_from {kind: shipped} on a key BOSS ships, it registers the shipped agent — persona,
    remit, grants, born from its pack — not a blank operator row the seed then never fills."""
    from boss_engine.services import seed_exclusions

    _forget(engine, "video_editor")
    made = client.post("/v1/agents", json={"project_id": PROJECT, "actor": ACTOR,
                                           "name": "Video Editor",
                                           "born_from": {"kind": "shipped"}})
    assert made.status_code == 201, made.text
    row = _roles(engine, PROJECT)["video_editor"]
    assert row["spec"]["persona"]["callsign"] == "Splice"
    assert row["spec"]["mission"] == SpecPackServiceSpec("video_editor")["mission"]
    assert row["spec"].get("source") != "operator"
    assert AGENT_ID.match(row["agent_id"] or "")
    assert row["born_from"]["kind"] == "pack" and row["born_from"]["pack_id"]
    with engine.connect() as c:
        assert not seed_exclusions.is_excluded(c, PROJECT, "role_spec", "video_editor")
        skills = set(c.execute(text("SELECT skill_id FROM role_skills WHERE project_id = :p "
                                    "AND role_key = 'video_editor'"), {"p": PROJECT}).scalars())
    from boss_engine.services.agent_baseline import BASELINE_SKILLS

    # Its pack's skills, and the baseline every agent holds on top (agent_baseline).
    wanted = {s for s in SpecPackServiceSpec("video_editor")["permissions"]["allowed_skills"]
              if "*" not in s} | set(BASELINE_SKILLS)
    assert skills and skills <= wanted
    assert set(BASELINE_SKILLS) <= skills


def test_register_with_something_written_is_the_operators_agent(client, engine):
    _forget(engine, "video_editor")
    made = client.post("/v1/agents", json={"project_id": PROJECT, "actor": ACTOR,
                                           "name": "Video Editor", "purpose": "Ours.",
                                           "born_from": {"kind": "blank"}})
    assert made.status_code == 201, made.text
    row = _roles(engine, PROJECT)["video_editor"]
    assert row["spec"]["mission"] == "Ours." and row["spec"]["source"] == "operator"
    _forget(engine, "video_editor")


def SpecPackServiceSpec(key: str) -> dict:  # noqa: N802 — reads as the thing it returns
    from boss_engine.services.specpack import SpecPackService

    return SpecPackService(_settings()).load_role_spec(key)


def test_register_on_a_shipped_role_installs_its_definition(client, engine):
    """The roster's Register action on a key BOSS ships registers the shipped agent — persona,
    remit and holdings — not an empty row."""
    _forget(engine, "video_editor")
    made = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": "video_editor"})
    assert made.status_code == 201, made.text
    roles = _roles(engine, PROJECT)
    row = roles["video_editor"]
    assert row["spec"]["persona"]["callsign"] == "Splice"
    assert row["spec"]["mission"] and AGENT_ID.match(row["agent_id"] or "")
    assert row["born_from"] == {"kind": "pack"}
