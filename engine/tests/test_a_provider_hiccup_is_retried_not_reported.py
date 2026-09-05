"""A 529 is the provider saying "not now", not an answer.

Anthropic returns 529 `overloaded_error` when its capacity is saturated. The coding pipeline
has always treated that as transient and retried it — code_agent's transient list names "529"
explicitly. The CHAT path did not: any exception became `failure`, and the operator was told
"I could not answer that: the model returned an empty answer", which reads as a fault in BOSS
and leaves them to notice and ask again.

The chat agent is the only interface a customer has. Handing them a provider's load-shedding as
a dead end is the difference between a product that absorbs a blip and one that stops working
whenever someone else is busy.
"""

from __future__ import annotations

import pytest

from boss_engine.agent.transient import _looks_transient, _with_transient_retry


class TestWhatCountsAsAHiccup:
    @pytest.mark.parametrize("message", [
        "API error 529 overloaded_error",
        "Error code: 529 - overloaded",
        "503 Service Unavailable",
        "upstream connect error: bad gateway",
        "connection reset by peer",
        "read timed out",
    ])
    def test_provider_capacity_and_transport_are_transient(self, message):
        assert _looks_transient(RuntimeError(message))

    @pytest.mark.parametrize("message", [
        "invalid_request_error: max_tokens too large",
        "authentication_error: invalid x-api-key",
        "permission denied for project",
        "KeyError: 'run_id'",
    ])
    def test_a_real_error_is_not_retried(self, message):
        """Retrying a bad request spends the operator's wait twice and still fails."""
        assert not _looks_transient(ValueError(message))


class TestTheRetry:
    def test_it_succeeds_on_a_later_attempt(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("529 overloaded_error")
            return "the answer"

        assert _with_transient_retry(flaky, attempts=3, backoff=0.01) == "the answer"
        assert calls["n"] == 3

    def test_it_gives_up_and_raises_the_real_error(self):
        def always_busy():
            raise RuntimeError("529 overloaded_error")

        with pytest.raises(RuntimeError, match="529"):
            _with_transient_retry(always_busy, attempts=2, backoff=0.01)

    def test_a_non_transient_error_is_raised_immediately(self):
        calls = {"n": 0}

        def bad_request():
            calls["n"] += 1
            raise ValueError("invalid_request_error")

        with pytest.raises(ValueError):
            _with_transient_retry(bad_request, attempts=3, backoff=0.01)
        assert calls["n"] == 1, "a bad request must not be tried again"

    def test_a_first_time_success_is_not_delayed(self):
        assert _with_transient_retry(lambda: "fast", attempts=3, backoff=99) == "fast"


def test_the_model_call_is_retried_not_the_whole_turn():
    """A 529 retries the completion. Retrying chat() itself would replay writes."""
    import inspect
    from pathlib import Path

    from boss_engine.agent.engine_agent import EngineAgent

    assert "_with_transient_retry" in inspect.getsource(EngineAgent.chat)
    import boss_engine.agent.engine_agent as agent_mod
    router = Path(agent_mod.__file__).resolve().parents[1] / "services" / "intent_router.py"
    assert "_with_transient_retry(lambda: self._answer_agent().chat" not in router.read_text()
