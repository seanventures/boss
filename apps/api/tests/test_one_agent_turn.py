"""ONE AGENT TURN (BOSSAPP-759): Metatron, BOSS's terminal, talks to the same agent as the
console's Ask BOSS, through one code path, so an agent fix is never made twice.

Operator, 2026-10-23: "metatron is just the CLI that interacts with the core BOSS agents… if two
have different code path for AI agents then we have to make duplicate fixes every time."

Before: a Metatron sentence went to MetatronCaptain.session_turn — its own keyword reader and
its own model decision — and asked to "set up three projects… tell me what you can't do" it ran
overwatch moves and proposed a factory job. Now POST /v1/metatron/session is a thin adapter onto
`agent_turn`, the function the console's chat routes call."""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from apps.api.app import main as api_main
from apps.api.app.routers import metatron_captain as captain_router
from boss_engine.services import agent_decision
from boss_engine.services import metatron_captain as captain_service
from boss_engine.services.intent_router import IntentRouter, RouterTurn

ROOT = Path(__file__).resolve().parents[3]


def _calls(fn) -> set[str]:
    """The names a function calls (`agent_turn`, `orchestration_agent.chat`, ...)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body  # the function's own statements, not its route decorators
    out: set[str] = set()
    for node in (n for stmt in body for n in ast.walk(stmt)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                base = func.value.id if isinstance(func.value, ast.Name) else ""
                out.add(f"{base}.{func.attr}" if base else func.attr)
    return out


def _undecorated(fn):
    return inspect.unwrap(fn)


def test_metatron_and_the_console_call_the_same_agent_turn():
    console = _calls(_undecorated(api_main.engine_agent_chat))
    alias = _calls(_undecorated(api_main.agent_chat))
    metatron = _calls(_undecorated(captain_router.captain_session_turn))
    assert "agent_turn" in console and "agent_turn" in alias
    assert "agent_turn" in metatron
    # The adapter is only that: nothing else that could decide, route or answer.
    assert metatron <= {"agent_turn"}, metatron
    # And the one turn is the one brain.
    assert "orchestration_agent.chat" in _calls(api_main.agent_turn)


#: What a second decision path looks like in source. None of it may appear in Metatron's router
#: or the captain's service: the BOSS agent (AgentService.chat → IntentRouter) decides turns.
_SECOND_PATH = {"agent_decision", "IntentRouter", "route_turn", "decide", "decide_free_text",
                "interpret_turn", "session_turn", "chat", "intent_router", "create_chat_completion",
                "create_text_response", "make_adapter", "project_chat_adapter"}


def _identifiers(source: str) -> set[str]:
    """Every name the CODE uses — imports, names, attributes, definitions. Docstrings and
    comments may say what was removed; only code counts."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            out.update((node.module or "").split("."))
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            out.update(part for a in node.names for part in a.name.split("."))
    return out


@pytest.mark.parametrize("module", [captain_router, captain_service])
def test_metatron_has_no_decision_path_of_its_own(module):
    used = _identifiers(Path(module.__file__).read_text())
    assert not used & _SECOND_PATH, (module.__name__, sorted(used & _SECOND_PATH))


@pytest.fixture()
def scripted(monkeypatch):
    """A scripted decision model (no paid call): records every decision it is asked for, and
    answers `action` for every message. The answer is a stub that echoes what it was given."""
    asked: list[dict] = []

    def decide(connection, settings, project_id, *, message, candidates, **kwargs):
        asked.append({"message": message, "candidates": sorted(c.key for c in candidates),
                      "situation": kwargs.get("situation"), "agent": kwargs.get("agent")})
        return agent_decision.AgentDecision(agent_decision.DECIDED, action="answer",
                                            confidence=0.95, by={"backend": "scripted"})

    monkeypatch.setattr(agent_decision, "decide", decide)
    monkeypatch.setattr(IntentRouter, "_a_model_can_answer", lambda *a, **k: True)

    def answer(self, connection, project_id, messages, **kwargs):
        said = messages[-1]["content"]
        return RouterTurn(reply=f"I cannot create projects from here: {said}",
                          proposed_actions=[{"id": "pa_x", "tool": "set_project_setting",
                                             "args": {"key": "k", "value": "v"},
                                             "label": "Set it", "why": "you asked"}])

    monkeypatch.setattr(IntentRouter, "_answer", answer)
    return asked


def _turn(client, path: str, message: str, actor: str):
    response = client.post(path, json={"project_id": "boss-dogfood", "actor": actor,
                                       "message": message, "surface": "chat"})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_same_input_gets_the_same_decision_in_metatron_and_the_console(client, scripted):
    said = "set up three projects for me and tell me what you can't do"
    console = _turn(client, "/v1/engine/agent/chat", said, "executive_console")
    terminal = _turn(client, "/v1/metatron/session", said, "ceo")
    assert len(scripted) == 2
    assert scripted[0] == scripted[1]
    for key in ("reply", "proposed_actions", "actions", "pending_confirmation", "next_steps"):
        assert console[key] == terminal[key], key
    # The same reply STRUCTURE, field for field: the terminal's --json is the console's API.
    assert set(console) == set(terminal)


def test_a_metatron_thread_is_an_ask_boss_thread(client, scripted):
    """Same actor, no thread id: a conversation started in the terminal continues in the
    console, and the console's list shows it."""
    first = _turn(client, "/v1/metatron/session", "hello from the terminal", "ceo")
    second = _turn(client, "/v1/engine/agent/chat", "and now from the console", "ceo")
    assert first["thread_id"] == second["thread_id"]
    assert [m["content"] for m in second["messages"] if m["role"] == "user"] == [
        "hello from the terminal", "and now from the console"]
    listed = client.get("/v1/agent/threads", params={"project_id": "boss-dogfood", "actor": "ceo"})
    assert listed.status_code == 200, listed.text
    assert first["thread_id"] in str(listed.json())


def test_plain_text_never_runs_overwatch_or_launches_a_factory_job(client, scripted, monkeypatch):
    """Whatever the words — "overwatch", "go", "drive auto", a setup request — a sentence is the
    agent's to answer. The captain's powers are not reachable from it."""
    def refuse(*_a, **_k):
        raise AssertionError("plain text reached a captain power")

    for name in ("overwatch_once", "drive", "watch_once", "command_once",
                 "_apply_validated_move"):
        monkeypatch.setattr(captain_service.MetatronCaptain, name, refuse)
    for said in ("overwatch", "go", "drive auto", "run overwatch now",
                 "set up three projects and tell me what you can't do",
                 "build a settings page"):
        body = _turn(client, "/v1/metatron/session", said, "ceo")
        assert body["reply"].startswith("I cannot create projects"), said
        assert not [a for a in body["actions"] if a.get("kind") in {
            "overwatch", "drive", "act", "theater"}], said


def test_overwatch_still_runs_as_a_typed_command(client):
    cycle = client.post("/v1/metatron/overwatch",
                        json={"project_id": "boss-dogfood", "actor": "ceo"})
    assert cycle.status_code == 200, cycle.text
    assert "verify" in cycle.json()


def test_the_terminal_docs_say_metatron_is_a_client_of_the_one_agent():
    doc = (ROOT / "docs" / "community" / "TERMINAL_API.md").read_text()
    assert "same BOSS agent" in doc
    assert "/v1/engine/agent/action" in doc
