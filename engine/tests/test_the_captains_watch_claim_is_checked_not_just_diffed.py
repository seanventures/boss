"""P1: the Engineering Captain's role prompt must describe `watch` correctly, and the claim
it makes must independently hold — not just be a line of new YAML text.

The Captain told the operator "I cannot watch this in the background" and then armed a
monitor anyway (wake_26186a08c079429e9e6d). The fix is one sentence in its role's
`boundaries.must` stating `watch` works; this proves the sentence is present AND that the
Captain's own permissions actually let it reach `watch` (permission `autopilot:operate`,
not in `forbidden_skills`).
"""
from __future__ import annotations

from boss_engine.config import get_settings
from boss_engine.services.specpack import SpecPackService
from boss_engine.tools import builtin


def _watch_tool():
    return builtin.build_default_registry().get("watch")


def test_the_role_prompt_names_watch_and_the_role_can_actually_reach_it():
    role = SpecPackService(get_settings()).load_role_spec("engineering_captain")

    must = " ".join(role.get("boundaries", {}).get("must") or [])
    assert "watch" in must.lower(), (
        "boundaries.must should tell the Captain what `watch` does and that it works now")

    watch_tool = _watch_tool()
    forbidden = (role.get("permissions") or {}).get("forbidden_skills") or []
    assert watch_tool.permission not in forbidden, (
        f"the role forbids {watch_tool.permission!r}, so the new sentence would be a lie: "
        f"the Captain could not actually call watch")
