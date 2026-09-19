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
    source = inspect.getsource(meeting_service.advance_meeting_run)
    assert "chat_engine_for(c, settings, run[\"project_id\"])" in source
    assert 'settings.model_copy(update={"chat_llm_engine": chosen_engine})' in source


def test_and_a_settings_lookup_cannot_end_a_meeting():
    """A panel that dies because a knob could not be read is worse than one that runs on the
    deployment default."""
    source = inspect.getsource(meeting_service.advance_meeting_run)
    at = source.index("from .setup_service import chat_engine_for")
    # The guard wraps the whole lookup: import, query and model_copy.
    assert "except Exception" in source[at:at + 700]


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
