"""A capability with no await table must not be asked for an await handle.

`deploy` is declared is_async — it takes a while — but there is no table to wait on a
deployment in, so it has no entry in `_AWAIT_KINDS`. The "an async step that cannot be
correlated has not succeeded" guard indexed `_AWAIT_KINDS[key]` unconditionally while building
its error message, and raised KeyError('deploy') on every deploy step.

That exception escaped the handler that releases leases, so five deploy steps sat `leased` for
fifteen hours showing the operator a running timer, and the only clue was a log line reading
`(non-fatal): 'deploy'`.

Two lessons, both encoded below: a guard must not assume every async capability is awaitable,
and every async capability that IS awaitable must have a spec — otherwise the guard silently
stops applying to it.
"""

from __future__ import annotations

import pytest

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import CAPABILITIES


ASYNC_KEYS = sorted(k for k, c in CAPABILITIES.items() if c.is_async)


def test_deploy_is_async_but_has_no_await_spec():
    """The exact combination that broke: the guard has to tolerate it."""
    assert CAPABILITIES["deploy"].is_async is True
    assert "deploy" not in runner._AWAIT_KINDS


@pytest.mark.parametrize("key", ASYNC_KEYS)
def test_the_guard_never_indexes_a_missing_await_spec(key):
    """Every async capability, not just the one that happened to be reported."""
    if key in runner._AWAIT_KINDS:
        return
    # The error-message branch must be unreachable for a capability with no spec.
    import inspect

    source = inspect.getsource(runner.execute_step)
    guard = source[source.index("if (capability.is_async"):]
    assert "key in _AWAIT_KINDS" in guard[:400], (
        f"{key} is async with no await spec; indexing _AWAIT_KINDS[key] raises KeyError"
    )


def test_every_await_kind_names_a_real_capability():
    """The other direction: a spec for a capability that does not exist is dead configuration."""
    for key in runner._AWAIT_KINDS:
        assert key in CAPABILITIES, f"_AWAIT_KINDS names {key!r}, which is not a capability"


def test_awaitable_capabilities_are_all_async():
    """A spec on a synchronous capability would never be consulted."""
    for key in runner._AWAIT_KINDS:
        assert CAPABILITIES[key].is_async, (
            f"{key} has an await spec but is not async, so nothing will ever park on it"
        )
