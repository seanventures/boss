"""The five policies a run is made of: context, tools, verify, recovery, stop.

Every rule that differed between the old loops is a field here, not a branch in the loop: when
the last step answers, how a cost cap ends a run, what an empty completion gets, how a tool
result is clipped. The loop (loop.py) reads them and does nothing on its own initiative, so
"what does a decision run do when it runs out of money" is answered by reading one preset
(presets.py), and a fix to one rule reaches every caller that uses it.

Each policy is a versioned value: `name@version` is written on every event the run records
(design §2.5), so a ledger row says which rules produced it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, Protocol, Union

from .engine import Block, ModelRequest
from .errors import Classified
from .state import RunState
from .types import Acceptance, Finding, Step, StopReason, ToolResult

# ── Protocols (design §2.5) ───────────────────────────────────────────────────────────────────


class ContextPolicy(Protocol):
    version: str

    def assemble(self, state: RunState, tools: list) -> ModelRequest: ...

    def render(self, result: ToolResult) -> str: ...

    def needs_compaction(self, state: RunState, window: int) -> bool: ...

    def compact(self, state: RunState, engine: Any) -> RunState: ...


class ToolPolicy(Protocol):
    version: str

    def select(self, state: RunState, available: list) -> list: ...

    def answer_tools(self, state: RunState, reason: StopReason, available: list) -> list: ...

    def max_parallel_calls(self) -> int: ...


class VerifyPolicy(Protocol):
    version: str
    max_reasks: int

    def after_step(self, state: RunState, step: Step) -> list: ...

    def accept_final(self, state: RunState, text: str) -> Acceptance: ...

    def reask(self, why: str) -> str: ...


class RecoveryPolicy(Protocol):
    version: str

    def on_model_error(self, state: RunState, error: Classified, attempts: int) -> str: ...

    def on_tool_error(self, state: RunState, result: ToolResult, failures: int) -> Optional[str]: ...

    def on_invalid(self, state: RunState, why: str, attempts: int) -> Literal["repair", "fail"]: ...

    def on_empty(self, state: RunState) -> Optional[str]: ...


class StopPolicy(Protocol):
    version: str

    def decide(self, state: RunState) -> Optional[StopReason]: ...

    def answer_prompt(self, state: RunState, reason: StopReason) -> Optional[str]: ...


# ── Context ───────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClipContext:
    """The request is the system blocks, the conversation and the step's tools, in that order
    (the cache-stable order of design §4.3). A tool result is shown as JSON clipped to `clip`
    characters with the INCOMPLETE marker (tool_result_text.clip_for_model); `redact` runs the
    secret redactor first, as the decision and pre-check loops did; a string result is shown
    as it is when `raw_strings` (the native coder's sandbox tools return text).

    Compaction is K9: until then nothing is compacted and the window is only reported."""

    clip: Optional[int] = 16_000
    redact: bool = False
    raw_strings: bool = False
    #: How a value JSON cannot encode is shown to the model. `str` everywhere except chat
    #: (K6), which keeps EngineAgent's `_jsonable` — a Decimal stays a number the model can
    #: compare, not a quoted string ("What is my spend this month?" is the incident).
    json_default: Optional[Callable[[Any], Any]] = None
    version: str = "ctx@1"

    def assemble(self, state: RunState, tools: list) -> ModelRequest:
        return ModelRequest(system=list(state.system), messages=list(state.messages),
                            tools=list(tools))

    def render(self, result: ToolResult) -> str:
        content = result.content
        if isinstance(content, str) and self.raw_strings:
            body = content
        else:
            body = json.dumps(content, default=self.json_default or str)
        if self.redact:
            from ..services.redaction import redact_secrets

            body = redact_secrets(body)
        if self.clip:
            from ..services.tool_result_text import clip_for_model

            body = clip_for_model(body, self.clip)
        return body

    def needs_compaction(self, state: RunState, window: int) -> bool:
        return False

    def compact(self, state: RunState, engine: Any) -> RunState:
        return state


# ── Tools ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OfferAll:
    """Every tool the set offers, every step. `answer_offer` names the tools still allowed on an
    answer forced by the turn's budget or clock (EngineAgent's closing propose_action), so the
    fix a run found can still be attached as a button in the same call (engine_agent.py:527)."""

    answer_offer: tuple = ()
    parallel: int = 8
    version: str = "tools@1"

    def select(self, state: RunState, available: list) -> list:
        return list(available)

    def answer_tools(self, state: RunState, reason: StopReason, available: list) -> list:
        if reason.why not in ("budget", "deadline") or not self.answer_offer:
            return []
        if state.extra.get("_offer_spent"):
            return []
        return [t for t in available if t.name in self.answer_offer]

    def max_parallel_calls(self) -> int:
        return self.parallel


# ── Verify ────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AcceptAny:
    """Any final text is the answer (chat, agent steps, the coder): verification of a build's
    work is the pipeline's, done on the diff, never on what the model says."""

    max_reasks: int = 0
    version: str = "verify@1"

    def after_step(self, state: RunState, step: Step) -> list:
        return []

    def accept_final(self, state: RunState, text: str) -> Acceptance:
        return Acceptance(ok=True)

    def reask(self, why: str) -> str:
        return why


@dataclass(frozen=True)
class ParsedAnswer:
    """A final answer that must parse and pass the caller's check (a decision's candidates and
    evidence, the pre-check's JSON items). Refused answers are asked again `max_reasks` times,
    with `reask_text` (a fixed string, or built from the reason)."""

    parse: Callable[[str], Any]
    accept: Callable[[Any, RunState], Acceptance]
    reask_text: Union[str, Callable[[str], str]] = "Answer again with ONLY the JSON object."
    max_reasks: int = 1
    version: str = "verify.parsed@1"

    def after_step(self, state: RunState, step: Step) -> list:
        return []

    def accept_final(self, state: RunState, text: str) -> Acceptance:
        parsed = self.parse(text)
        if parsed is None:
            return Acceptance(ok=False, why="not the JSON object asked for")
        return self.accept(parsed, state)

    def reask(self, why: str) -> str:
        return self.reask_text(why) if callable(self.reask_text) else str(self.reask_text)


# ── Recovery ──────────────────────────────────────────────────────────────────────────────────

GENERIC_NUDGE = (
    "SYSTEM: `{name}` has now failed {count} times in this run. Stop retrying it the same way: "
    "you are in a loop and cannot see it from inside. Use a different tool or different "
    "arguments, or answer now with what you have established and say plainly what you could "
    "not determine.")

EMPTY_PROMPT = (
    "Your last message was empty. Answer the question now in plain text, using what you have "
    "already established. Do not call any more tools and do not apologise.")

REFUSED_CALL_PROMPT = (
    "YOUR LAST TOOL CALL WAS REFUSED BEFORE IT RAN, so nothing happened and "
    "nothing changed. The model provider checked it against the tool's "
    "parameters and rejected it:\n\n"
    "{complaint}\n\n"
    "This is a fault in how the call was written, not in the tool. Read that "
    "tool's parameters again, include every property it marks required, and "
    "make the call again. Everything you have already done still stands — do "
    "not repeat it.")

WaitMode = Literal["durable", "inline", "fail"]


@dataclass(frozen=True)
class Recover:
    """How a run recovers, as data:

      transient_tries     attempts of one model call on a provider hiccup (EngineAgent: 3)
      backoff_seconds     the wait before attempt n is backoff * n (agent/transient.py)
      empty_retries       empty completions pressed again before the run accepts silence
      refused_call_max    provider-refused malformed calls fed back (agent_loop: 3)
      invalid_repairs     the kernel's own schema refusals per tool before the call fails
      same_tool_failures  failures of one tool before the loop is told it is in one (0: never)
      wait_mode           a deferred model call: `durable` parks the run (budget_wait),
                          `inline` sleeps the budget's retry-after and asks again (bounded by
                          `max_wait_seconds`), `fail` ends the run saying so
    """

    transient_tries: int = 3
    backoff_seconds: float = 2.0
    empty_retries: int = 2
    empty_prompt: str = EMPTY_PROMPT
    refused_call_max: int = 3
    refused_call_prompt: str = REFUSED_CALL_PROMPT
    invalid_repairs: int = 1
    same_tool_failures: int = 3
    nudge: str = GENERIC_NUDGE
    wait_mode: WaitMode = "durable"
    max_wait_seconds: float = 180.0
    version: str = "recover@1"

    def on_model_error(self, state: RunState, error: Classified, attempts: int) -> str:
        """retry | feedback | wait | suspend | answer | fail."""
        if error.kind == "transient":
            return "retry" if attempts < self.transient_tries else "fail"
        if error.kind == "refused_call":
            return "feedback" if state.refused_calls < self.refused_call_max else "fail"
        if error.kind == "budget_wait":
            if self.wait_mode == "inline":
                return "wait" if state.waited_seconds < self.max_wait_seconds else "fail"
            return "suspend" if self.wait_mode == "durable" else "fail"
        if error.kind == "deadline":
            return "fail" if state.answering else "answer"
        return "fail"

    def refused_feedback(self, complaint: str) -> str:
        return self.refused_call_prompt.format(complaint=complaint)

    def on_tool_error(self, state: RunState, result: ToolResult, failures: int) -> Optional[str]:
        if self.same_tool_failures and failures == self.same_tool_failures:
            return self.nudge.format(name=result.name, count=failures)
        return None

    def on_invalid(self, state: RunState, why: str, attempts: int) -> Literal["repair", "fail"]:
        return "repair" if attempts <= self.invalid_repairs else "fail"

    def on_empty(self, state: RunState) -> Optional[str]:
        if state.answering or state.empties >= self.empty_retries:
            return None
        return self.empty_prompt

    def finding_for(self, name: str, failures: int) -> Finding:
        return Finding(source="tool", severity="major",
                       message=f"{name} failed {failures} times in this run")


# ── Stop ──────────────────────────────────────────────────────────────────────────────────────

AnswerMode = Literal["extra", "within", "none"]
PromptWhen = Literal["always", "after_tool"]


@dataclass(frozen=True)
class Caps:
    """When a run must answer, and when it must end.

      max_steps        model calls that count
      answer           `within`: the last of max_steps is the answer, with no tools
                       (EngineAgent, decisions, the pre-check); `extra`: max_steps tool steps,
                       then one more call to answer (agent_loop); `none`: no answer step
                       (the native coder: the diff is the answer)
      max_usd_micros   the run's spend cap; `on_cost` says whether reaching it forces the
                       answer (decisions) or ends the run (the pre-check). `cost_lookahead`
                       answers early when one more call at the last call's price would cross
                       it (the pre-check)
      turn_budget      a services.turn_budget.TurnBudget: when it says answer now, the next
                       call is the answer (chat)
      answer_prompt    what the answer call is told; `answer_prompt_when` says whether always
                       or only after a tool result (decisions and the pre-check)
      fallback_text    the answer when the run ends with nothing said
    """

    max_steps: int
    answer: AnswerMode = "within"
    max_usd_micros: Optional[int] = None
    on_cost: Literal["answer", "stop"] = "answer"
    cost_lookahead: bool = False
    turn_budget: Any = None
    answer_prompt_text: Union[None, str, Callable[[RunState, StopReason], str]] = None
    answer_prompt_when: PromptWhen = "always"
    fallback_text: str = ""
    version: str = "stop@1"

    def decide(self, state: RunState) -> Optional[StopReason]:
        spent = state.usage.cost_usd_micros
        if self.max_usd_micros is not None and spent >= self.max_usd_micros:
            if self.on_cost == "stop":
                return StopReason("stop", "cost")
            if state.step < self.max_steps:
                return StopReason("answer", "cost")
        if self.answer == "extra":
            if state.step >= self.max_steps:
                return (StopReason("stop", "steps") if state.extra.get("_extra_answered")
                        else StopReason("answer", "steps"))
        elif state.step >= self.max_steps:
            return StopReason("stop", "steps")
        if state.out_of_time:
            return StopReason("answer", "deadline")
        if self.turn_budget is not None and self.turn_budget.must_answer_now():
            return StopReason("answer", "budget")
        if self.answer == "within" and state.step == self.max_steps - 1:
            return StopReason("answer", "steps")
        if (self.cost_lookahead and self.max_usd_micros is not None and state.step > 0
                and spent + state.last_cost > self.max_usd_micros):
            return StopReason("answer", "cost")
        return None

    def answer_prompt(self, state: RunState, reason: StopReason) -> Optional[str]:
        text = self.answer_prompt_text
        if text is None:
            return None
        if self.answer_prompt_when == "after_tool":
            last = state.messages[-1] if state.messages else {}
            if not (isinstance(last, dict) and last.get("role") == "tool"):
                return None
        elif state.answer_prompted:
            return None
        return text(state, reason) if callable(text) else str(text)


# ── The bundle ────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Policies:
    stop: Any
    context: Any = field(default_factory=ClipContext)
    tools: Any = field(default_factory=OfferAll)
    verify: Any = field(default_factory=AcceptAny)
    recovery: Any = field(default_factory=Recover)

    def versions(self) -> dict:
        return {"context": self.context.version, "tools": self.tools.version,
                "verify": self.verify.version, "recovery": self.recovery.version,
                "stop": self.stop.version}


def system_blocks(system: Union[None, str, list]) -> list:
    """A caller's system prompt as blocks: a string is one block, cached as the stable prefix."""
    if not system:
        return []
    if isinstance(system, str):
        return [Block(text=system, cache=True)]
    return [b if isinstance(b, Block) else Block(text=str(b)) for b in system]


__all__ = ["AcceptAny", "Caps", "ClipContext", "ContextPolicy", "EMPTY_PROMPT", "GENERIC_NUDGE",
           "OfferAll", "ParsedAnswer", "Policies", "REFUSED_CALL_PROMPT", "Recover",
           "RecoveryPolicy", "StopPolicy", "ToolPolicy", "VerifyPolicy", "system_blocks"]
