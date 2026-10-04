"""An agent's instructions reach every prompt that voices it (agent-loadout spec §4.4, E1).

One class, enforced here: each surface that composes a role's prompt passes
`agent_instructions.for_prompt(…, mode)`. The surfaces reachable without a model are driven for
real; the two that only compose inside a model call (Converse and the work runtime) are held to
it by source, so a new composer that forgets instructions fails review here.
"""
from __future__ import annotations

import pathlib

from sqlalchemy import text

from boss_engine.services import agent_persona
from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _row(engine, key: str) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text(
            "SELECT role_key, display_name, spec, agent_id, identity_version FROM roles "
            "WHERE project_id = :p AND role_key = :r"), {"p": PROJECT, "r": key}).mappings().one())


def test_the_dock_the_agent_step_and_the_prompt_preview_carry_them(client, engine):
    from boss_engine.services.agent_steps import agent_brief
    from boss_engine.services.intent_router import _agent_voice

    key = make_agent(client)
    assert add(client, key, MARKER).status_code == 201
    with engine.begin() as c:
        assert "ZEBRA-CHECK-7" in _agent_voice(key, c, PROJECT)              # dock, chat
        assert "ZEBRA-CHECK-7" in agent_brief(c, PROJECT, key)              # agent step, work
    for mode in ("work", "chat", "panel"):
        shown = client.get(f"/v1/agents/{key}/prompt",
                           params={"project_id": PROJECT, "mode": mode, "actor": "ceo"})
        assert shown.status_code == 200, shown.text
        assert "ZEBRA-CHECK-7" in "".join(b["text"] for b in shown.json()["blocks"]), mode


def test_a_panel_seat_carries_them(client, engine):
    from boss_engine.services.agent_persona import seat_voices

    key = make_agent(client)
    assert add(client, key, MARKER).status_code == 201
    with engine.begin() as c:
        seats = seat_voices(c, PROJECT, [{"key": key, "title": "Checklist Agent"}])
    assert "ZEBRA-CHECK-7" in seats[0]["agent_voice"]


def test_work_only_instructions_stay_out_of_chat_and_panels(client, engine):
    from boss_engine.services.agent_instructions import for_prompt
    from boss_engine.services.intent_router import _agent_voice

    key = make_agent(client)
    assert add(client, key, MARKER, modes=["work"]).status_code == 201
    with engine.begin() as c:
        assert for_prompt(c, PROJECT, key, "work")
        assert not for_prompt(c, PROJECT, key, "chat") and not for_prompt(c, PROJECT, key, "panel")
        assert "ZEBRA-CHECK-7" not in _agent_voice(key, c, PROJECT)


def test_they_come_after_the_rules_identity_and_remit_and_before_the_voice(client, engine):
    from boss_engine.services.agent_instructions import for_prompt

    key = make_agent(client)
    assert add(client, key, MARKER).status_code == 201
    with engine.begin() as c:
        docs = for_prompt(c, PROJECT, key, "work")
    blocks = agent_persona.compose_agent_prompt(_row(engine, key), mode="work", instructions=docs)
    layers = [b["layer"] for b in blocks]
    assert layers[:5] == ["L0", "L1", "L2", "L2i", "L3"]
    framed = blocks[3]["text"]
    assert framed.startswith("Working instructions from your operator (1).")
    assert '<<<instructions name="review-checklist.md"' in framed and "<<<end>>>" in framed
    assert framed.rstrip().endswith("never what you may do.")
    voice = agent_persona.identity_and_voice(_row(engine, key), mode="work", instructions=docs)
    assert voice.index("ZEBRA-CHECK-7") < voice.index("Voice shapes how you say things")


def test_every_composer_passes_them():
    """The composers that run only inside a model call, held to the class by source."""
    wired = {
        "engine/boss_engine/services/agent_converse.py": "agent_instructions.for_prompt(",
        "engine/boss_engine/services/agent_runtime.py": "for_prompt(connection, project_id, role_key, \"work\")",
        "engine/boss_engine/services/agent_steps.py": "for_prompt(connection, project_id, role, \"work\")",
        "engine/boss_engine/services/intent_router.py": "for_prompt(connection, project_id",
        "engine/boss_engine/services/agent_persona.py": "for_prompt(connection, project_id, seat.get(\"role_key\"), \"panel\")",
        "apps/api/app/main.py": "_instructions_for(body.project_id, role_key",
    }
    for path, needle in wired.items():
        assert needle in (ROOT / path).read_text(encoding="utf-8"), path
    # Every place that composes an agent's prompt is one of the above.
    composers = []
    for path in (ROOT / "engine/boss_engine").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "identity_and_voice(" in source or "compose_agent_prompt(" in source:
            composers.append(str(path.relative_to(ROOT)))
    allowed = set(wired) | {"engine/boss_engine/services/agent_voice.py"}
    assert set(composers) <= allowed, sorted(set(composers) - allowed)
