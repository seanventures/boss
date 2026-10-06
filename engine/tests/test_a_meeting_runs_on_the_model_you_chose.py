""""the meeting it started finished as 'cancelled' — HTTPStatusError: 500 … /v1/meetings/step"

Under the 500:

    File "meeting_service.py", line 354, in _ask
    File "openai_adapter.py", line 331, in create_text_response
    raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

On an install with two connected subscriptions and `chat_llm_engine` set to `codex` — in the
box labelled "Console chat, agents and meetings", whose help text promises it "runs the agent
panels and speaks in meetings".

The line that picks the brain was already right: `make_agent_chat_adapter(settings)`, with a
comment describing this exact failure being fixed once before. It failed anyway, because
`settings` there is the DEPLOYMENT's. The project's choice lives in `project_settings` and
nothing layered it onto this path — the same gap the console chat had, in the third place.

And routing it at a subscription would then have raised AttributeError, because the wrapper
that provides the fallback implemented `create_chat_completion` and not
`create_text_response` — which is the only method a meeting calls.
"""
from __future__ import annotations

import inspect

from boss_engine.services import meeting_service
from boss_engine.services.claude_cli_adapter import FallingBackAdapter


def test_the_panel_uses_the_engine_this_project_chose():
    """Through the one router (model_routing), not a pasted copy of the resolution."""
    source = inspect.getsource(meeting_service.advance_meeting_run)
    assert 'model_routing.project_chat_adapter(c, settings, run["project_id"])' in source
    assert "chat_engine_for" not in source.split("from . import model_routing")[1]


def test_and_a_settings_lookup_cannot_end_a_meeting():
    """A panel that dies because a knob could not be read is worse than one that runs on the
    deployment default. Behaviour: the router's choice survives a connection that cannot
    answer a single query."""
    from boss_engine.config import Settings
    from boss_engine.services import model_routing

    class _Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("settings table unreachable")

    choice = model_routing.choose(_Broken(), Settings().model_copy(update={
        "openai_api_key": "sk-test-not-real", "chat_llm_engine": "", "llm_engine": "auto"}),
        "p")
    assert choice.engine == "native" and choice.why == model_routing.ENVIRONMENT


def test_the_fallback_wrapper_implements_what_a_meeting_calls():
    """A wrapper that implements half the interface it wraps is a fallback that works only for
    the callers somebody tested."""
    assert hasattr(FallingBackAdapter, "create_text_response")
    assert hasattr(FallingBackAdapter, "create_chat_completion")


def test_and_on_the_same_terms_as_the_chat_one():
    """Infrastructure failure falls back; a model failure is an answer and belongs to the
    caller's retry. Two methods with two different rules would be worse than one."""
    text_source = inspect.getsource(FallingBackAdapter.create_text_response)
    chat_source = inspect.getsource(FallingBackAdapter.create_chat_completion)
    for fragment in ("if not self._unavailable(exc):", "raise",
                     "self.last_backend = type(self.primary).__name__",
                     "self.last_backend = type(self.fallback).__name__"):
        assert fragment in text_source, fragment
        assert fragment in chat_source, fragment


def test_both_cli_brains_can_actually_answer_a_panel():
    """The panel's only call. Inherited from CliChatBrain, so a new CLI adapter gets it too."""
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
    from boss_engine.services.codex_cli_adapter import CodexCliAdapter

    for adapter in (ClaudeCliAdapter, CodexCliAdapter):
        assert hasattr(adapter, "create_text_response"), adapter.__name__


def test_the_setting_that_promises_meetings_is_the_one_being_read():
    """`chat_llm_engine` is labelled "Console chat, agents and meetings". If that text is going
    to say meetings, this is the path that has to honour it."""
    from boss_engine.services.project_settings import KNOBS

    assert "meetings" in KNOBS["chat_llm_engine"]["label"].lower()
    assert "meetings" in KNOBS["chat_llm_engine"]["help"].lower()


# -- a fallback that cannot work is not a fallback ---------------------------------------------

def test_no_fallback_is_attached_when_it_could_not_answer():
    """"exited 1" is in the unavailable list, so ANY hiccup from the CLI — codex printed
    `failed to refresh available models` and exited 1 — sent the call to the OpenAI adapter,
    which on an install with no key raises "OPENAI_API_KEY is not configured".

    That is what the operator saw, twice, about a subscription that was connected and working,
    while the real reason sat in a log nobody reads. Wrapping in a backend that cannot answer
    does not add resilience; it replaces an actionable error with an unactionable one.
    """
    from boss_engine.services.claude_cli_adapter import (
        ClaudeCliAdapter,
        FallingBackAdapter,
        make_agent_chat_adapter,
    )

    class _NoKey:
        chat_llm_engine = "claude_code"
        llm_engine = "auto"
        openai_api_key = ""
        azure_openai_key = ""
        claude_cli_model = None
        claude_cli_path = "claude"
        repo_root = __import__("pathlib").Path("/tmp")

    assert isinstance(make_agent_chat_adapter(_NoKey()), ClaudeCliAdapter)

    class _WithKey(_NoKey):
        openai_api_key = "sk-x"

    assert isinstance(make_agent_chat_adapter(_WithKey()), FallingBackAdapter)


def test_and_azure_counts_as_a_usable_fallback():
    """The OpenAI adapter answers on either credential; checking only one would drop the
    fallback for an Azure install that has a working one."""
    from boss_engine.services.claude_cli_adapter import openai_fallback_usable

    class _Azure:
        openai_api_key = ""
        azure_openai_key = "az-x"

    assert openai_fallback_usable(_Azure()) is True


def test_codex_gets_the_same_treatment():
    import inspect

    from boss_engine.services import codex_cli_adapter

    source = inspect.getsource(codex_cli_adapter.make_codex_chat_adapter)
    assert "openai_fallback_usable(settings)" in source
    assert "return codex" in source


def test_and_a_failed_fallback_reports_the_primary_failure():
    """The first failure is the real one. Reporting the second hides a CLI that said exactly
    what was wrong behind a backend the operator never chose."""
    import inspect

    from boss_engine.services.claude_cli_adapter import FallingBackAdapter

    for method in (FallingBackAdapter.create_chat_completion,
                   FallingBackAdapter.create_text_response):
        source = inspect.getsource(method)
        assert "and the fallback failed too" in source
        assert "from primary_error" in source
