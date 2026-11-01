"""The KG workflows' agent steps name the role agents whose work they are, the project gets those
agents when it gets the workflow, and the consent at start asks for exactly them.

The defect (northpeak-lending, 2026-10-31): Investigate on an AML alert asked "Let Security
Reviewer and Documentation Writer act for you?" — but the AML steps are the Network Mapper's (Mesh),
the AML Analyst's (Sift) and the SAR Narrator's (Scribe). The catalogue steps named nobody, the
project had no `roles` rows for the pack roles, and the consent fell back to the capabilities'
captains. Same for banking_regulatory_compliance (Tally, Switch, Plumb, Mend).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.identity import consent
from boss_engine.services import step_agents
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services.workflow_capabilities import CAPABILITIES

ROLES_DIR = Path(__file__).resolve().parents[2] / "packages" / "specpack" / "roles"

#: step id -> the role agent that does it (and nothing else in either workflow is an agent's).
EXPECTED = {
    "aml_alert_investigation": {
        "network_mapping": "aml_network_mapper",
        "aml_analysis": "aml_analyst",
        "narrative": "sar_narrator",
    },
    "banking_regulatory_compliance": {
        "compliance_assessment": "compliance_analyst",
        "compliance_orchestration": "pipeline_orchestrator",
        "lane_confirmation": "lane_agent",
        "remediation_package": "remediation_advisor",
    },
}


def _entry(key: str) -> dict:
    return next(e for e in catalogue.CATALOGUE if e["key"] == key)


def _named(entry: dict) -> dict[str, str]:
    return {s["id"]: step_agents.named_agent(s) for s in entry["steps"]
            if step_agents.named_agent(s)}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_each_agent_step_names_its_role_agent_and_nothing_else_names_one(key):
    entry = _entry(key)
    named = _named(entry)
    # Gates are a person's; every other named step is exactly the role agent its capability says.
    named = {sid: role for sid, role in named.items() if role != "human"}
    assert named == EXPECTED[key]
    for step in entry["steps"]:
        cap = CAPABILITIES.get(str((step.get("binding") or {}).get("capability") or ""))
        if cap is not None and cap.role_agent:
            # One source: the capability's role_agent, as a SOFT default (an operator may change it).
            assert step["captain"] == cap.role_agent == step["default_captain"], step["id"]
    assert catalogue.roles_needed(entry) == list(dict.fromkeys(EXPECTED[key].values()))


def test_every_role_agent_is_a_shipped_role_with_a_callsign():
    roles = {r for e in catalogue.CATALOGUE for r in catalogue.roles_needed(e)}
    assert roles >= {r for m in EXPECTED.values() for r in m.values()}
    for role in roles:
        spec = yaml.safe_load((ROLES_DIR / f"{role}.yaml").read_text(encoding="utf-8"))
        assert spec["role_id"] == role and spec["persona"]["callsign"], role


# ── against Postgres ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url:
        pytest.skip("DATABASE_URL must be set to a reachable postgres for these tests")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    from boss_engine.db import ensure_schema

    ensure_schema(engine)
    return engine


@pytest.fixture()
def loaded(pg_engine, monkeypatch):
    """A project with the AML sample loaded (banking-aml includes banking-regulatory, so both
    workflows install), rolled back afterwards."""
    from boss_engine.services import mcp_registry
    from boss_engine.services.knowledge import aml_sample, ontology_packs
    from tools import mcp_aml_mock

    monkeypatch.setattr(mcp_registry, "call_tool_unattended",
                        lambda settings, server, tool, args:
                        mcp_aml_mock.TOOL_FNS[server["name"]][tool](args))
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_kgr_{suffix}", f"ws_kgr_{suffix}", f"p_kgr_{suffix}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'KG')"), {"i": org})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'KG')"),
                         {"i": ws, "o": org})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                              "(:i, :w, 'NorthPeak', :s)"), {"i": project, "w": ws, "s": project})
            result = aml_sample.load_sample(conn, project_id=project, actor="ceo")
            assert result["ok"], result
            yield conn, project
        finally:
            transaction.rollback()
            ontology_packs._PROJECT_PACK_CACHE.pop(project, None)


def _roles(c, project) -> set[str]:
    return {str(r) for r in c.execute(text("SELECT role_key FROM roles WHERE project_id = :p"),
                                      {"p": project}).scalars()}


def _published(c, project, key) -> dict:
    spec = c.execute(text(
        "SELECT v.spec FROM workflow_definitions d JOIN workflow_versions v "
        "ON v.id = d.active_version_id WHERE d.project_id = :p AND d.workflow_key = :k"),
        {"p": project, "k": key}).scalar()
    assert isinstance(spec, dict), f"{key} is not installed"
    return spec


def test_getting_the_workflows_installs_their_role_agents_and_publishes_them_named(loaded):
    c, project = loaded
    needed = {r for m in EXPECTED.values() for r in m.values()}
    assert needed <= _roles(c, project)
    for key, expected in EXPECTED.items():
        named = {s["id"]: step_agents.named_agent(s) for s in _published(c, project, key)["steps"]
                 if step_agents.named_agent(s) and step_agents.named_agent(s) != "human"}
        assert named == expected, key
    # Born as the cast is: an identity (agent id) for each, from its pack.
    ids = dict(c.execute(text("SELECT role_key, agent_id FROM roles WHERE project_id = :p "
                              "AND role_key = ANY(:k)"), {"p": project, "k": sorted(needed)}).all())
    assert all(ids.get(r) for r in needed), ids


def test_a_project_seeded_before_the_roles_existed_is_backfilled_on_the_next_seed(loaded):
    c, project = loaded
    c.execute(text("DELETE FROM roles WHERE project_id = :p AND role_key IN "
                   "('aml_analyst', 'lane_agent')"), {"p": project})
    first = catalogue.restaff_role_agent_entries(c, project, actor="boss")
    assert first is not None
    assert {"aml_analyst", "lane_agent"} <= _roles(c, project)
    # Idempotent: a second pass installs nothing and publishes nothing new.
    before = c.execute(text("SELECT count(*) FROM roles WHERE project_id = :p"), {"p": project}).scalar()
    versions = c.execute(text("SELECT count(*) FROM workflow_versions WHERE project_id = :p"),
                         {"p": project}).scalar()
    again = catalogue.restaff_role_agent_entries(c, project, actor="boss")
    assert [i["version_id"] for i in again["installed"]] == \
        [i["version_id"] for i in first["installed"]], again
    assert c.execute(text("SELECT count(*) FROM workflow_versions WHERE project_id = :p"),
                     {"p": project}).scalar() == versions
    assert c.execute(text("SELECT count(*) FROM roles WHERE project_id = :p"),
                     {"p": project}).scalar() == before


def test_an_agent_the_operator_deleted_is_not_reinstalled(loaded):
    from boss_engine.services import seed_exclusions

    c, project = loaded
    c.execute(text("DELETE FROM roles WHERE project_id = :p AND role_key = 'sar_narrator'"),
              {"p": project})
    seed_exclusions.exclude(c, project, "role", "sar_narrator", actor="ceo")
    catalogue.restaff_role_agent_entries(c, project, actor="boss")
    assert "sar_narrator" not in _roles(c, project)
    # ... and its step is then published unnamed (Auto), not handed to somebody else.
    narrative = next(s for s in _published(c, project, "aml_alert_investigation")["steps"]
                     if s["id"] == "narrative")
    assert not step_agents.named_agent(narrative)


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_the_consent_to_start_asks_for_exactly_the_role_agents(loaded, key):
    """What the dialog lists is workflow_needs' agents: exactly the steps' role agents — no
    Security Reviewer, Documentation Writer or Chief Architect for steps that are BOSS's own code
    or a person's decision."""
    c, project = loaded
    roles, _perms, _repos = consent.workflow_needs(c, project, _published(c, project, key), {})
    assert roles == list(dict.fromkeys(EXPECTED[key].values()))
