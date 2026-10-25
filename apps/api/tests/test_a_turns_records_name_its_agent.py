"""WHAT A TURN RECORDS NAMES WHO SPOKE (BOSSAPP-768 project flow, found live in northpeak-bank).

Learnings recorded in a Metatron chat (root_cause, investigation, precedent) had EMPTY provenance
agent_name/agent_role, and `/agent site_reliability_engineer` was refused for the agent whose key
is `sre`. Now a chat turn addressed to an agent — by key, name or any spelling of its name — runs
as that agent, and every rule, learning and decision it records carries the agent's real name,
role and Passport id; a turn nobody was addressed in records "BOSS (general)", never a blank.
Real database; the router is stubbed only so no model is called (it records exactly as the
record_learning and kg_propose_rule tools do inside a turn).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import agent_identity
from boss_engine.services.agent_service import AgentService
from boss_engine.services.knowledge import learning_writeback, rules
from boss_engine.services.llm_telemetry import GENERAL_NAME, GENERAL_ROLE, get_llm_context


@pytest.fixture
def project(engine) -> str:
    pid = get_settings().default_project_id
    with engine.connect() as db:
        have = {r[0] for r in db.execute(text("SELECT role_key FROM roles WHERE project_id = :p"),
                                         {"p": pid})}
    if "sre" not in have:
        pytest.skip("the default project has no sre agent in this database")
    return pid


class _Recording:
    """A router whose turn records one learning and reads a rule's provenance, as the tools do."""

    def __init__(self):
        self.seen: list[dict] = []

    def route_turn(self, connection, *, project_id, role=None, **_):
        held = get_llm_context()
        item = learning_writeback.record(
            connection, project_id=project_id, learning_kind="investigation",
            title=f"Why the ledger stalled ({role or 'general'})",
            statement=f"The nightly ledger job stalls when the lock is held ({role or 'general'}).",
            detail={"tried": "re-ran it", "ruled_out": "the database", "found": "a held lock"},
            about={}, evidence=[{"type": "log", "ref": "chat", "excerpt": "lock wait timeout"}],
            confidence=0.5)
        actor_ref = held.get("speaker_role") or held.get("role_key") or "ceo"   # as kg_propose_rule
        self.seen.append({"role": role, "ctx": held, "learning": item["provenance"],
                          "rule": rules._provenance(connection, project_id, "agent", actor_ref)})
        return SimpleNamespace(reply="ok", tool_calls=[], actions=[], proposed_actions=[],
                               next_steps=[], pending_confirmation=None)


def _turn(engine, project, role):
    service = AgentService(get_settings())
    service.router = _Recording()
    with engine.begin() as db:
        service.chat(db, project_id=project, actor="ceo", message="why did the ledger stall?",
                     role=role, new_thread=True)
    return service.router.seen[-1]


def test_an_addressed_agent_is_named_on_what_its_turn_records(engine, project):
    with engine.connect() as db:
        sre = db.execute(text("SELECT display_name, agent_id FROM roles WHERE project_id = :p "
                              "AND role_key = 'sre'"), {"p": project}).mappings().one()
    for typed in ("site_reliability_engineer", "Site Reliability Engineer", "sre"):
        seen = _turn(engine, project, typed)
        assert seen["role"] == "sre", typed            # the router is handed the agent's key
        for prov in (seen["learning"], seen["rule"]):
            assert prov["agent_role"] == "sre", (typed, prov)
            assert prov["agent_name"] == sre["display_name"], (typed, prov)
            assert prov["agent_id"] == sre["agent_id"] and prov["agent_id"], (typed, prov)
    # The speaker is the turn's only: nothing leaks into the next call on this thread.
    assert "speaker_role" not in get_llm_context()


def test_a_turn_nobody_was_addressed_in_is_boss_itself_said_so(engine, project):
    seen = _turn(engine, project, None)
    for prov in (seen["learning"], seen["rule"]):
        assert (prov["agent_role"], prov["agent_name"]) == (GENERAL_ROLE, GENERAL_NAME), prov
        assert prov.get("agent_id") is None
    seen = _turn(engine, project, "nobody_like_this")
    assert seen["learning"]["agent_name"] == GENERAL_NAME


def test_any_spelling_of_an_agents_name_resolves_to_it(engine, project):
    with engine.connect() as db:
        for typed in ("site_reliability_engineer", "Site-Reliability-Engineer", "SITE RELIABILITY ENGINEER",
                      "Beacon", "sre"):
            assert agent_identity.resolve_in(db, project, typed) == "sre", typed
        who = agent_identity.addressed(db, project, "site_reliability_engineer")
        assert who["addressed"] is True and who["agent_role"] == "sre" and who["agent_id"]
        assert agent_identity.addressed(db, project, None)["agent_name"] == GENERAL_NAME


def test_the_incident_commander_ships_with_the_core_cast():
    """The banking project's cast (Chief Architect, Software Engineer, Site Reliability Engineer,
    Incident Commander) is BOSS's shipped cast: the core packs install it in every project."""
    from boss_engine.services.specpack import SpecPackService

    specpack = SpecPackService(get_settings())
    role = specpack.load_role_spec("incident_commander")
    assert role["display_name"] == "Incident Commander" and role["persona"]["callsign"] == "Anchor"
    for pack in ("core_v1", "core_v2"):
        assert "incident_commander" in specpack.load_pack_spec(pack)["installs"]["roles"], pack
