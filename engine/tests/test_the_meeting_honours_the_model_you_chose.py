"""The setting says "and meetings". The meeting was not reading it.

An operator set "Console chat, agents and meetings" to `codex` — the box whose help text
promises it "runs the agent panels and speaks in meetings" — and every panel died with
`OPENAI_API_KEY is not configured`.

    "why are you saying meeting is configured with OpenAI it should be codex right? see below
     your settings"

They were right. That setting is `chat_llm_engine`, and only `make_agent_chat_adapter` reads
it. `meeting_service` called `make_chat_adapter`, which reads `llm_engine` — a different knob,
defaulting to "auto", falling through to the OpenAI adapter. So the choice was never consulted.

The trap in the obvious remedy is why this matters: setting an OpenAI key WOULD have made the
meetings run, on the provider the operator had just chosen not to use, and nothing would ever
have said so.

The comment above `chat_llm_engine` describes this exact failure for the chat — "an operator
who sets one lock believes they have moved everything". It was true of meetings too.
"""
from __future__ import annotations

import inspect

from boss_engine.services import meeting_service
from boss_engine.services.project_settings import KNOBS


def test_the_setting_still_claims_to_cover_meetings():
    """If this promise is ever withdrawn, the test below stops being required."""
    spec = KNOBS["chat_llm_engine"]
    assert "meetings" in spec["label"].lower()
    assert "meetings" in spec["help"].lower()


def test_the_meeting_uses_the_adapter_that_reads_that_setting():
    source = inspect.getsource(meeting_service)
    # The project's routed model — the one router every model call uses (model_routing), which
    # reads chat_llm_engine through `choose`.
    assert 'model_routing.project_chat_adapter(c, settings, run["project_id"])' in source
    # And no longer the one that silently ignores it.
    assert "adapter = make_chat_adapter(settings)" not in source


def test_only_the_agent_adapter_reads_chat_llm_engine():
    """The thing that made this a trap: two near-identical factories, one knob each."""
    from boss_engine.services import claude_cli_adapter

    agent = inspect.getsource(claude_cli_adapter.make_agent_chat_adapter)
    plain = inspect.getsource(claude_cli_adapter.make_chat_adapter)
    assert "chat_llm_engine" in agent
    assert "chat_llm_engine" not in plain


def test_an_install_that_never_set_it_is_unaffected():
    """`make_agent_chat_adapter` inherits when the setting is empty, so this change cannot
    move an existing deployment onto a different provider."""
    from boss_engine.services import claude_cli_adapter

    agent = inspect.getsource(claude_cli_adapter.make_agent_chat_adapter)
    assert "if not engine:" in agent
    assert "return make_chat_adapter(settings)" in agent
