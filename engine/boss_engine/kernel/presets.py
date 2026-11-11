"""Each old loop's rules, as kernel policies (design §2.5, §2.10, §8.2).

A caller moving onto the kernel (K2 decisions, K3 the pre-check, K4 agent steps, K6 chat, K11
the coder) passes its preset and gets its old behaviour — the conformance suite
(test_kernel_conformance.py) runs the same scripts through both and holds them equal — except
for the INTENDED differences, each one stricter or safer than before and listed in
docs/architecture/HARNESS_KERNEL_DESIGN.md §8.4:

  * a transient provider error is retried (≤3 tries) by every preset, not only chat's;
  * a provider's 400 on a malformed call is fed back (≤3) by every preset, not only agent_loop's;
  * a call whose arguments break the tool's schema is refused before it runs (one repair);
  * an agent step's empty completion is pressed twice instead of being the answer;
  * the same tool failing three times draws a nudge and a finding in agent steps, decisions and
    the pre-check (chat had it; the coder keeps iterating, as test runs fail until they pass);
  * an agent step whose MODEL call the budget defers parks (budget_wait) instead of failing;
  * every call of a step is answered, the coder's held call's siblings included;
  * every preset honours a CancelToken before each model and tool call.

The answer-now texts of agent_loop and EngineAgent were inline literals; they are copied here
VERBATIM and the conformance suite compares the requests, so the copies cannot drift. The old
literals go when their loop moves (K4, K6). The decision and pre-check texts are imported, not
copied: they are module constants already.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .policies import (AcceptAny, Caps, ClipContext, OfferAll, ParsedAnswer, Policies, Recover)
from .state import RunState
from .types import Acceptance, StopReason

# ── agent_loop._drive_run (workflow agent steps, the factory plan) ────────────────────────────

AGENT_LOOP_ANSWER = (
    "You have used your tool budget for this turn. Do not apologise and do not ask "
    "to continue. Write the answer from what you have already established:\n"
    "1. What you FOUND, with the concrete evidence (ids, counts, timings).\n"
    "2. What you could NOT determine, and precisely which check would settle it.\n"
    "3. The single next action you would take.\n"
    "Partial findings are useful; a report of running out of steps is not."
)
AGENT_LOOP_FALLBACK = (
    "I ran out of tool budget before reaching a conclusion, and could not summarise "
    "what I had. Ask again narrowing the question to one run or one table."
)


def agent_loop_resume_note(pending: dict, body: str) -> str:
    return (f"APPROVED & EXECUTED: a human approved the deferred action "
            f"'{pending.get('tool')}'. Result: {body[:2000]}. "
            "Continue and finalize your plan, incorporating this outcome.")


def agent_loop(max_steps: int) -> Policies:
    """`max_steps` tool steps, then one call without tools to answer (agent_loop.py:2960)."""
    return Policies(
        stop=Caps(max_steps=int(max_steps), answer="extra", answer_prompt_text=AGENT_LOOP_ANSWER,
                  fallback_text=AGENT_LOOP_FALLBACK, version="stop.agent_loop@1"),
        context=ClipContext(clip=16_000, version="ctx.agent_loop@1"),
        tools=OfferAll(version="tools.agent_loop@1"),
        verify=AcceptAny(),
        recovery=Recover(wait_mode="durable", version="recover.agent_loop@1"))


# ── EngineAgent.chat (Ask BOSS, Metatron, the CLI turn) ───────────────────────────────────────

ENGINE_AGENT_NUDGE = (
    "SYSTEM: `{name}` has now failed {count} times "
    "this turn. Stop retrying it — you are in a loop and cannot "
    "see it from inside.\n\n"
    "Do this instead, in order:\n"
    "1. Is there a PURPOSE-BUILT tool for what you are trying to "
    "learn? Hand-written SQL is the fallback, not the first "
    "resort — e.g. a workflow run (wfr_) is `describe_workflow_run`"
    ", a code-task run (crun_) is `diagnose_run`.\n"
    "2. If there is genuinely no tool for this, that is a GAP IN "
    "BOSS. Use `search_code` to find the relevant platform code, "
    "and `create_code_task` to raise the fix. That is a real, "
    "expected part of your job.\n"
    "3. Either way, ANSWER THE OPERATOR NOW with what you already "
    "know, say plainly what you could not determine and why, and "
    "give them a concrete next step. Do not end the turn with an "
    "apology about your own tooling and nothing else."
)
_ENGINE_AGENT_ANSWER_BODY = (
    "Do not apologise. Answer from what you have already established:\n"
    "1. The answer to the operator's question, with concrete evidence.\n"
    "2. What is left: each part of the request you could NOT check or finish,"
    " one line each, and why.\n"
    "3. The single next action you would take.\n"
    "The operator gets a one-press Continue that resumes from your list of "
    "what is left, so never ask them to ask again or to re-send the request. "
    "Partial findings are useful; a report of running out of steps is not."
)
_ENGINE_AGENT_OFFER = (
    "\nIf you have found a fix and have not yet offered it, you may call "
    "propose_action ONCE in this same message to attach it as a button "
    "— and write the answer in this message too. There is no further "
    "turn after this one."
)


def engine_agent(max_steps: int = 6, *, budget: Any = None,
                 offer: tuple = ("propose_action",)) -> Policies:
    """The last of `max_steps` answers with no tools; the turn's budget forces it sooner, and
    then the answer may carry one propose_action button in the same call."""

    def answer_prompt(state: RunState, reason: StopReason) -> str:
        out_of_turn = reason.why in ("budget", "deadline")
        head = (("This turn's budget is spent ("
                 + (budget.describe() if budget is not None else "its time is up") + "). ")
                if out_of_turn else "You are out of tool budget for this turn. ")
        offering = out_of_turn and bool(offer) and not state.extra.get("_offer_spent")
        return head + _ENGINE_AGENT_ANSWER_BODY + (_ENGINE_AGENT_OFFER if offering else "")

    return Policies(
        stop=Caps(max_steps=int(max_steps), answer="within", turn_budget=budget,
                  answer_prompt_text=answer_prompt, version="stop.engine_agent@1"),
        context=ClipContext(clip=16_000, version="ctx.engine_agent@1"),
        tools=OfferAll(answer_offer=tuple(offer), version="tools.engine_agent@1"),
        verify=AcceptAny(),
        recovery=Recover(nudge=ENGINE_AGENT_NUDGE, version="recover.engine_agent@1"))


# ── agent_decision._ask_reading (AgentService.decide with a read phase) ───────────────────────

def decision(*, max_turns: int = 4, max_usd_micros: int = 300_000, result_chars: int = 8_000,
             accept: Callable[[Any, RunState], Acceptance]) -> Policies:
    """Reads for up to `max_turns` turns and `max_usd_micros`; the last turn answers with no
    tools; a refused answer is asked once more. `accept(raw, state)` is the caller's check
    (agent_decision._accept against the corpus plus `state.rendered`)."""
    from ..services.agent_decision import ANSWER_NOW, _refused_again, parse

    return Policies(
        stop=Caps(max_steps=int(max_turns), answer="within", max_usd_micros=int(max_usd_micros),
                  on_cost="answer", answer_prompt_text=ANSWER_NOW,
                  answer_prompt_when="after_tool", version="stop.decision@1"),
        context=ClipContext(clip=int(result_chars), redact=True, version="ctx.decision@1"),
        tools=OfferAll(version="tools.decision@1"),
        verify=ParsedAnswer(parse=parse, accept=accept, reask_text=_refused_again, max_reasks=1,
                            version="verify.decision@1"),
        recovery=Recover(empty_retries=0, wait_mode="fail", version="recover.decision@1"))


# ── workflow_precheck_agent.run_agent ─────────────────────────────────────────────────────────

def precheck(*, max_turns: int = 8, max_usd_micros: int = 500_000,
             max_wait_seconds: float = 180.0) -> Policies:
    """Ends at the turn or cost cap (no answer forced by cost: it STOPS); the last affordable turn
    answers; a per-minute budget deferral is waited out in place, within `max_wait_seconds`."""
    from ..services import workflow_precheck_agent as pre

    def accept(items: Any, state: RunState) -> Acceptance:
        return Acceptance(ok=True, structured={"items": items})

    return Policies(
        stop=Caps(max_steps=int(max_turns), answer="within", max_usd_micros=int(max_usd_micros),
                  on_cost="stop", cost_lookahead=True, answer_prompt_text=pre.ANSWER_NOW,
                  answer_prompt_when="after_tool", version="stop.precheck@1"),
        context=ClipContext(clip=pre.MAX_TOOL_RESULT_CHARS, redact=True,
                            version="ctx.precheck@1"),
        tools=OfferAll(version="tools.precheck@1"),
        verify=ParsedAnswer(parse=pre.parse_answer, accept=accept, reask_text=pre.FORMAT_AGAIN,
                            max_reasks=pre.FORMAT_RETRIES, version="verify.precheck@1"),
        recovery=Recover(empty_retries=0, wait_mode="inline",
                         max_wait_seconds=float(max_wait_seconds), version="recover.precheck@1"))


# ── NativeCodeAgent._run_tool_loop (the scaffolded coder) ─────────────────────────────────────

def native_coder(max_turns: int = 40, *, deadline: Optional[float] = None) -> Policies:
    """Every turn offers the tools; no answer step (the diff is the answer); the sandbox tools'
    text goes to the model as it is. `deadline` is enforced by the run's CancelToken."""
    return Policies(
        stop=Caps(max_steps=int(max_turns), answer="none", version="stop.native_coder@1"),
        context=ClipContext(clip=None, raw_strings=True, version="ctx.native_coder@1"),
        tools=OfferAll(version="tools.native_coder@1"),
        verify=AcceptAny(),
        recovery=Recover(empty_retries=0, same_tool_failures=0,
                         version="recover.native_coder@1"))


__all__ = ["AGENT_LOOP_ANSWER", "AGENT_LOOP_FALLBACK", "ENGINE_AGENT_NUDGE", "agent_loop",
           "agent_loop_resume_note", "decision", "engine_agent", "native_coder", "precheck"]
