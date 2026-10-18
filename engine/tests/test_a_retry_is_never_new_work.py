"""Asking BOSS to retry a step is never a request for new work (D2, crun_61720b80).

On the laptop the chat model is Anthropic. `model_configured` checked OpenAI/Azure keys only,
so the keyword layer routed every turn: "Please retry the Build step of the Order API run
(wfr_…)" matched "build" and became a new governed code task titled with the sentence.
"""
from types import SimpleNamespace

from boss_engine.services import intent_router as ir


def _router():
    r = ir.IntentRouter.__new__(ir.IntentRouter)
    r.settings = SimpleNamespace(openai_api_key="", azure_openai_key="")
    r._answer_adapter = None
    return r


def test_the_keyword_layer_never_turns_a_retry_into_a_build():
    r = _router()
    for msg in ("Please retry the Build step of the Order API run (wfr_4ff9551e906643079cfb).",
                "Consent given. Please retry the Build step of the run now.",
                "re-run the build step of crun_abc123def",
                "update the step that failed and retry it"):
        name, _ = r._deterministic_intent(msg)
        assert name != "create_code_task", msg
    assert r._deterministic_intent("build a shift swap board for nurses")[0] == "create_code_task"


def test_any_connected_model_routes_the_turn(monkeypatch):
    from boss_engine.services import model_routing

    r = _router()

    class Conn:
        def begin_nested(self):
            return SimpleNamespace(rollback=lambda: None)

    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *a, **k: object())
    assert r._a_model_can_answer(Conn(), "p") is True

    def none(*a, **k):
        raise RuntimeError("no model connected")

    monkeypatch.setattr(model_routing, "project_chat_adapter", none)
    assert r._a_model_can_answer(Conn(), "p") is False
