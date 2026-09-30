"""The router's self-correction paths must actually execute.

Two quality features in `IntentRouter._answer` re-ask the answer agent:

- a **grounding re-ask** when a role answered without reading the project record;
- an **action-claim reconcile** when the reply claims a change that no tool made.

Both called `self.agent.chat(...)`. `IntentRouter` has no `agent` attribute, so both
raised `AttributeError` on every turn — inside `except Exception: ... = None`. The features
were dead, and the bare except is why nobody found out. (They also referenced an `actor`
name that was never a parameter of `_answer`.)

These tests pin the three things that were wrong: the agent object is reachable, the actor
is threaded in, and a failure is logged rather than swallowed in silence.
"""

from __future__ import annotations

import inspect

from boss_engine.services import intent_router
from boss_engine.services.intent_router import IntentRouter


def test_answer_takes_an_actor_so_the_reask_can_identify_itself() -> None:
    params = inspect.signature(IntentRouter._answer).parameters
    assert "actor" in params, "_answer must accept the actor it answers on behalf of"
    assert params["actor"].kind is inspect.Parameter.KEYWORD_ONLY


def test_every_answer_call_site_passes_the_actor_through() -> None:
    source = inspect.getsource(IntentRouter)
    call_sites = [ln for ln in source.splitlines() if "self._answer(" in ln]
    assert call_sites, "expected _answer to be called"
    for line in call_sites:
        assert "actor=actor" in line, f"actor not threaded at: {line.strip()}"


def test_the_reask_targets_an_agent_that_exists() -> None:
    source = inspect.getsource(IntentRouter)
    assert "self.agent.chat(" not in source, (
        "IntentRouter has no `agent` attribute — this raises AttributeError on every turn"
    )
    # Two call sites predate this fix and already used the lazy agent — that is what
    # made the two broken ones identifiable as a slip rather than a different design.
    assert source.count("self._answer_agent().chat(") >= 4, (
        "both the grounding re-ask and the action-claim reconcile must use the lazy "
        "answer agent, alongside the call sites that already did"
    )


def test_the_router_really_has_no_agent_attribute() -> None:
    """The guard above is only meaningful while this stays true."""
    from boss_engine.config import Settings

    router = IntentRouter(Settings())
    assert not hasattr(router, "agent")
    assert hasattr(router, "_answer_agent")


def test_a_failed_reask_is_logged_not_swallowed(caplog) -> None:
    """`except Exception: x = None` is how a permanently broken path stays invisible."""
    source = inspect.getsource(IntentRouter)
    assert "reread = None" in source and "again = None" in source
    for marker in ("grounding re-ask failed", "action-claim reconcile failed"):
        assert marker in source, f"missing visible failure log: {marker}"


def test_the_answer_agent_accepts_the_kwargs_the_reask_sends() -> None:
    """The call sites pass history/system/allow_writes/max_steps — prove that is the
    interface `_answer_agent()` returns, not a lookalike."""
    from boss_engine.agent.engine_agent import EngineAgent

    params = inspect.signature(EngineAgent.chat).parameters
    for expected in ("project_id", "actor", "message", "history", "system",
                     "allow_writes", "max_steps"):
        assert expected in params, f"EngineAgent.chat is missing {expected}"
