"""Every role BOSS ships is somebody: a callsign, a tagline and a persona that pass the lint.

boss-1's roster showed COO, CTO, Metatron, Video Editor and a dozen more with no callsign and no
voice: their YAMLs predated the Agent World and only the new cast had personas. Enumerated from
the files, so a role added later without a persona fails here, not on someone's screen.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boss_engine.services.agent_persona import lint_persona

SPECPACK = Path(__file__).resolve().parents[2] / "packages" / "specpack"
ROLES = sorted((SPECPACK / "roles").glob("*.yaml"))


def _persona(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("persona") or {}


def test_the_roles_are_enumerated():
    assert len(ROLES) >= 34


@pytest.mark.parametrize("path", ROLES, ids=lambda p: p.stem)
def test_each_role_has_a_callsign_tagline_and_persona_the_lint_accepts(path):
    persona = _persona(path)
    for field in ("callsign", "tagline", "about_me"):
        assert str(persona.get(field) or "").strip(), f"{path.stem} has no {field}"
    assert persona.get("voice") and persona.get("never"), f"{path.stem} has no voice or never"
    assert (persona.get("avatar") or {}).get("seed") == path.stem
    errors, warnings = lint_persona(persona)
    assert errors == [] and warnings == [], f"{path.stem}: {errors or warnings}"


def test_callsigns_are_distinct():
    seen: dict[str, str] = {}
    for path in ROLES:
        callsign = str(_persona(path).get("callsign") or "").lower()
        assert callsign not in seen, f"{path.stem} and {seen[callsign]} are both {callsign!r}"
        seen[callsign] = path.stem


def _leading_clause(tagline: str) -> str:
    """'Who owns it, and is it moving?' -> 'who owns it'."""
    import re

    return re.split(r"[,.?!;:]", tagline.strip().lower(), maxsplit=1)[0].strip()


def test_taglines_are_distinct_down_to_their_leading_clause():
    """Each role sounds like itself on the roster. COO "Who owns it, and is it moving?",
    Captain "Who owns it, and what's blocking it?" and Program Manager "Owner, date, blocker."
    said one thing three times."""
    seen: dict[str, str] = {}
    for path in ROLES:
        clause = _leading_clause(str(_persona(path).get("tagline") or ""))
        assert clause not in seen, f"{path.stem} and {seen[clause]} both lead with {clause!r}"
        seen[clause] = path.stem


def test_the_coordinators_each_have_their_own_beat():
    """COO: priorities across teams. Captain: the build crew. Program Manager: the dates."""
    tag = {k: str(_persona(SPECPACK / "roles" / f"{k}.yaml")["tagline"]).lower()
           for k in ("coo", "engineering_captain", "program_manager")}
    assert "team" in tag["coo"]
    assert "slice" in tag["engineering_captain"] or "engineer" in tag["engineering_captain"]
    assert "date" in tag["program_manager"]
    assert not any("owns" in t or "owner" in t for t in tag.values())


def test_every_role_a_pack_installs_ships_with_a_persona():
    keys = {p.stem for p in ROLES}
    for pack in sorted((SPECPACK / "packs").glob("*.yaml")):
        roles = ((yaml.safe_load(pack.read_text(encoding="utf-8")) or {}).get("installs")
                 or {}).get("roles") or []
        missing = sorted(set(roles) - keys)
        assert not missing, f"{pack.stem} installs {missing}, which have no role file"


def test_the_two_architects_have_distinct_remits():
    """chief_architect (Atlas) shapes one change; cao (Keystone) guards the platform
    invariants every change leans on. Both are referenced by work, so both stay."""
    atlas = yaml.safe_load((SPECPACK / "roles" / "chief_architect.yaml").read_text())
    keystone = yaml.safe_load((SPECPACK / "roles" / "cao.yaml").read_text())
    assert atlas["mission"] != keystone["mission"]
    assert "invariant" in keystone["persona"]["tagline"].lower()
    assert "trade-off" in atlas["persona"]["tagline"].lower()
