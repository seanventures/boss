"""The agent routes. Proven by the mechanism, not by an outcome that could pass either way.

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

from boss_engine.config import Settings
from boss_engine.services.intent_router import IntentRouter, RouterTurn


class _Manifest:
    def resolve(self, *_a, **_k):
        return None

    def enabled(self, *_a, **_k):
        return True

    def disabled(self, *_a, **_k):
        return []


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


def _route(router, message: str, calls: list):
    router._answer = lambda *_a, **_k: (calls.append(message), RouterTurn(reply="answered"))[1]
    return router.route_turn(
        None, project_id="p", actor="ceo",
        messages=[{"role": "user", "content": message}], manifest=_Manifest(),
    )


@pytest.mark.parametrize("message", [
    "list the tools you can call that CHANGE something",   # the one that regressed
    "fix the deployment",
    "build a settings page",
    "First research auth providers and then implement the login page",
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


def test_without_any_model_the_keyword_fallback_still_works():
    """A deployment with no model backend at all must still be able to start work, or the
    fallback is not a fallback."""
    router = _router(openai_api_key=None, BOSS_LLM_ENGINE="none")
    assert not (router.settings.openai_api_key or router.settings.azure_openai_key), (
        "the fixture must clear ambient credentials, or this asserts nothing")
    name, _args = router._deterministic_intent("build a settings page")
    assert name == "create_code_task"
