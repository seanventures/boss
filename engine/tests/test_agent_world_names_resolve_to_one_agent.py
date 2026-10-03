"""Every name BOSS already gives an agent lands on one Agent World role key.

A panel seat says "QA / Test", the captain roster says `ux_engineer`, the cost ledger says
`architect`, a build stage says `implement`. If any of those fails to map, that agent shows up
faceless on one screen and with a face on another, which is the drift Agent World removes.
This checks the frozen table against the live sources, so a new seat or stage that nobody
mapped fails here instead of on a screen.
"""
from __future__ import annotations

import pathlib

from boss_engine.services import agent_world_types as aw
from boss_engine.services import captain_coordination, llm_telemetry, meeting_service


def _lands(name: str) -> str | None:
    key = name.strip().lower()
    key = aw.ALIASES.get(key, key)
    return key if key in aw.CANONICAL_ROLE_KEYS else None


def test_every_panel_seat_key_and_title_lands_on_a_role():
    for key, seat in meeting_service.ROLE_CATALOG.items():
        assert _lands(key), key
        assert _lands(seat["title"]), seat["title"]


def test_every_captain_roster_id_lands_on_a_role():
    for entry in captain_coordination.ROSTER:
        assert _lands(entry["id"]), entry["id"]


def test_every_build_stage_and_ledger_alias_lands_on_a_role():
    for stage, role in llm_telemetry.STAGE_ROLES.items():
        assert _lands(role), (stage, role)
    for old in llm_telemetry.ROLE_ALIASES:
        assert _lands(old), old


def test_aliases_point_only_at_real_keys_and_never_chain():
    for alias, key in aw.ALIASES.items():
        assert key in aw.CANONICAL_ROLE_KEYS, (alias, key)
        assert alias == alias.lower().strip()


def test_every_shipped_role_yaml_is_a_canonical_key():
    roles = pathlib.Path("packages/specpack/roles")
    for path in roles.glob("*.yaml"):
        assert path.stem in aw.CANONICAL_ROLE_KEYS, path.stem


def test_room_presets_seat_two_to_five_real_agents():
    for preset in aw.ROOM_PRESETS:
        assert 2 <= len(preset["seats"]) <= 5, preset["key"]
        assert all(s in aw.CANONICAL_ROLE_KEYS for s in preset["seats"]), preset["key"]


def test_every_act_how_has_one_edge_label():
    hows = set(aw.ActHow.__args__)  # type: ignore[attr-defined]
    assert set(aw.EDGE_LABELS) == hows
