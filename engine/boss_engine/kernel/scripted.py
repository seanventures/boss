"""THE SCRIPTED CHAT ENGINE: a model that does exactly what a script says, at no cost.

services/scripted_engine.py plays a whole build (a coding engine with `run_role`); it has no chat
surface, so it cannot drive a tool loop. This is its chat counterpart, and it has TWO faces over
ONE script:

  * `complete(ModelRequest) -> ModelTurn`  the kernel's Engine protocol;
  * `create_chat_completion(messages, tools=...) -> (message, usage)`  the adapter shape every
    old loop (agent_loop, EngineAgent, agent_decision, the pre-check, NativeCodeAgent) calls.

The conformance suite hands the SAME script to an old loop and to the kernel, so a difference in
outcome is a difference in the loop, never in the model (test_kernel_conformance.py).

Script items, one per model call:

    Say("text")              a final answer
    Call("tool", {...})      one tool call (args a dict, or a raw string to script malformed JSON)
    Calls(Call(..), ..)      several calls in one turn
    Empty()                  no text and no tool call (a provider hiccup)
    Fail(exception)          the call raises (a 529, a budget deferral, a provider's 400)

Every request is recorded (`requests`): the messages and the tool names offered (None when the
call offered no tools), so a test can assert what the loop asked as well as what it did.
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .engine import CancelToken, EngineTraits, ModelRequest, parse_tool_calls
from .types import ModelTurn, Usage


@dataclass(frozen=True)
class Say:
    text: str


@dataclass(frozen=True)
class Call:
    name: str
    args: Any = None
    id: str = ""


class Calls:
    def __init__(self, *calls: Call):
        self.calls = tuple(calls)


@dataclass(frozen=True)
class Empty:
    pass


@dataclass(frozen=True)
class Fail:
    error: BaseException


class ScriptedEngine:
    """See the module docstring. `cost_usd_micros` is what each call reports it cost, so cost caps
    can be exercised; `after` is what the model says once the script has run out (a loop that
    asked more often than the script expected then fails on an assertion, not an IndexError)."""

    def __init__(self, script: list, *, name: str = "scripted", model: str = "scripted-v1",
                 cost_usd_micros: int = 0, after: Any = None,
                 traits: Optional[EngineTraits] = None):
        self.script = list(script)
        self.name = name
        self.model = model
        self.cost_usd_micros = int(cost_usd_micros)
        self.after = after if after is not None else Say("(the script has ended)")
        self.traits = traits or EngineTraits(family="scripted")
        self.requests: list = []
        self._lock = threading.Lock()
        self._ids = 0

    # ── the kernel face ────────────────────────────────────────────────────────────────────
    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn:
        tools = [t.name for t in req.tools] or None
        message, usage = self._play(req.openai_messages(), tools)
        calls = parse_tool_calls(message.get("tool_calls"))
        text = message.get("content") or ""
        if on_delta is not None and text:
            on_delta(text)
        return ModelTurn(text=text, tool_calls=calls,
                         stop_reason="tool_use" if calls else "end_turn",
                         usage=Usage.from_adapter_usage(usage), raw=dict(message["raw"]))

    def count(self, req: ModelRequest) -> int:
        from .engine import estimate_tokens

        return estimate_tokens(req)

    # ── the adapter face ───────────────────────────────────────────────────────────────────
    def create_chat_completion(self, messages: list, tools: Optional[list] = None,
                               tool_choice: str = "auto", **_kwargs: Any) -> tuple:
        names = [str((t.get("function") or t).get("name") or "") for t in tools] if tools else None
        return self._play(messages, names)

    # ── the script ─────────────────────────────────────────────────────────────────────────
    @property
    def calls(self) -> int:
        return len(self.requests)

    def _play(self, messages: list, tools: Optional[list]) -> tuple:
        with self._lock:
            self.requests.append({"messages": copy.deepcopy(list(messages)), "tools": tools})
            item = self.script.pop(0) if self.script else self.after
        if isinstance(item, Fail):
            raise item.error
        usage = {"model": self.model, "prompt_tokens": 10, "completion_tokens": 5,
                 "total_tokens": 15, "cost_usd_micros": self.cost_usd_micros}
        if isinstance(item, Say):
            return _message(item.text, []), usage
        if isinstance(item, Empty):
            return _message("", []), usage
        calls = list(item.calls) if isinstance(item, Calls) else [item]
        return _message(None, [self._wire(c) for c in calls]), usage

    def _wire(self, call: Call) -> dict:
        with self._lock:
            self._ids += 1
            n = self._ids
        args = call.args if call.args is not None else {}
        raw = args if isinstance(args, str) else json.dumps(args)
        return {"id": call.id or f"call_{n}", "name": call.name, "arguments": raw}


def _message(text: Optional[str], calls: list) -> dict:
    raw: dict = {"role": "assistant", "content": text}
    if calls:
        raw["tool_calls"] = [{"id": c["id"], "type": "function",
                              "function": {"name": c["name"], "arguments": c["arguments"]}}
                             for c in calls]
    return {"content": text, "tool_calls": calls or None, "raw": raw}


__all__ = ["Call", "Calls", "Empty", "Fail", "Say", "ScriptedEngine"]
