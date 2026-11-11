"""THE ANTHROPIC MESSAGES ENGINE, PROVEN AGAINST A FAKE MESSAGES API (BOSSAPP-961 K7).

The fake sits at the httpx transport seam under the real `anthropic` client, so everything the
SDK would put on the wire — tool schemas, cache breakpoints, the folded tool_result turns, SSE
streaming — is what these tests read, and nothing is monkeypatched inside the engine.

What is proven here, fake-first (HARNESS_KERNEL_DESIGN.md §8.1 K7):

  * a tool call round-trips through the KERNEL loop: tool_use out, tool_result back, answer;
  * strict schemas go up with `strict: true` (and a schema that does not qualify goes up bare);
  * cache breakpoints land on the marked system blocks and the tool prefix, stable prefix
    first — a cached block after changing state is refused loudly;
  * deferred tools ride with `defer_loading` behind the provider's BM25 search tool;
  * usage maps into the ledger's fields exactly as OpenAIChatEngine maps the adapters' (input,
    output, cache read, cache write, cost at the priced Anthropic rates);
  * a 529/429 is transient (the kernel retries it), a 400 is permanent (the run fails carrying
    the provider's words), and a provider's refusal of a malformed tool call stays the kernel's
    one feedback behaviour;
  * streaming delivers deltas as they arrive and a CancelToken stops the stream BETWEEN events;
  * `count` is exact through /v1/messages/count_tokens;
  * the engine-generic conformance scenarios (step cap, refused call, malformed-call repair,
    empty completion, transient) hold on this engine as they hold on the scripted one;
  * the K7 selection seam (model_routing.kernel_chat_engine) answers an engine only behind
    BOSS_CHAT_ANTHROPIC_NATIVE on a Claude lane with an API key, through the model proxy.

One live smoke test exists at the bottom; it is skipped without ANTHROPIC_API_KEY in the
environment, so CI never spends money here.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.anthropic_engine import (
    DEFAULT_MODEL, NATIVE_CONTENT_KEY, AnthropicMessagesEngine, NoAnthropicKey,
    ProviderPermanent, ProviderTransient, engine_for)
from boss_engine.kernel.engine import Block, CancelToken, ModelRequest
from boss_engine.kernel.errors import classify
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema

SYSTEM = "You are the test agent."
ASK = "Check the run."
MODEL = "claude-haiku-4-5"

READ_SCHEMA = {"type": "object", "properties": {"run": {"type": "string"}},
               "required": ["run"], "additionalProperties": False}
LOOSE_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}

REFUSED_400 = ("tool call validation failed: parameters for tool read_logs did not match "
               "schema: errors: [missing properties: 'run']")


def read_logs_result(args):
    return {"ok": True, "tool": "read_logs", "lines": [f"run {args.get('run')}: 3 errors"]}


def kernel_tools():
    return FunctionToolSet([Tool(ToolSchema("read_logs", "Read a run's logs.", READ_SCHEMA),
                                 read_logs_result)])


# ── The fake Messages API ─────────────────────────────────────────────────────────────────────

def say(text, *, usage=None, stop="end_turn"):
    content = [{"type": "text", "text": text}] if text is not None else []
    return {"json": _message(content, stop, usage)}


def tool_use(name, args, *, call_id="toolu_1", text=None, usage=None):
    content = ([{"type": "text", "text": text}] if text else [])
    content.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
    return {"json": _message(content, "tool_use", usage)}


def error(status, err_type, message):
    return {"status": status,
            "json": {"type": "error", "error": {"type": err_type, "message": message}}}


def sse(*events):
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return {"sse": body}


def _usage(usage):
    return {"input_tokens": 100, "output_tokens": 9, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, **(usage or {})}


def _message(content, stop, usage):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": MODEL,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": _usage(usage)}


class FakeMessagesAPI:
    """Scripted answers for POST /v1/messages (and a fixed one for count_tokens), recording
    every request body so the tests read the wire shape, not the engine's intentions."""

    def __init__(self, script, *, count=42):
        self.script = list(script)
        self.count = count
        self.requests: list = []
        self.count_requests: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/count_tokens"):
            self.count_requests.append(body)
            return httpx.Response(200, json={"input_tokens": self.count})
        self.requests.append(body)
        assert self.script, "the engine called more often than the script expected"
        item = self.script.pop(0)
        if "sse" in item:
            return httpx.Response(200, content=item["sse"].encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(item.get("status", 200), json=item["json"])

    def engine(self, *, model=MODEL, **kw) -> AnthropicMessagesEngine:
        import anthropic

        client = anthropic.Anthropic(
            api_key="sk-fake-test", base_url="http://fake.local", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)))
        return AnthropicMessagesEngine(client, model=model, **kw)


def kernel_run(fake, policies, *, tools=None, messages=None, system=SYSTEM, **spec_kw):
    engine = fake.engine()
    result = kernel.run(kernel.RunSpec(
        engine=engine, tools=tools or kernel_tools(), policies=policies, project_id="p",
        system=system, messages=messages or [{"role": "user", "content": ASK}],
        sleep=lambda s: None, **spec_kw))
    return result, fake


def request_tools(body):
    return [t.get("name") for t in body.get("tools") or []]


# ════ The round trip ══════════════════════════════════════════════════════════════════════════

class TestToolCallRoundTrip:
    def test_a_tool_call_round_trips_through_the_kernel(self):
        fake = FakeMessagesAPI([
            tool_use("read_logs", {"run": "r1"},
                     usage={"cache_creation_input_tokens": 40, "cache_read_input_tokens": 0}),
            say("Both runs are red.", usage={"cache_read_input_tokens": 60, "input_tokens": 7}),
        ])
        result, _ = kernel_run(fake, presets.agent_loop(4))
        assert result.status == "completed" and result.text == "Both runs are red."
        assert [t["tool"] for t in result.trace] == ["read_logs"]

        first, second = fake.requests
        # The system prompt went as a cached text block, before the conversation.
        assert first["system"][0]["text"] == SYSTEM
        assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert first["messages"][0] == {"role": "user", "content": ASK}
        # The strict tool schema went up natively, with the tool-prefix breakpoint.
        tool = first["tools"][0]
        assert tool["name"] == "read_logs" and tool["input_schema"] == READ_SCHEMA
        assert tool["strict"] is True
        assert tool["cache_control"] == {"type": "ephemeral"}
        # The tool_use came back to the API as itself, and its result as ONE tool_result turn.
        assistant = second["messages"][1]
        assert assistant["role"] == "assistant"
        assert {"type": "tool_use", "id": "toolu_1", "name": "read_logs",
                "input": {"run": "r1"}} in assistant["content"]
        results = second["messages"][2]
        assert results["role"] == "user"
        assert results["content"][0]["type"] == "tool_result"
        assert results["content"][0]["tool_use_id"] == "toolu_1"
        assert "3 errors" in results["content"][0]["content"]

    def test_usage_and_cost_map_into_the_ledgers_fields(self):
        """input/output/cache_read/cache_write and USD, priced as openai_adapter prices every
        engine: claude-haiku-4-5 is ($1, $5)/MTok, so micros = in*1 + out*5 + read*0.1 +
        write*1.25 — the same arithmetic OpenAIChatEngine's usage takes."""
        fake = FakeMessagesAPI([say("done", usage={
            "input_tokens": 100, "output_tokens": 10,
            "cache_read_input_tokens": 60, "cache_creation_input_tokens": 40})])
        turn = fake.engine().complete(ModelRequest(
            system=[Block(SYSTEM, cache=True)], messages=[{"role": "user", "content": ASK}]))
        u = turn.usage
        assert (u.input_tokens, u.output_tokens) == (100, 10)
        assert (u.cache_read_tokens, u.cache_write_tokens) == (60, 40)
        assert u.model == MODEL and u.billing == "metered"
        assert u.cost_usd_micros == round(100 * 1.0 + 10 * 5.0 + 60 * 1.0 * 0.1
                                          + 40 * 1.0 * 1.25)

    def test_the_native_blocks_ride_the_checkpoint_and_replay_verbatim(self):
        """ModelTurn.raw stays the OpenAI checkpoint shape (K4's contract) and carries the
        provider-native blocks beside it; a raw without them (another engine's checkpoint) is
        rebuilt from text + tool_calls."""
        fake = FakeMessagesAPI([tool_use("read_logs", {"run": "r1"}, text="Looking.")])
        turn = fake.engine().complete(ModelRequest(system=[], messages=[
            {"role": "user", "content": ASK}]))
        assert turn.raw["role"] == "assistant" and turn.raw["content"] == "Looking."
        assert turn.raw["tool_calls"][0]["function"]["name"] == "read_logs"
        assert turn.raw[NATIVE_CONTENT_KEY][1]["type"] == "tool_use"

        replay = FakeMessagesAPI([say("ok")])
        replay.engine().complete(ModelRequest(system=[], messages=[
            {"role": "user", "content": ASK},
            {"role": "assistant", "content": "Looking.",
             "tool_calls": [{"id": "toolu_9", "type": "function",
                             "function": {"name": "read_logs",
                                          "arguments": "{\"run\": \"r2\"}"}}]},
            {"role": "tool", "tool_call_id": "toolu_9", "content": "{\"ok\": true}"},
        ]))
        sent = replay.requests[0]["messages"]
        assert sent[1]["content"] == [{"type": "text", "text": "Looking."},
                                      {"type": "tool_use", "id": "toolu_9", "name": "read_logs",
                                       "input": {"run": "r2"}}]
        assert sent[2]["content"][0]["tool_use_id"] == "toolu_9"


# ════ The request shape ═══════════════════════════════════════════════════════════════════════

class TestRequestShape:
    def test_cache_breakpoints_fall_on_the_marked_blocks_and_changing_state_goes_last(self):
        fake = FakeMessagesAPI([say("ok")])
        fake.engine().complete(ModelRequest(
            system=[Block("stable primer", cache=True), Block("role identity", cache=True),
                    Block("changing state: budget left $2")],
            messages=[{"role": "user", "content": ASK}],
            tools=[ToolSchema("read_logs", "Read.", READ_SCHEMA)]))
        body = fake.requests[0]
        marks = ["cache_control" in b for b in body["system"]]
        assert marks == [True, True, False]
        assert body["system"][-1]["text"].startswith("changing state")
        assert "cache_control" in body["tools"][-1]
        # tools -> system -> messages is the wire's own order; the changing block is the last
        # system block and the new user message is the last message.
        assert body["messages"][-1] == {"role": "user", "content": ASK}

    def test_a_cached_block_after_changing_state_is_refused_loudly(self):
        fake = FakeMessagesAPI([])
        with pytest.raises(ValueError, match="stable prefix first"):
            fake.engine().complete(ModelRequest(
                system=[Block("changing"), Block("stable", cache=True)],
                messages=[{"role": "user", "content": ASK}]))

    def test_strict_goes_up_only_when_the_schema_qualifies(self):
        fake = FakeMessagesAPI([say("ok")])
        fake.engine().complete(ModelRequest(
            system=[], messages=[{"role": "user", "content": ASK}],
            tools=[ToolSchema("read_logs", "Read.", READ_SCHEMA),
                   ToolSchema("search", "Search.", LOOSE_SCHEMA)]))
        tools = {t["name"]: t for t in fake.requests[0]["tools"]}
        assert tools["read_logs"]["strict"] is True
        assert "strict" not in tools["search"]

    def test_deferred_tools_ride_behind_the_provider_search_tool(self):
        fake = FakeMessagesAPI([say("ok"), say("ok")])
        engine = fake.engine()
        engine.complete(ModelRequest(
            system=[Block(SYSTEM, cache=True)], messages=[{"role": "user", "content": ASK}],
            tools=[ToolSchema("read_logs", "Read.", READ_SCHEMA),
                   ToolSchema("rare_tool", "Rarely.", READ_SCHEMA, deferred=True)]))
        tools = fake.requests[0]["tools"]
        assert [t.get("name") for t in tools] == ["read_logs", "tool_search_tool_bm25",
                                                  "rare_tool"]
        assert tools[1]["type"] == "tool_search_tool_bm25_20251119"
        assert tools[2]["defer_loading"] is True
        assert "cache_control" not in tools[2], "a deferred tool may not carry a breakpoint"
        assert "cache_control" in tools[0], "the breakpoint sits on the core tool prefix"
        # Every tool deferred would be refused by the API; the guard sends them plain instead.
        engine.complete(ModelRequest(
            system=[], messages=[{"role": "user", "content": ASK}],
            tools=[ToolSchema("rare_tool", "Rarely.", READ_SCHEMA, deferred=True)]))
        only = fake.requests[1]["tools"]
        assert [t.get("name") for t in only] == ["rare_tool"]
        assert "defer_loading" not in only[0]

    def test_tool_choice_maps_to_the_messages_shapes(self):
        fake = FakeMessagesAPI([say("a"), say("b"), say("c")])
        engine = fake.engine()
        for choice in ("required", "none", "read_logs"):
            engine.complete(ModelRequest(system=[], messages=[{"role": "user", "content": ASK}],
                                         tools=[ToolSchema("read_logs", "Read.", READ_SCHEMA)],
                                         tool_choice=choice))
        assert fake.requests[0]["tool_choice"] == {"type": "any"}
        assert fake.requests[1]["tool_choice"] == {"type": "none"}
        assert fake.requests[2]["tool_choice"] == {"type": "tool", "name": "read_logs"}


# ════ Errors: transient, permanent, refused call ══════════════════════════════════════════════

class TestTypedErrors:
    def test_overload_and_rate_limit_are_transient_and_the_kernel_retries_them(self):
        fake = FakeMessagesAPI([
            error(529, "overloaded_error", "Overloaded"),
            error(429, "rate_limit_error", "Number of requests has exceeded your rate limit"),
            say("third attempt answered"),
        ])
        result, _ = kernel_run(fake, presets.engine_agent(6))
        assert result.status == "completed" and result.text == "third attempt answered"
        assert [e.kind for e in result.events].count("model.retry") == 2
        assert len(fake.requests) == 3

    def test_an_invalid_request_is_permanent_and_fails_with_the_providers_words(self):
        fake = FakeMessagesAPI([
            error(400, "invalid_request_error", "max_tokens: must be at least 1")])
        result, _ = kernel_run(fake, presets.engine_agent(6))
        assert result.status == "failed"
        assert "max_tokens: must be at least 1" in result.error
        assert len(fake.requests) == 1, "a permanent refusal is never retried"

    def test_the_classes_classify_as_declared(self):
        assert classify(ProviderTransient("529")).kind == "transient"
        assert classify(ProviderPermanent("400")).kind == "fatal"


# ════ Streaming and cancellation ══════════════════════════════════════════════════════════════

def stream_script(*deltas, stop="end_turn", tool=None):
    events = [{"type": "message_start",
               "message": _message([], None, {"input_tokens": 10,
                                              "cache_read_input_tokens": 6})},
              {"type": "content_block_start", "index": 0,
               "content_block": {"type": "text", "text": ""}}]
    events += [{"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": d}} for d in deltas]
    events.append({"type": "content_block_stop", "index": 0})
    if tool is not None:
        name, parts = tool
        events.append({"type": "content_block_start", "index": 1,
                       "content_block": {"type": "tool_use", "id": "toolu_s", "name": name,
                                         "input": {}}})
        events += [{"type": "content_block_delta", "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": p}} for p in parts]
        events.append({"type": "content_block_stop", "index": 1})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop,
                                                      "stop_sequence": None},
                   "usage": {"output_tokens": 5}})
    events.append({"type": "message_stop"})
    return sse(*events)


class TestStreaming:
    def test_deltas_arrive_as_they_stream_and_the_turn_is_whole(self):
        fake = FakeMessagesAPI([stream_script("Hel", "lo ", "there",
                                              tool=("read_logs", ['{"ru', 'n": "r1"}']),
                                              stop="tool_use")])
        got: list = []
        turn = fake.engine().complete(
            ModelRequest(system=[], messages=[{"role": "user", "content": ASK}]),
            on_delta=got.append)
        assert got == ["Hel", "lo ", "there"]
        assert turn.text == "Hello there" and turn.stop_reason == "tool_use"
        assert turn.tool_calls[0].name == "read_logs"
        assert turn.tool_calls[0].arguments == {"run": "r1"}
        assert turn.usage.input_tokens == 10 and turn.usage.output_tokens == 5
        assert turn.usage.cache_read_tokens == 6

    def test_a_cancel_token_stops_the_stream_between_events(self):
        fake = FakeMessagesAPI([stream_script("first ", "never read", "nor this")])
        token = CancelToken()
        got: list = []

        def cancelling(text):
            got.append(text)
            token.cancel(by="operator", reason="stop requested")

        with pytest.raises(Exception) as caught:
            fake.engine().complete(
                ModelRequest(system=[], messages=[{"role": "user", "content": ASK}]),
                on_delta=cancelling, cancel=token)
        from boss_engine.kernel.errors import Cancelled

        assert isinstance(caught.value, Cancelled)
        assert got == ["first "], "nothing was read past the cancel"

    def test_a_cancelled_kernel_run_ends_cancelled(self):
        fake = FakeMessagesAPI([stream_script("thinking ", "aloud")])
        token = CancelToken()
        engine = fake.engine()
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=kernel_tools(), policies=presets.engine_agent(6),
            project_id="p", system=SYSTEM, messages=[{"role": "user", "content": ASK}],
            cancel=token, on_delta=lambda _t: token.cancel(by="operator"),
            sleep=lambda s: None))
        assert result.status == "cancelled"
        assert len(fake.requests) == 1


# ════ Counting ════════════════════════════════════════════════════════════════════════════════

class TestCountTokens:
    def test_count_is_exact_through_the_counter_endpoint(self):
        fake = FakeMessagesAPI([], count=1234)
        req = ModelRequest(system=[Block(SYSTEM, cache=True)],
                           messages=[{"role": "user", "content": ASK}],
                           tools=[ToolSchema("read_logs", "Read.", READ_SCHEMA)])
        assert fake.engine().count(req) == 1234
        body = fake.count_requests[0]
        assert "max_tokens" not in body and body["tools"][0]["name"] == "read_logs"

    def test_a_failed_count_falls_back_to_the_estimate(self):
        def refusing(request):
            return httpx.Response(500, json={"type": "error",
                                             "error": {"type": "api_error", "message": "down"}})

        import anthropic

        client = anthropic.Anthropic(
            api_key="sk-fake-test", base_url="http://fake.local", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(refusing)))
        engine = AnthropicMessagesEngine(client, model=MODEL)
        req = ModelRequest(system=[Block(SYSTEM)], messages=[{"role": "user", "content": ASK}])
        assert engine.count(req) > 0


# ════ The engine-generic conformance scenarios (design §8.1 K1 list, on this engine) ══════════

class TestConformanceScenarios:
    def test_step_cap_answers_without_tools(self):
        fake = FakeMessagesAPI([
            tool_use("read_logs", {"run": "r1"}, call_id="toolu_a"),
            tool_use("read_logs", {"run": "r2"}, call_id="toolu_b"),
            say("Both runs are red."),
        ])
        result, _ = kernel_run(fake, presets.engine_agent(3))
        assert result.status == "completed" and result.exhausted == "steps"
        assert [request_tools(r) for r in fake.requests] == [
            ["read_logs"], ["read_logs"], []]
        assert fake.requests[-1]["messages"][-1]["content"].startswith(
            "You are out of tool budget for this turn.")

    def test_the_providers_refusal_of_a_malformed_call_is_fed_back(self):
        fake = FakeMessagesAPI([
            error(400, "invalid_request_error", REFUSED_400),
            say("rewrote the call; done"),
        ])
        result, _ = kernel_run(fake, presets.agent_loop(4))
        assert result.status == "completed" and result.text == "rewrote the call; done"
        feedback = fake.requests[1]["messages"][-1]["content"]
        assert feedback.startswith("YOUR LAST TOOL CALL WAS REFUSED BEFORE IT RAN")
        assert "did not match" in feedback

    def test_a_schema_breaking_call_is_refused_before_the_tool_runs_and_repaired(self):
        ran: list = []

        def recording(args):
            ran.append(dict(args))
            return read_logs_result(args)

        tools = FunctionToolSet([Tool(ToolSchema("read_logs", "Read.", READ_SCHEMA), recording)])
        fake = FakeMessagesAPI([
            tool_use("read_logs", {"run": 7}, call_id="toolu_bad"),
            tool_use("read_logs", {"run": "r1"}, call_id="toolu_good"),
            say("ok"),
        ])
        result, _ = kernel_run(fake, presets.agent_loop(6), tools=tools)
        assert result.status == "completed"
        assert ran == [{"run": "r1"}], "the schema-breaking call never reached the tool"
        refusal = json.loads(fake.requests[1]["messages"][-1]["content"][0]["content"])
        assert refusal["error"] == "invalid_arguments"
        kinds = [e.kind for e in result.events]
        assert "schema.invalid" in kinds and "schema.repaired" in kinds

    def test_an_empty_completion_is_pressed_twice(self):
        fake = FakeMessagesAPI([say(None), say(None), say("third time lucky")])
        result, _ = kernel_run(fake, presets.engine_agent(6))
        assert result.status == "completed" and result.text == "third time lucky"
        assert fake.requests[1]["messages"][-1]["content"].startswith(
            "Your last message was empty.")

    def test_a_transient_run_out_of_retries_fails_with_the_last_words(self):
        fake = FakeMessagesAPI([error(529, "overloaded_error", "Overloaded")] * 3)
        result, _ = kernel_run(fake, presets.engine_agent(6))
        assert result.status == "failed" and "529" in result.error
        assert len(fake.requests) == 3


# ════ The selection seam (flag + key, through the model proxy) ════════════════════════════════

def _settings(**extra):
    return SimpleNamespace(claude_credential_kind="", claude_bound_api_key="",
                           anthropic_api_key=extra.pop("anthropic_api_key", ""),
                           claude_cli_model=extra.pop("claude_cli_model", ""),
                           agent_sdk_model="", chat_model="",
                           code_agent_timeout_seconds=0, boss_secrets_dir="",
                           **extra)


@pytest.fixture()
def no_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BOSS_CHAT_ANTHROPIC_NATIVE", raising=False)


class TestSelectionSeam:
    def test_the_flag_off_means_none(self, no_env_key, monkeypatch):
        from boss_engine.services.model_routing import ModelChoice, kernel_chat_engine

        choice = ModelChoice("claude_code", "test")
        assert kernel_chat_engine(choice, None, _settings(anthropic_api_key="sk-x"), "p") is None

    def test_a_lane_that_is_not_claude_means_none(self, no_env_key, monkeypatch):
        from boss_engine.services.model_routing import ModelChoice, kernel_chat_engine

        monkeypatch.setenv("BOSS_CHAT_ANTHROPIC_NATIVE", "1")
        choice = ModelChoice("native", "test")
        assert kernel_chat_engine(choice, None, _settings(anthropic_api_key="sk-x"), "p") is None

    def test_no_api_key_means_none_even_with_the_flag_on(self, no_env_key, monkeypatch):
        from boss_engine.services.model_routing import ModelChoice, kernel_chat_engine

        monkeypatch.setenv("BOSS_CHAT_ANTHROPIC_NATIVE", "1")
        choice = ModelChoice("claude_code", "test")
        assert kernel_chat_engine(choice, None, _settings(), "p") is None

    def test_the_engine_is_built_on_a_proxy_token_never_the_key(self, no_env_key, monkeypatch):
        from boss_engine.services import model_proxy
        from boss_engine.services.model_routing import ModelChoice, kernel_chat_engine

        proxy = model_proxy.ModelProxy()
        monkeypatch.setattr(model_proxy, "_PROXY", proxy)
        monkeypatch.setenv("BOSS_CHAT_ANTHROPIC_NATIVE", "true")
        try:
            engine = kernel_chat_engine(
                ModelChoice("claude_code", "test"), None,
                _settings(anthropic_api_key="sk-ant-test-not-a-real-key",
                          claude_cli_model="claude-opus-4-6"), "p")
            assert isinstance(engine, AnthropicMessagesEngine)
            assert engine.model == "claude-opus-4-6"
            assert engine.traits.native_tools and engine.traits.count_tokens
            assert engine.traits.prompt_cache == "explicit" and engine.traits.tool_search
            held = engine.client.api_key
            assert held.startswith(model_proxy.TOKEN_PREFIX), "the engine holds a run token"
            assert held != "sk-ant-test-not-a-real-key"
            assert str(engine.client.base_url).startswith("http://127.0.0.1:")
            assert str(engine.client.base_url).rstrip("/").endswith("/anthropic")
            grant = proxy.grant_for(held)
            assert grant is not None and grant.live()
            engine.close()
            assert proxy.grant_for(held).revoked, "close revokes the token"
        finally:
            proxy.stop()
            monkeypatch.setattr(model_proxy, "_PROXY", None)

    def test_a_cli_shorthand_model_falls_back_to_the_engines_default(self, no_env_key,
                                                                     monkeypatch):
        from boss_engine.services import model_proxy
        from boss_engine.services.model_routing import ModelChoice, kernel_chat_engine

        proxy = model_proxy.ModelProxy()
        monkeypatch.setattr(model_proxy, "_PROXY", proxy)
        monkeypatch.setenv("BOSS_CHAT_ANTHROPIC_NATIVE", "1")
        try:
            engine = kernel_chat_engine(
                ModelChoice("claude_code", "test"), None,
                _settings(anthropic_api_key="sk-ant-test-not-a-real-key",
                          claude_cli_model="sonnet"), "p")
            assert engine is not None and engine.model == DEFAULT_MODEL
            engine.close()
        finally:
            proxy.stop()
            monkeypatch.setattr(model_proxy, "_PROXY", None)

    def test_engine_for_raises_no_anthropic_key_when_none_is_held(self, no_env_key):
        with pytest.raises(NoAnthropicKey):
            engine_for(_settings())


# ════ One live smoke call (skipped without a key; ≤ a few cents on haiku) ═════════════════════

@pytest.mark.skipif(not (os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
                    reason="no ANTHROPIC_API_KEY in this environment; the fake suite above "
                           "is the K7 proof")
def test_live_smoke_one_tool_call_on_haiku():
    """One real round trip through the model proxy: a tool call, its result, an answer. The
    cost is printed so the operator's report can carry it."""
    from boss_engine.services import model_proxy

    proxy = model_proxy.ModelProxy()
    settings = SimpleNamespace(code_agent_timeout_seconds=0, anthropic_api_key="",
                               claude_credential_kind="", claude_bound_api_key="")
    issued = proxy.issue("anthropic", os.environ["ANTHROPIC_API_KEY"],
                         ttl=model_proxy.session_ttl(settings))
    import anthropic

    client = anthropic.Anthropic(api_key=issued.token, base_url=issued.base_url, max_retries=0)
    engine = AnthropicMessagesEngine(client, model="claude-haiku-4-5",
                                     max_output_tokens=256,
                                     on_close=lambda: proxy.revoke(issued.token))
    try:
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=kernel_tools(), policies=presets.engine_agent(3),
            project_id="p", system="Answer with the read_logs tool, then one short sentence.",
            messages=[{"role": "user", "content": "How many errors does run r1 show?"}]))
        assert result.status == "completed"
        assert any(t["tool"] == "read_logs" for t in result.trace), "the model called the tool"
        assert result.usage.cost_usd_micros > 0
        print(f"\nlive smoke: {result.steps} steps, "
              f"in={result.usage.input_tokens} out={result.usage.output_tokens} "
              f"cache_read={result.usage.cache_read_tokens} "
              f"cache_write={result.usage.cache_write_tokens} "
              f"cost=${result.usage.cost_usd_micros / 1e6:.4f}")
    finally:
        engine.close()
        proxy.stop()
