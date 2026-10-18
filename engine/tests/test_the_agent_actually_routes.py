"""The agent routes — and since 2026-10-18 a MODEL decides what the turn does (agent_decision).

Proven by the mechanism, not by an outcome that could pass either way.

I inverted the router so the agent decides instead of a keyword matcher, gated it on
`agent_router_llm_enabled` — which defaults to False and is unset in production — and shipped
dead code. My verification passed anyway: the sentences I tried contained no build verbs, so the
matcher abstained and the turn reached the agent for the OLD reason. "The regex had no opinion"
is not "the agent is routing", and the next message containing one ("list the tools you can call
that CHANGE something") spawned a governed code task.

So these tests assert that `route_turn` REACHES `_answer` for a message the deterministic
matcher has a strong opinion about. A test that only checks the reply cannot tell the two paths
apart — which is exactly how the first one passed.
"""

import pytest

from ._decision_stub import decided
from boss_engine.config import Settings
from boss_engine.services.intent_router import IntentRouter, RouterTurn


class _Capability:
    enabled = True
    reason = None


class _Manifest:
    def resolve(self, *_a, **_k):
        return None

    def enabled(self, *_a, **_k):
        return True

    def disabled(self, *_a, **_k):
        return []

    def get(self, *_a, **_k):
        return _Capability()


#: Every env var that could make Settings look configured when the test says it is not.
#: Without clearing these, Settings picks up the developer's real .env — the first version of
#: this file asserted "no model configured" and failed by printing a live Azure key into the
#: test output. A test that reads ambient credentials is both wrong and unsafe.
_MODEL_ENV = (
    "OPENAI_API_KEY", "AZURE_OPENAI_KEY", "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT", "BOSS_AGENT_ROUTER_LLM_ENABLED",
    # claude_cli has no API key but IS a real model backend, so config marks the credential
    # present ("claude-cli-proxy"). Leaving it set makes "no model configured" unreachable.
    "BOSS_LLM_ENGINE",
)


@pytest.fixture(autouse=True)
def _no_ambient_model(monkeypatch):
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)


def _router(**overrides) -> IntentRouter:
    key = overrides.pop("openai_api_key", "sk-test")
    settings = Settings(
        _env_file=None,                       # do not read the repo's .env
        DATABASE_URL="postgresql://x/y",
        **({"OPENAI_API_KEY": key} if key else {}),
        **overrides,
    )
    return IntentRouter(settings)


def _route(router, message: str, calls: list, action: str = "operate"):
    decided(router, action)
    router._answer = lambda *_a, **_k: (calls.append(message), RouterTurn(reply="answered"))[1]
    return router.route_turn(
        None, project_id="p", actor="ceo",
        messages=[{"role": "user", "content": message}], manifest=_Manifest(),
    )


@pytest.mark.parametrize("message", [
    "list the tools you can call that CHANGE something",   # the one that regressed
    "fix the deployment",
    "build a settings page",
    "add a currency picker",
])
def test_every_message_reaches_the_agent_when_a_model_exists(message):
    """Including ones the keyword matcher feels strongly about. The agent holds those same
    capabilities and can choose between them with the run in front of it."""
    calls: list = []
    turn = _route(_router(), message, calls)
    assert calls == [message], (
        f"{message!r} never reached the agent — the deterministic matcher actuated it instead")
    assert turn.reply == "answered"


def test_a_phased_command_stays_governed_when_a_model_exists():
    """A configured model must not swallow a phased command into an unbudgeted agent turn.

    "First research, then implement" is propose_command. Discovery, meetings, PRDs,
    and phased commands stay on the governed path so the reservation still happens.
    """
    message = "First research auth providers and then implement the login page"
    answers: list = []
    dispatched: list = []
    router = _router()
    router._answer = lambda *_a, **_k: (answers.append(message), RouterTurn(reply="answered"))[1]

    def _dispatch(_connection, name, _arguments, **_k):
        dispatched.append(name)
        return RouterTurn(reply="governed")

    router._dispatch = _dispatch
    model = decided(router, "propose_command")
    turn = router.route_turn(
        None, project_id="p", actor="ceo",
        messages=[{"role": "user", "content": message}], manifest=_Manifest(),
    )
    assert answers == [], "a phased command must not reach the agent"
    assert dispatched == ["propose_command"]
    assert turn.reply == "governed"
    assert turn.decision["action"] == "propose_command"
    # The keyword reading reached the model as a hint, never as the decision.
    assert "keyword reading" in model.prompts[0]


def test_it_is_not_gated_on_the_classifier_flag():
    """The bug: gated on agent_router_llm_enabled, which is False by default and unset in
    production, so the inversion never ran. That flag governs the older `_model_intent`
    classifier, not the tool-calling agent."""
    calls: list = []
    router = _router(BOSS_AGENT_ROUTER_LLM_ENABLED=False)
    assert router.settings.agent_router_llm_enabled is False
    _route(router, "build a settings page", calls)
    assert calls, "the agent must route regardless of the classifier flag"


def test_azure_alone_is_enough_of_a_model():
    calls: list = []
    router = _router(openai_api_key=None, AZURE_OPENAI_KEY="k",
                     AZURE_OPENAI_ENDPOINT="https://x", AZURE_OPENAI_DEPLOYMENT="d")
    _route(router, "build a settings page", calls)
    assert calls


def test_openai_api_key_is_the_canonical_model_signal():
    """Config sets it to the sentinel "claude-cli-proxy" when the engine is claude_cli, with
    the comment "has no API key but IS a real model backend". So gating on it means "a model
    backend exists", which is exactly the condition the agent needs — including on a deployment
    that runs the Claude CLI and has no OpenAI key at all."""
    router = _router(openai_api_key=None, BOSS_LLM_ENGINE="claude_cli")
    assert router.settings.openai_api_key == "claude-cli-proxy"
    calls: list = []
    _route(router, "build a settings page", calls)
    assert calls, "a claude_cli deployment must still route through the agent"


def test_without_any_model_nothing_is_started_and_boss_says_why(monkeypatch):
    """No model, no keyword fallback: "build a settings page" starts nothing, and the reply
    says BOSS needs a connected model (operator, 2026-10-18)."""
    from boss_engine.services import agent_decision

    router = _router(openai_api_key=None, BOSS_LLM_ENGINE="none")
    monkeypatch.setattr(router, "_a_model_can_answer", lambda *_a, **_k: False)
    started: list = []
    router._dispatch = lambda *a, **k: started.append(a)
    router._answer = lambda *a, **k: started.append(a)
    turn = router.route_turn(None, project_id="p", actor="ceo",
                             messages=[{"role": "user", "content": "build a settings page"}],
                             manifest=_Manifest())
    assert started == []
    assert turn.reply == agent_decision.NO_MODEL_REPLY
    assert turn.actions == []


def test_an_unsure_decision_asks_one_question_and_does_nothing():
    router = _router()
    decided(router, "new_code_change", confidence=0.4,
            question="Do you want a new build, or the Build step of wfr_x retried?")
    started: list = []
    router._dispatch = lambda *a, **k: started.append(a)
    router._answer = lambda *a, **k: started.append(a)
    turn = router.route_turn(None, project_id="p", actor="ceo",
                             messages=[{"role": "user", "content": "build it again"}],
                             manifest=_Manifest())
    assert started == []
    assert turn.reply == "Do you want a new build, or the Build step of wfr_x retried?"
    assert turn.decision["status"] == "clarify"


def test_a_question_never_authorises_a_write_and_an_instruction_does():
    seen: list = []
    for action, writes in (("answer", False), ("investigate", False), ("operate", True),
                           ("new_code_change", True)):
        router = _router()
        decided(router, action)
        router._answer = lambda *a, **k: (seen.append(k.get("writes")), RouterTurn(reply="ok"))[1]
        router.route_turn(None, project_id="p", actor="ceo",
                          messages=[{"role": "user", "content": "the build step failed"}],
                          manifest=_Manifest())
        assert seen[-1] is writes, action
