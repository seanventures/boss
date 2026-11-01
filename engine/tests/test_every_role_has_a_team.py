"""Every shipped agent sits in one team, and one resolver says which (agents-home spec §4.6).

Agents › Live's Agent force, Organization › Zones, Directory's sections and the Skills "Who can"
matrix all group agents by team. There was no team field anywhere — the Skills spec assumed
groups with no source — so each surface would have kept its own list. `team:` in the role YAML
is the one source, `agent_identity.team_of` the one reader.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from boss_engine.services import agent_identity
from boss_engine.services.shipped_cast import fill_missing

ROLES = Path(__file__).resolve().parents[2] / "packages/specpack/roles"
ARCHETYPES = {"first_principles_researcher", "relentless_operator", "teacher_who_simplifies",
              "visionary_founder"}


def _shipped() -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(ROLES.glob("*.yaml"))}


def test_every_shipped_role_names_a_shipped_team():
    roles = _shipped()
    assert len(roles) == 44  # + archivist (BOSSAPP-622), + incident_commander, + compliance_officer,
    # + the KG pack role agents (compliance_analyst, pipeline_orchestrator, lane_agent,
    # remediation_advisor, aml_network_mapper, aml_analyst, sar_narrator)
    shipped_teams = set(agent_identity.TEAM_KEYS) - {"yours"}
    missing = {k: r.get("team") for k, r in roles.items() if r.get("team") not in shipped_teams}
    assert not missing, f"roles with no shipped team: {missing}"
    # Every shipped team has somebody in it.
    assert {r["team"] for r in roles.values()} == shipped_teams


def test_the_archetypes_are_thinkers_and_say_they_are_not_people():
    roles = _shipped()
    thinkers = {k for k, r in roles.items() if r["team"] == "thinkers"}
    assert thinkers == ARCHETYPES
    team = agent_identity.team_of(roles["visionary_founder"])
    assert team == {"key": "thinkers", "label": "Thinkers",
                    "tip": "Inspired by a way of thinking. Not real people."}


def test_an_agent_with_no_team_is_yours_and_a_typed_team_is_its_own():
    assert agent_identity.team_of(None)["key"] == "yours"
    assert agent_identity.team_of({"mission": "x"})["key"] == "yours"
    assert agent_identity.team_of({"spec": {"team": "quality"}})["label"] == "Quality"
    assert agent_identity.team_of({"team": "Growth Squad"}) == {"key": "growth_squad",
                                                                 "label": "Growth Squad"}


def test_the_team_order_is_the_one_every_surface_draws():
    assert agent_identity.TEAM_KEYS == ("leadership", "architecture", "engineering", "quality",
                                        "operations", "cost", "media", "thinkers", "yours")


def test_an_installed_role_gets_its_shipped_team_and_a_chosen_team_is_kept():
    shipped = {"team": "quality", "persona": {"callsign": "Vera"}}
    # An operator-edited role with no team still gets one: a team is not a remit or a voice.
    assert fill_missing({"mission": "edited"}, shipped, operator_edited=True)["team"] == "quality"
    # A team somebody chose is never changed.
    assert fill_missing({"team": "media"}, shipped, operator_edited=False)["team"] == "media"


def test_moving_an_agent_between_teams_is_not_a_new_version():
    row = {"role_key": "qa", "agent_id": "agt_0123456789", "display_name": "QA",
           "spec": {"mission": "m", "team": "quality"}, "reports_to": None,
           "autonomy_level": 1, "lifecycle": "active", "born_by": "spec-pack",
           "born_from": {}, "sponsor": None}
    moved = {**row, "spec": {"mission": "m", "team": "media"}}
    assert agent_identity.fingerprint_of(agent_identity.canonical(row, [], {})) == \
        agent_identity.fingerprint_of(agent_identity.canonical(moved, [], {}))
