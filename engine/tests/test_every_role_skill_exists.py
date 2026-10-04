"""Every skill a role names exists (skills-mcp RC4, E5, test 4).

THE GAP. 46 ids in role specs' `allowed_skills` named no shipped skill (code.review, ci.status,
test.run, decision.create…). sync_role_skills reported them unavailable and granted nothing,
silently, while the Passport showed the role "holding" them. Each id now resolves to a shipped
skill or, through kits/aliases.yaml, to one; ids that meant nothing runnable were removed.
"""
from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2] / "packages" / "specpack"


def _skills() -> set[str]:
    out: set[str] = set()
    for path in (ROOT / "skills").glob("*.yaml"):
        for skill in (yaml.safe_load(path.read_text()) or {}).get("skills") or []:
            out.add(str(skill["id"]))
    return out


def _roles():
    for path in sorted((ROOT / "roles").glob("*.yaml")):
        yield path.stem, (yaml.safe_load(path.read_text()) or {}).get("permissions") or {}


def test_every_allowed_skill_resolves():
    skills = _skills()
    aliases = (yaml.safe_load((ROOT / "kits" / "aliases.yaml").read_text()) or {})["aliases"]
    for alias, target in aliases.items():
        assert target in skills, f"alias {alias} -> {target}, which no skill spec ships"
    missing = [(role, skill) for role, permissions in _roles()
               for skill in permissions.get("allowed_skills") or []
               if "*" not in skill and skill not in skills and skill not in aliases]
    assert not missing, f"role skills that resolve to nothing: {missing}"


def test_every_role_mcp_server_is_seeded():
    from boss_engine.bootstrap import BUILTIN_MCP_SERVERS

    seeded = {s["name"] for s in BUILTIN_MCP_SERVERS}
    wrong = [(role, entry.get("server")) for role, permissions in _roles()
             for entry in permissions.get("mcp_servers") or []
             if entry.get("server") not in seeded]
    assert not wrong, wrong


def test_every_ability_a_role_names_is_in_a_kit():
    keys = set()
    for path in (ROOT / "kits").glob("*.yaml"):
        for ability in (yaml.safe_load(path.read_text()) or {}).get("abilities") or []:
            keys.add(ability["key"])
    wrong = [(role, key) for role, permissions in _roles()
             for key in (permissions.get("abilities") or [])
             + (permissions.get("recommended_abilities") or []) if key not in keys]
    assert not wrong, wrong
