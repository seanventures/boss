"""THE CAPTAIN IS RETIRED AS AN ENGINE; ITS POWERS ARE THE BOSS AGENT'S ACTIONS (BOSSAPP-759).

Operator, 2026-10-23: "Why do we even have the captain autopilot? Retire it. Put its
functionality into the BOSS agent. There must be ONE agent pipeline, and all agents go through
it." The captain's watch, goals, drive, overwatch and theater are registry tools the one agent
chooses (tools/captain_tools.py); a typed /watch, /overwatch, /drive is a press of that tool
through the agent's own press path (`agent_action`); the worker's scheduled tick only watches."""
from __future__ import annotations

import ast
import inspect
import textwrap

from apps.api.app import main as api_main
from apps.api.app.routers import metatron_captain as captain_router
from boss_engine.tools import build_discuss_read_registry
from boss_engine.tools.captain_tools import CAPTAIN_TOOLS

APPLYING = {"captain_drive", "captain_overwatch", "captain_theater"}


def test_the_captains_powers_are_tools_of_the_one_agent():
    chat = build_discuss_read_registry()
    for name in CAPTAIN_TOOLS:
        spec = chat.get(name)
        assert spec.permission and spec.permission.startswith("autopilot:"), name
        # The agent may observe on its own; anything that APPLIES moves is offered, never
        # pressed by the model — plain text cannot set off an overwatch or a factory build.
        assert spec.self_executable is (name not in APPLYING), name


def test_the_model_cannot_press_an_applying_captain_tool(client):
    from boss_engine.context import EngineContext

    chat = build_discuss_read_registry()
    ctx = EngineContext(settings=api_main.settings, engine=api_main.engine)
    for name in sorted(APPLYING):
        refused = chat.invoke(name, ctx, project_id="boss-dogfood", actor="ceo", args={})
        assert refused["status"] == "confirmation_required", name
        assert "propose_action" in refused["do_this"], name


def _body_calls(fn) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))
    names: set[str] = set()
    for node in (n for stmt in tree.body[0].body for n in ast.walk(stmt)):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return names


def test_every_typed_captain_command_is_a_press_of_the_agents_action():
    for route in (captain_router.captain_watch, captain_router.captain_drive,
                  captain_router.captain_overwatch, captain_router.captain_theater_enact,
                  captain_router.captain_goals_create):
        calls = _body_calls(route)
        assert "_press" in calls, route.__name__
        assert not calls & {"watch_once", "drive", "overwatch_once", "command_once",
                            "set_goal"}, (route.__name__, calls)
    assert "agent_action" in _body_calls(captain_router._press)


def test_overwatch_runs_when_the_person_presses_the_agents_offer(client):
    """The console's button (and Metatron's numbered choice) on a captain_overwatch offer."""
    thread = client.post("/v1/agent/threads/ensure",
                         json={"project_id": "boss-dogfood", "actor": "ceo"})
    thread_id = thread.json().get("thread_id") if thread.status_code == 200 else None
    pressed = client.post("/v1/engine/agent/action", json={
        "project_id": "boss-dogfood", "actor": "ceo", "tool": "captain_overwatch", "args": {},
        "thread_id": thread_id, "label": "Run overwatch"})
    assert pressed.status_code == 200, pressed.text
    body = pressed.json()
    assert body["ok"] is True and body["tool"] == "captain_overwatch"
    assert "verify" in body["result"]
    assert (body["result"].get("validation") or {}).get("ok") is True


def test_the_scheduled_watch_observes_and_applies_nothing(client, monkeypatch):
    """The worker's tick posts /v1/metatron/watch: the agent's captain_watch, through the press
    path. The autopilot that applied moves on a timer is gone."""
    from boss_engine.services.metatron_captain import MetatronCaptain

    def refuse(*_a, **_k):
        raise AssertionError("a scheduled watch applied a move")

    for name in ("_apply_validated_move", "overwatch_once", "command_once"):
        monkeypatch.setattr(MetatronCaptain, name, refuse)
    tick = client.post("/v1/metatron/watch", json={"project_id": "boss-dogfood",
                                                   "actor": "system"})
    assert tick.status_code == 200, tick.text
    assert tick.json()["id"].startswith("mwt_")

    from apps.worker.boss_worker import control_plane

    assert not hasattr(control_plane.ControlPlaneClient, "tick_metatron_overwatch")
    source = inspect.getsource(__import__("apps.worker.boss_worker.main", fromlist=["x"]))
    assert "tick_metatron_overwatch" not in source
