"""The seed is additive only: it never puts back what an operator removed.

THE INCIDENT. Every install, upgrade, `make seed` and POST /v1/bootstrap/seed ran a converging
seed. `sync_role_skills` re-granted every skill a role spec names, so a skill an operator took
off an agent came back on the next deploy (boss-1: 48 `skill.assigned_to_agent` events for 16
grants). `ensure_mcp_server` re-registered every built-in MCP server by name, so deleting one
lasted until the next restart. The role installer rewrote any bundled role whose spec differed
from the pack's YAML — which is every role an operator had edited.

THE RULE IS ENFORCED BY ENUMERATION, not by remembering. This file finds every function in the
engine and the API that writes one of the tables a seed fills, and every call into those writers,
and checks each against a reviewed list: an OPERATOR path (a person's decision, which may lift an
exclusion) or a SEED path (which must ask services/seed_exclusions before it writes). A new
writer or a new caller fails here until somebody decides which it is. The behaviour itself — a
full re-seed after real removals — is exercised against Postgres in
apps/api/tests/test_one_agent_model.py.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from boss_engine.services import agent_studio, mcp_registry, seed_exclusions, skill_definitions
from boss_engine.services import specpack

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [ROOT / "engine" / "boss_engine", ROOT / "apps" / "api" / "app", ROOT / "apps" / "worker"]

#: The tables a seed fills and an operator can take things out of.
SEEDED_TABLES = ("role_skills", "mcp_servers", "roles", "role_mcp_servers")

#: Every function that INSERTs into one of them, and whose decision it carries out.
WRITERS = {
    "services/skill_definitions.py::assign_skill_to_role": "operator",
    "services/mcp_registry.py::register_mcp_server": "operator",
    "services/specpack.py::register_role": "operator",
    "services/agents.py::create_agent": "operator",
    "services/agents.py::grant_mcp": "operator",
    "services/specpack.py::_upsert_role_spec": "seed",
}

#: Every place one of the writers above is called from, and why that is safe.
CALLERS = {
    "bootstrap.py::_seed_project_defaults -> ensure_mcp_server": "seed path below",
    "services/mcp_registry.py::ensure_mcp_server -> register_mcp_server": "seed path below",
    "services/skill_definitions.py::sync_role_skills -> assign_skill_to_role": "seed path below",
    "services/specpack.py::* -> _upsert_role_spec": "the writer itself asks seed_exclusions",
    "services/agent_studio.py::_seed_agent -> _upsert_role_spec": "seed path below",
    "services/agent_instructions.py::skill_from_file -> assign_skill_to_role":
        "operator: a person imports a guide and explicitly grants it in the same transaction",
    "services/skill_packaging.py::import_mcp -> ensure_mcp_server":
        "an operator's import; lifts the exclusion first",
    "services/agents.py::* -> assign_skill_to_role": "operator: the agent resource",
    "services/agents.py::* -> grant_mcp": "operator: the agent resource",
    # A person's press of "Load the NorthPeak Bank data platform" (or the AML sample): registers
    # the sample's connectors with respect_operator, and grants them to the stages' agents only
    # where no grant exists and the agent was not removed.
    "services/knowledge/dq_platform.py::load_platform -> ensure_mcp_server":
        "operator: a person loads the DQ sample (respect_operator)",
    "services/knowledge/dq_platform.py::_install_workflows -> grant_mcp":
        "operator: a person loads the DQ sample; never re-grants a removed one",
    "services/knowledge/aml_sample.py::load_sample -> ensure_mcp_server":
        "operator: a person loads the AML sample (respect_operator)",
    "tools/agent_tools.py::* -> assign_skill_to_role": "operator: assign_skill_to_agent",
    "tools/agent_tools.py::* -> grant_mcp": "operator: assign_mcp_to_agent",
    "main.py::* -> register_mcp_server": "operator: POST /v1/mcp/servers",
    # Integration of the agents and skills-mcp streams: skills-mcp's chat tool connects a server
    # on an operator's request. It lands switched off and unreviewed, and register_mcp_server
    # lifts any earlier deletion because a person asked for it again.
    "tools/builtin.py::_register_write_tools -> register_mcp_server":
        "operator: the register_mcp_server chat tool",
    "main.py::* -> assign_skill_to_role": "operator: POST /v1/role-skills",
    "main.py::* -> register_role": "operator: POST /v1/roles",
    "routers/agents.py::* -> create_agent": "operator: POST /v1/agents",
    "routers/agents.py::* -> grant_mcp": "operator: POST /v1/role-mcp-servers",
    # Agent World: role specs name the MCP servers their agent holds; the seed grants them the
    # way sync_role_skills grants skills, and asks seed_exclusions first (SEED_PATHS below).
    "services/agents.py::sync_role_mcp -> grant_mcp": "seed path below",
    # The upgrade's half of the install seed: every boot, every project (shipped_cast).
    "services/shipped_cast.py::sync_project -> _upsert_role_spec":
        "seed path below (install_or_fill is its nested helper)",
    "services/shipped_cast.py::install_or_fill -> _upsert_role_spec":
        "seed path below; and a role the operator deleted is never reinstalled",
    # The role agents a catalogue workflow's steps are (workflow_catalogue.roles_needed),
    # installed with the workflow: only a MISSING role; _upsert_role_spec asks seed_exclusions,
    # so one the operator deleted is never reinstalled.
    "services/shipped_cast.py::install_roles -> _upsert_role_spec":
        "seed path; a role the operator deleted is never reinstalled",
    # Register on the roster, for a key BOSS ships: the operator asked for the shipped agent.
    "services/agents.py::_install_shipped -> _upsert_role_spec":
        "operator: Register lifts the key's exclusions, then installs the shipped role",
    "services/shipped_cast.py::_ensure_builtin_mcp_servers -> ensure_mcp_server":
        "seed path below; only a missing server, never one the operator removed",
    # Skills & MCP's Kit (skills-mcp spec §6.3): giving an ability is a person's press — PUT
    # holders, Apply recommended — and lifts what it grants; the seed's half (abilities a role
    # names) is _grant_quietly, below, which asks seed_exclusions and never lifts one.
    "services/kit.py::_grant -> assign_skill_to_role": "operator: PUT /v1/kit/{ability}/holders",
    "services/kit.py::_grant -> grant_mcp": "operator: PUT /v1/kit/{ability}/holders",
    "services/kit.py::_revoke -> grant_mcp":
        "operator: taking an ability off narrows the server's tools to what the agent still needs",
    "services/kit.py::_grant_quietly -> assign_skill_to_role": "seed path below",
    "services/kit.py::_grant_quietly -> grant_mcp": "seed path below",
    "services/kit_connections.py::add_catalogue_server -> register_mcp_server":
        "operator: Add from the MCP catalogue (or Library docs' Save & test)",
}

#: The seed paths, each of which must consult the exclusions before it writes.
SEED_PATHS = [
    skill_definitions.sync_role_skills,
    __import__("boss_engine.services.agents", fromlist=["agents"]).sync_role_mcp,
    mcp_registry.ensure_mcp_server,
    specpack.SpecPackService._upsert_role_spec,
    agent_studio.AgentStudioService._seed_agent,
    __import__("boss_engine.services.shipped_cast", fromlist=["x"]).sync_project,
    __import__("boss_engine.services.shipped_cast", fromlist=["x"])._ensure_builtin_mcp_servers,
    __import__("boss_engine.services.kit", fromlist=["x"]).sync_role_abilities,
    __import__("boss_engine.services.kit", fromlist=["x"])._grant_quietly,
]


def _python_files():
    for base in SCANNED:
        for path in base.rglob("*.py"):
            if "/tests/" in str(path) or "__pycache__" in str(path):
                continue
            yield path


def _short(path: Path) -> str:
    text_path = str(path)
    for base in SCANNED:
        if text_path.startswith(str(base)):
            return text_path[len(str(base)) + 1:]
    return text_path


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_every_writer_of_a_seeded_table_is_reviewed():
    found: set[str] = set()
    insert = re.compile(r"INSERT\s+INTO\s+(" + "|".join(SEEDED_TABLES) + r")\b", re.I)
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        if not insert.search(source):
            continue
        tree = ast.parse(source)
        for fn in _functions(tree):
            body = ast.get_source_segment(source, fn) or ""
            # The innermost function owns the INSERT; an enclosing one only contains it.
            inner = [n for n in _functions(fn) if n is not fn]
            for nested in inner:
                body = body.replace(ast.get_source_segment(source, nested) or "", "")
            if insert.search(body):
                found.add(f"{_short(path)}::{fn.name}")
    assert found == set(WRITERS), (
        f"new writers: {sorted(found - set(WRITERS))}; gone: {sorted(set(WRITERS) - found)}. "
        "Decide whether each is an operator's decision or a seed, and list it in WRITERS.")


def test_every_call_into_a_writer_is_reviewed():
    writer_names = {key.split("::")[1] for key in WRITERS} | {"ensure_mcp_server"}
    calls: set[str] = set()
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for fn in _functions(tree):
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", ""))
                # `create_agent` is also Agent Studio's (an agent_definitions row, not a role);
                # only the agent resource's writes a seeded table.
                if name == "create_agent" and not (
                        isinstance(node.func, ast.Attribute)
                        and getattr(node.func.value, "id", "") == "agents"):
                    continue
                if name in writer_names and name != fn.name:
                    calls.add((_short(path), fn.name, name))

    def reviewed(path: str, caller: str, callee: str) -> bool:
        return (f"{path}::{caller} -> {callee}" in CALLERS
                or f"{path}::* -> {callee}" in CALLERS)

    unreviewed = sorted(f"{p}::{c} -> {w}" for p, c, w in calls if not reviewed(p, c, w))
    assert not unreviewed, (
        f"unreviewed calls into a seeded-table writer: {unreviewed}. A seed path must consult "
        "services/seed_exclusions; an operator path is listed in CALLERS with its reason.")


def test_every_seed_path_asks_what_an_operator_removed():
    for fn in SEED_PATHS:
        source = inspect.getsource(fn)
        assert "seed_exclusions" in source or "is_excluded" in source, (
            f"{fn.__qualname__} writes during a seed without asking seed_exclusions, so it "
            "would put back what an operator removed")


def test_every_operator_removal_is_remembered():
    """The other half: a removal that records nothing cannot be honoured by the seed."""
    assert "seed_exclusions.exclude(" in inspect.getsource(
        skill_definitions.unassign_skill_from_role)
    assert "seed_exclusions.exclude(" in inspect.getsource(mcp_registry.delete_mcp_server)
    from boss_engine.services import agents

    assert 'exclude(connection, project_id, "role"' in inspect.getsource(agents.delete_agent)
    assert '"role_spec"' in inspect.getsource(agents.update_agent)


#: Every function that writes a role's SPEC or NAME (an INSERT INTO roles, or an UPDATE roles
#: that sets spec or display_name), and whose decision it carries out.
SPEC_WRITERS = {
    "services/specpack.py::register_role": "operator",
    "services/specpack.py::_upsert_role_spec": "seed",
    "services/agents.py::create_agent": "operator",
    "services/agents.py::update_agent": "operator",
    # Fills only ABSENT persona/MCP fields, and asks whether the operator edited the role.
    "services/shipped_cast.py::install_or_fill": "seed",
}


def test_every_write_of_a_role_spec_is_remembered_or_asks():
    """An edit the seed does not know about is an edit the seed reverts.

    THE INCIDENT (review of milestone/agents, round 1). The WRITERS list above enumerated
    INSERTs only, so it never saw `UPDATE roles SET spec = …`. POST /v1/roles could rewrite a
    bundled role's spec and record nothing, and the next seed put the pack's YAML back. Creating
    an agent under a deleted bundled key lifted the delete but recorded no edit, so the seed
    overwrote the operator's new agent with the pack's mission and grants.

    Every spec writer is found here by its SQL and must be either an operator path that records
    a `role_spec` exclusion, or a seed path that asks seed_exclusions before it writes.
    """
    writes = re.compile(
        r"INSERT\s+INTO\s+roles\b|UPDATE\s+roles\s+SET\b(?:(?!\bWHERE\b)[\s\S])*?"
        r"\b(?:spec|display_name)\s*=", re.I)
    found: dict[str, str] = {}
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        if not writes.search(source):
            continue
        tree = ast.parse(source)
        for fn in _functions(tree):
            body = ast.get_source_segment(source, fn) or ""
            for nested in [n for n in _functions(fn) if n is not fn]:
                body = body.replace(ast.get_source_segment(source, nested) or "", "")
            if writes.search(body):
                found[f"{_short(path)}::{fn.name}"] = body
    assert set(found) == set(SPEC_WRITERS), (
        f"new spec writers: {sorted(set(found) - set(SPEC_WRITERS))}; gone: "
        f"{sorted(set(SPEC_WRITERS) - set(found))}. Decide whether each is an operator's "
        "decision (record a role_spec exclusion) or a seed (ask seed_exclusions).")
    for name, kind in SPEC_WRITERS.items():
        if kind == "operator":
            assert '"role_spec"' in found[name], (
                f"{name} writes a role's spec for an operator and records no role_spec "
                "exclusion, so the next seed reverts it")
        else:
            assert "seed_exclusions" in found[name], f"{name} seeds without asking"


def test_the_kinds_are_the_ones_the_schema_allows():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS seed_exclusions"):]
    block = block[:block.index(");")]
    for kind in seed_exclusions.KINDS:
        assert f"'{kind}'" in block
