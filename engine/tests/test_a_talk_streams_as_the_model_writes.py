"""A talk shows an agent's words as the model writes them, on the adapters BOSS really uses.

The console streamed tokens and the route passed them through, but no adapter could stream:
`agent_converse._call` only streams when the adapter has `stream_text_response`, none did, so
every reply waited for the whole model call (20-60 s of thinking dots) and was then replayed in
about a second. These tests drive the adapters' own streaming paths: the Claude CLI's
`stream-json` output from a real child process that does not finish its reply until the test
has seen the first words, the OpenAI adapter's `stream=True` on both endpoints, and the
fallback and governed wrappers, which must pass a stream through rather than hide it.
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter, FallingBackAdapter
from boss_engine.services.model_routing import GovernedChatAdapter
from boss_engine.services.openai_adapter import OpenAIAdapter


# ── The Claude CLI: a real child process writing stream-json ─────────────────────────────────

FAKE_CLAUDE = r'''#!{python}
import json, os, sys, time
def out(event):
    sys.stdout.write(json.dumps(event) + "\n"); sys.stdout.flush()
def delta(text):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}}})
open(os.environ["FAKE_PID"], "w").write(str(os.getpid()))
out({"type": "system", "subtype": "init"})
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "error":
    out({"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": "usage limit reached"})
    sys.exit(1)
delta("First words. ")
# The rest of the reply is written only once the reader has shown the first words: a reader
# that waits for the whole call before showing anything would see "LATE" here.
marker = os.environ["FAKE_MARKER"]
for _ in range(100):
    if os.path.exists(marker):
        break
    time.sleep(0.05)
delta("Then the rest." if os.path.exists(marker) else "LATE")
if mode == "hang":
    time.sleep(30)
out({"type": "result", "subtype": "success", "is_error": False,
     "result": "First words. Then the rest.", "session_id": "s1",
     "usage": {"input_tokens": 11, "output_tokens": 7}, "modelUsage": {"claude-x": {}},
     "total_cost_usd": 0.001})
'''


@pytest.fixture()
def claude(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_PID", str(tmp_path / "pid"))
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "marker"))
    settings = SimpleNamespace(repo_root=tmp_path, claude_cli_timeout=20, claude_cli_model=None,
                               claude_cli_path=str(binary))
    adapter = ClaudeCliAdapter(settings)
    monkeypatch.setattr(adapter, "_child_env", lambda: dict(os.environ))
    return adapter, tmp_path


def test_the_claude_cli_yields_the_first_words_before_the_reply_is_finished(claude):
    adapter, tmp = claude
    pieces = []
    for piece in adapter.stream_text_response("Hi", system_prompt="You are Vera."):
        if isinstance(piece, str) and not pieces:
            (tmp / "marker").write_text("seen")
        pieces.append(piece)
    assert pieces[:2] == ["First words. ", "Then the rest."]
    usage = pieces[-1]
    assert isinstance(usage, dict)
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["model"]) == (
        11, 7, "claude-x")


def test_the_claude_cli_stream_raises_the_cli_s_own_error(claude, monkeypatch):
    adapter, _ = claude
    monkeypatch.setenv("FAKE_MODE", "error")
    with pytest.raises(RuntimeError, match="usage limit reached"):
        list(adapter.stream_text_response("Hi"))


def test_stopping_a_stream_ends_the_cli_process(claude, monkeypatch):
    adapter, tmp = claude
    monkeypatch.setenv("FAKE_MODE", "hang")
    stream = adapter.stream_text_response("Hi")
    assert next(stream) == "First words. "
    pid = int((tmp / "pid").read_text())
    stream.close()  # the person pressed Stop
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the CLI kept running after its stream was closed")


# ── The OpenAI adapter: stream=True on both endpoints ────────────────────────────────────────

def _settings(**over):
    base = dict(azure_openai_key=None, azure_openai_endpoint=None, azure_openai_deployment=None,
                openai_api_key="sk-test", openai_base_url="", openai_model="gpt-4o")
    base.update(over)
    return SimpleNamespace(**base)


def test_openai_compatible_endpoint_streams_chat_completions(monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)

        def chunks():
            for word in ("Show ", "me ", "the test."):
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=word))])
            yield SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=9, completion_tokens=3, total_tokens=12))
        return chunks()

    adapter = OpenAIAdapter(_settings(openai_base_url="http://compatible.test/v1"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(adapter, "_openai_client", lambda: client)
    pieces = list(adapter.stream_text_response("Hi", system_prompt="You are Vera."))
    assert pieces[:3] == ["Show ", "me ", "the test."]
    assert seen["stream"] is True and seen["messages"][0] == {"role": "system",
                                                              "content": "You are Vera."}
    assert pieces[-1]["completion_tokens"] == 3 and pieces[-1]["prompt_tokens"] == 9


def test_openai_responses_api_streams_text_deltas(monkeypatch):
    def create(**kwargs):
        assert kwargs["stream"] is True

        def events():
            yield SimpleNamespace(type="response.output_text.delta", delta="Show me ")
            yield SimpleNamespace(type="response.output_text.delta", delta="the test.")
            yield SimpleNamespace(type="response.completed", response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=8, output_tokens=4, total_tokens=12)))
        return events()

    adapter = OpenAIAdapter(_settings())
    monkeypatch.setattr(adapter, "_openai_client",
                        lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    pieces = list(adapter.stream_text_response("Hi"))
    assert pieces[:2] == ["Show me ", "the test."]
    assert (pieces[-1]["prompt_tokens"], pieces[-1]["completion_tokens"]) == (8, 4)


# ── Through the conversation and the wrappers ────────────────────────────────────────────────

def test_the_adapter_stream_preserves_the_first_token_boundary(claude):
    """The actual CLI adapter yields while its owned child waits for the reader.

    Canonical conversation job/lease/receipt proof lives in the real-PG API suite.
    """
    adapter, tmp = claude
    out = []
    for value in adapter.stream_text_response("Hi", system_prompt="You are Vera."):
        if isinstance(value, str) and not out:
            (tmp / "marker").write_text("seen")
        out.append(value)
    assert out[:-1] == ["First words. ", "Then the rest."]
    assert "".join(out[:-1]) == "First words. Then the rest."
    assert out[-1]["completion_tokens"] == 7


def test_the_fallback_wrapper_passes_the_stream_through(claude):
    adapter, tmp = claude
    wrapped = FallingBackAdapter(adapter, SimpleNamespace())
    assert callable(wrapped.stream_text_response)
    first = next(iter(wrapped.stream_text_response("Hi")))
    assert first == "First words. "
    (tmp / "marker").write_text("seen")
    assert FallingBackAdapter(SimpleNamespace(), SimpleNamespace()).stream_text_response is None


def test_the_fallback_wrapper_falls_back_before_the_first_word_only():
    class Down:
        def stream_text_response(self, prompt, system_prompt=None, model=None):
            raise RuntimeError("claude CLI not found. Install Claude Code")
            yield  # pragma: no cover

    class Whole:
        def create_text_response(self, prompt, system_prompt=None, model=None):
            return "r", "From the fallback.", {"completion_tokens": 2}

    pieces = list(FallingBackAdapter(Down(), Whole()).stream_text_response("Hi"))
    assert pieces == ["From the fallback.", {"completion_tokens": 2}]

    class Cut:
        def stream_text_response(self, prompt, system_prompt=None, model=None):
            yield "Half an answer"
            raise RuntimeError("claude CLI timed out after 240s")

    with pytest.raises(RuntimeError, match="timed out"):
        list(FallingBackAdapter(Cut(), Whole()).stream_text_response("Hi"))


def test_the_governed_wrapper_streams_and_settles_with_the_streams_usage(monkeypatch):
    class Inner:
        def stream_text_response(self, prompt, system_prompt=None, model=None):
            yield "Words"
            yield {"model": "m", "completion_tokens": 4}

    ledger: list = []
    governed = GovernedChatAdapter(Inner(), None, None, "p1", skill_id="s", ref="r",
                                   estimate_usd_micros=0)
    monkeypatch.setattr(governed, "_open", lambda: ledger.append("reserved") or ("res", "rcp"))
    monkeypatch.setattr(governed, "_settle", lambda held, usage: ledger.append(("settled", usage)))
    monkeypatch.setattr(governed, "_fail", lambda held, error: ledger.append("released"))
    stream = governed.stream_text_response("Hi")
    assert ledger == []  # nothing is reserved until the stream is read
    assert next(stream) == "Words" and ledger == ["reserved"]
    list(stream)
    assert ledger == ["reserved", ("settled", {"model": "m", "completion_tokens": 4})]

    ledger.clear()
    cut = governed.stream_text_response("Hi")
    next(cut)
    cut.close()  # Stop: the reservation is given back, not settled
    assert ledger == ["reserved", "released"]
    # An inner adapter that cannot stream is not handed out ungoverned.
    assert GovernedChatAdapter(SimpleNamespace(), None, None, "p1", skill_id="s", ref="r",
                               estimate_usd_micros=0).stream_text_response is None

