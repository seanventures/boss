"""Every skill a Kit ability names can run in Community (skills-mcp RC4b, E12, test 4b).

THE GAP. About half the Community skills had no executor, and the bundles the engineering
roles named were outside the Community catalogue — so "File tickets", "Team chat" and "Tests &
CI" were dead even after a grant. An ability is shown Ready only if it can actually run, so
every skill it names must be (a) in the Community catalogue and (b) backed by an executor.
Tool rows a kit lists but never grants (Merge a PR, Off by default) are not held, so they are
held to (a) only.
"""
from __future__ import annotations

import pathlib

import yaml

from boss_engine.services.skill_executors import registered_executor_ids

ROOT = pathlib.Path(__file__).resolve().parents[2] / "packages" / "specpack"


def _community_skills() -> set[str]:
    pack = yaml.safe_load((ROOT / "packs" / "community_edition.yaml").read_text())
    bundles = set(pack["installs"]["skills"])
    out: set[str] = set()
    for path in (ROOT / "skills").glob("*.yaml"):
        if path.stem in bundles:
            for skill in (yaml.safe_load(path.read_text()) or {}).get("skills") or []:
                out.add(str(skill["id"]))
    return out


def _abilities():
    for path in sorted((ROOT / "kits").glob("*.yaml")):
        if path.stem in ("mcp_catalogue", "aliases"):
            continue
        for ability in (yaml.safe_load(path.read_text()) or {}).get("abilities") or []:
            yield ability


def test_every_granted_kit_skill_is_in_community_and_runs():
    community = _community_skills()
    executors = set(registered_executor_ids())
    problems = []
    for ability in _abilities():
        for skill in ability.get("skills") or []:
            if skill not in community:
                problems.append(f"{ability['key']}: {skill} is not in the Community catalogue")
            if skill not in executors:
                problems.append(f"{ability['key']}: {skill} has no executor")
    assert not problems, problems


def test_every_listed_tool_skill_ships_in_community():
    community = _community_skills()
    for ability in _abilities():
        for tool in ability.get("tools") or []:
            ref = str(tool.get("ref") or "")
            if ref.startswith("skill:"):
                assert ref[6:] in community, f"{ability['key']} lists {ref}"


def test_every_kit_mcp_server_is_seeded_or_in_the_catalogue():
    from boss_engine.bootstrap import BUILTIN_MCP_SERVERS

    seeded = {s["name"] for s in BUILTIN_MCP_SERVERS}
    catalogue = {e["name"] for e in yaml.safe_load(
        (ROOT / "kits" / "mcp_catalogue.yaml").read_text())["catalogue"]}
    for ability in _abilities():
        for entry in ability.get("mcp") or []:
            assert entry["server"] in seeded | catalogue, (ability["key"], entry)


def test_essentials_are_abilities():
    main = yaml.safe_load((ROOT / "kits" / "software_factory.yaml").read_text())
    keys = {a["key"] for a in main["abilities"]}
    assert set(main["essentials"]) <= keys
    assert main["essentials"] == ["open_prs", "web_research", "file_tickets", "email"]


def test_a_guide_skill_has_an_executor_and_takes_no_config():
    """agent-loadout §4.6: a `guide` (a SKILL.md playbook) runs through `skill.guide`, and a
    config — anything that could make it DO something — is refused."""
    import pytest

    from boss_engine.services.skill_definitions import (BEHAVIOURS, SkillDefinitionError,
                                                        validate_config)

    assert "guide" in BEHAVIOURS
    assert "skill.guide" in set(registered_executor_ids())
    assert validate_config("guide", {}, []) == {}
    with pytest.raises(SkillDefinitionError):
        validate_config("guide", {"x": 1}, [])
