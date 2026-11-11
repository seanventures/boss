"""ONE CLI SESSION PER CHAT TURN (BOSSAPP-1006 + 1007).

On a CLI text brain every chat round-trip used to be its own `claude -p` process, re-paying the
CLI's ~34k-token preamble plus the WHOLE rendered conversation and every tool schema, per step
(audit BOSSAPP-997 §1a: ~$0.95 a call; "what can you do?" cost $3.99). The coding pipeline has
resumed its fix rounds for exactly this reason (`code_agent._invoke` passes `--resume`); chat
never did.

This module is the turn's session state, carried on a contextvar so it reaches the adapter
through every wrapper (Recorded/Failover/Governed) without changing a single call signature —
which is the point: the recording seam and the per-project call-class gate see every call
exactly as before, because the calls themselves do not change, only what each one carries.

Shape of a turn with the scope open, on an adapter that can resume (`_chat_resume`):

  * the turn's FIRST CLI call — the router's turn decision (`create_text_response`), or the
    loop's first step when there is no decision — STARTS the session and records its id. That
    is the merged decision call of BOSSAPP-1006: the decision is the first exchange of the
    turn's one session instead of a session of its own, the same merge the kernel's decision
    reading proved (agent_decision._ask_reading_on_kernel: decide and read in ONE run).
  * every later `create_chat_completion` of the turn RESUMES that session and sends only the
    delta — new tool results, new user notices, schemas of newly found tools — instead of the
    whole conversation again. The preamble and the history ride the provider's cache.
  * a NEW turn opens a new scope, so it starts fresh (`turn_scope` below).
  * a resume that fails falls back to today's per-step behaviour for the rest of the turn
    (`TurnSession.broken`), and an engine that cannot resume never enters any of this.

Nothing here talks to a CLI: the adapter (claude_cli_adapter.CliChatBrain) reads this state and
does the resuming. An open scope with no capable adapter is inert.
"""
from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

__all__ = ["TurnSession", "current", "in_turn_scope", "turn_scope"]


@dataclass
class TurnSession:
    """What one chat turn's CLI session holds, mutated by the adapter as calls are made."""

    #: The CLI's own session id (the result JSON's `session_id`). The CLI issues a NEW id on
    #: every resume, so the adapter re-reads it after each call and always resumes the latest.
    session_id: Optional[str] = None
    #: id() of the message LIST whose steps the session is currently following. EngineAgent
    #: mutates one list through a pass, so identity is the cheap, reliable conversation key; a
    #: different list (a later pass of the same turn, or a nested conversation) resumes with
    #: its full prompt instead of a delta and takes ownership.
    owner_convo: Optional[int] = None
    #: How many of the owner conversation's messages are already inside the session.
    sent_messages: int = 0
    #: Tool names whose schemas the session has already been shown.
    sent_tools: set = field(default_factory=set)
    #: Why resuming stopped for this turn ("" while it works). Once set, every remaining call
    #: of the turn runs per-step, exactly as before this module existed.
    broken: str = ""
    #: id() of the adapter that owns the session. A turn that somehow switches adapters
    #: (failover built a new backend) must not hand one engine's session id to another.
    owner_adapter: Optional[int] = None
    #: Observability for tests and debugging: how many calls started fresh vs resumed.
    fresh_calls: int = 0
    resumed_calls: int = 0


_TURN: ContextVar[Optional[TurnSession]] = ContextVar("cli_chat_turn_session", default=None)


def current() -> Optional[TurnSession]:
    """The open turn's session state, or None outside any turn scope."""
    return _TURN.get()


@contextmanager
def turn_scope() -> Iterator[TurnSession]:
    """One chat turn. The OUTERMOST scope owns the turn: the router opens it around the whole
    of `route_turn` (decision + every answer pass), and EngineAgent.chat opens one only when
    none is open (a direct caller — MCP, tests), so nesting never splits a turn in two."""
    existing = _TURN.get()
    if existing is not None:
        yield existing
        return
    state = TurnSession()
    token = _TURN.set(state)
    try:
        yield state
    finally:
        _TURN.reset(token)


def in_turn_scope(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn`, run inside `turn_scope`. functools.wraps keeps the wrapped function's signature
    AND its source visible to inspect (both follow __wrapped__), so the contract tests that
    read `route_turn`'s and `EngineAgent.chat`'s bodies keep reading the real ones."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with turn_scope():
            return fn(*args, **kwargs)

    return wrapper
