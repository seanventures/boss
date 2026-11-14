"""The engine seam: one model round-trip with native tool calling.

An Engine takes a provider-neutral request (system blocks, messages, tool schemas) and returns a
ModelTurn. It owns no loop, no retry, no budget and no tool execution: the kernel does those once
for every engine, which is the point (design §2.3).

"Provider-neutral" messages are the OpenAI chat shape BOSS already stores in agent_runs.messages
({role, content, tool_calls, tool_call_id}). Every engine BOSS runs today speaks it, so a kernel
checkpoint and an agent_loop checkpoint are the same shape (K4 depends on that). An engine on
another wire format (Anthropic Messages, K7) converts on the way in and out.

`OpenAIChatEngine` wraps an existing adapter — OpenAIAdapter or anything with its
`create_chat_completion(messages, tools=..., tool_choice=...)` — and is not a new client.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence, Union

from .types import ModelTurn, ToolCall, Usage


@dataclass(frozen=True)
class EngineTraits:
    """What an engine can do natively. The kernel adds what it lacks; it never removes what it
    has (a frontier engine keeps its own tool calling, caching and search)."""

    native_tools: bool = True
    strict_tools: bool = False
    tool_search: bool = False
    prompt_cache: Literal["explicit", "automatic", "none"] = "none"
    context_window: int = 128_000
    count_tokens: bool = False
    streams: bool = False
    family: str = "openai"


@dataclass(frozen=True)
class Block:
    """One ordered piece of the system prompt. `cache` marks a breakpoint an engine with
    explicit caching may tag (design §4.3); engines without it join the text."""

    text: str
    cache: bool = False


@dataclass
class ModelRequest:
    system: list
    messages: list
    tools: list = field(default_factory=list)
    tool_choice: Union[Literal["auto", "none", "required"], str] = "auto"
    max_output_tokens: Optional[int] = None
    response_schema: Optional[dict] = None
    deadline: Optional[float] = None

    def system_text(self) -> str:
        return "\n\n".join(b.text for b in self.system if isinstance(b, Block) and b.text)

    def openai_messages(self) -> list:
        """The request as the chat-completions message list: the system blocks first, as the
        old loops put their system message first."""
        system = self.system_text()
        head = [{"role": "system", "content": system}] if system else []
        return head + [dict(m) for m in self.messages]


class CancelToken:
    """Set once, read everywhere. `should_stop` reads an outside stop (a code task's stop row);
    `deadline` (time.monotonic seconds) ends the run when passed. Thread-safe: a cancel can come
    from the API thread while the loop runs on a worker thread."""

    def __init__(self, *, should_stop: Optional[Callable[[], bool]] = None,
                 deadline: Optional[float] = None, clock: Optional[Callable[[], float]] = None):
        import time

        self._event = threading.Event()
        self.reason = ""
        self.by = ""
        self.should_stop = should_stop
        self.deadline = deadline
        self.clock = clock or time.monotonic

    def cancel(self, *, by: str = "", reason: str = "cancelled") -> None:
        self.by, self.reason = by, reason
        self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self.deadline is not None and self.clock() >= self.deadline:
            self.cancel(by="deadline", reason="deadline")
            return True
        if self.should_stop is not None:
            try:
                if self.should_stop():
                    self.cancel(by="stop request", reason="stop requested")
                    return True
            except Exception:  # noqa: BLE001 — a stop check that breaks never stops a run
                return False
        return False


class Engine(Protocol):
    name: str
    traits: EngineTraits

    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn: ...

    def count(self, req: ModelRequest) -> int: ...


def estimate_tokens(req: ModelRequest) -> int:
    """chars/4 over everything sent: the heuristic token_efficiency.py already uses, until an
    engine has an exact counter (design §6.1)."""
    size = len(req.system_text()) + len(json.dumps(req.messages, default=str))
    size += len(json.dumps([t.as_openai() for t in req.tools], default=str))
    return max(1, size // 4)


def parse_tool_calls(raw_calls: Optional[Sequence[Mapping[str, Any]]]) -> list:
    """The adapters' `[{id, name, arguments: str}]` as ToolCalls. Arguments that are not a JSON
    object stay `None`, so the kernel refuses the call instead of running the tool on `{}`."""
    calls: list = []
    for n, call in enumerate(raw_calls or []):
        raw = call.get("arguments")
        if isinstance(raw, Mapping):
            parsed: Optional[dict] = dict(raw)
            raw_text = json.dumps(parsed)
        else:
            raw_text = str(raw if raw is not None else "")
            try:
                value = json.loads(raw_text or "{}")
                parsed = value if isinstance(value, dict) else None
            except (TypeError, ValueError):
                parsed = None
        calls.append(ToolCall(id=str(call.get("id") or f"call_{n}"),
                              name=str(call.get("name") or ""),
                              arguments=parsed, raw_arguments=raw_text))
    return calls


class OpenAIChatEngine:
    """Every OpenAI-compatible lane (native, azure, openrouter, groq, deepseek, gemini, xai, vllm,
    ollama) through the adapter BOSS already has. Streaming of tool turns is not offered by the
    adapter, so `on_delta` receives the whole text once the turn is back (true streaming comes
    with the Messages engine, K7)."""

    def __init__(self, adapter: Any, *, name: str = "openai",
                 traits: Optional[EngineTraits] = None, model: Optional[str] = None):
        self.adapter = adapter
        self.name = name
        self.traits = traits or EngineTraits()
        self.model = model

    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn:
        tools = [t.as_openai(strict=self.traits.strict_tools) for t in req.tools] or None
        kwargs: dict = {"tools": tools}
        if tools and req.tool_choice != "auto":
            kwargs["tool_choice"] = req.tool_choice
        if self.model:
            kwargs["model_override"] = self.model
        message, usage = self.adapter.create_chat_completion(req.openai_messages(), **kwargs)
        message = message if isinstance(message, Mapping) else {"content": str(message or "")}
        calls = parse_tool_calls(message.get("tool_calls"))
        text = message.get("content")
        text = text if isinstance(text, str) else ("" if text is None else str(text))
        raw = message.get("raw") or {"role": "assistant", "content": text,
                                     **({"tool_calls": [c.as_openai() for c in calls]}
                                        if calls else {})}
        if on_delta is not None and text:
            on_delta(text)
        return ModelTurn(text=text, tool_calls=calls,
                         stop_reason="tool_use" if calls else "end_turn",
                         usage=_usage_of(usage), raw=dict(raw))

    def count(self, req: ModelRequest) -> int:
        return estimate_tokens(req)


class TextEngine:
    """A TOOL-LESS engine over `create_text_response(prompt, system_prompt=...)` — the one call
    every adapter BOSS has answers, the CLI brains (Claude Code, Codex on a subscription)
    included. Meeting seats, the synthesis and the PRD draft run on it (presets.meeting_seat):
    they produce text, offer no tools, and until now called the adapter raw, outside every
    governance seam.

    The conversation is sent as the adapter has always been asked: a single user message is the
    prompt itself (byte-identical to the old `_ask`); anything longer is rendered role by role.
    A request offering tools is a programming error here and is refused. `last_error` keeps the
    adapter's own exception, so a caller can re-raise exactly what the old call raised."""

    def __init__(self, adapter: Any, *, name: str = "text"):
        self.adapter = adapter
        self.name = name
        self.traits = EngineTraits(native_tools=False, family="text")
        self.last_error: Optional[BaseException] = None

    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn:
        if req.tools:
            raise ValueError("TextEngine serves tool-less runs only; this request offers tools")
        try:
            _ref, text, usage = self.adapter.create_text_response(
                self.prompt_of(req.messages), system_prompt=req.system_text())
        except BaseException as error:
            self.last_error = error
            raise
        text = text if isinstance(text, str) else ("" if text is None else str(text))
        if on_delta is not None and text:
            on_delta(text)
        return ModelTurn(text=text, tool_calls=[], stop_reason="end_turn",
                         usage=_usage_of(usage), raw={"role": "assistant", "content": text})

    @staticmethod
    def prompt_of(messages: Sequence[Mapping[str, Any]]) -> str:
        if len(messages) == 1 and messages[0].get("role") == "user":
            return str(messages[0].get("content") or "")
        return "\n\n".join(f"{str(m.get('role') or 'user').upper()}: {m.get('content') or ''}"
                            for m in messages)

    def count(self, req: ModelRequest) -> int:
        return estimate_tokens(req)


def _usage_of(usage: Any) -> Usage:
    """The adapter's usage dict, priced the way every loop priced it (estimate_cost_usd_micros
    honours an exact cost the backend reported)."""
    u = dict(usage) if isinstance(usage, Mapping) else {}
    try:
        from ..services.openai_adapter import estimate_cost_usd_micros

        cost = int(estimate_cost_usd_micros(u))
    except Exception:  # noqa: BLE001 — an unpriceable usage costs what it says, or nothing
        cost = int(u.get("cost_usd_micros") or 0)
    return Usage.from_adapter_usage(u, cost_usd_micros=cost)


#: EVERY ENGINE THE KERNEL RUNS ON, by name → "module:Class" (AP-P1b acceptance (a)). The kernel
#: conformance suite (test_kernel_engine_conformance.py) plays its scenarios through each one,
#: and fails when an Engine class in this package is missing here — so a new engine cannot skip
#: the suite. Delegate engines (Claude Code, Codex: delegate.py) run their own loops and are
#: not kernel engines.
REGISTERED_ENGINES: dict = {
    "scripted": "boss_engine.kernel.scripted:ScriptedEngine",
    "openai_chat": "boss_engine.kernel.engine:OpenAIChatEngine",
    "anthropic_messages": "boss_engine.kernel.anthropic_engine:AnthropicMessagesEngine",
    "text": "boss_engine.kernel.engine:TextEngine",
}


__all__ = ["Block", "CancelToken", "Engine", "EngineTraits", "ModelRequest", "OpenAIChatEngine",
           "REGISTERED_ENGINES", "TextEngine", "estimate_tokens", "parse_tool_calls"]
