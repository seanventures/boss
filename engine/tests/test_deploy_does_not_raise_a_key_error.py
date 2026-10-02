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

A third, from the review of the failed-step change (2026-10-02): an async capability with NO
spec is itself the defect. Deploy went green on "planned" because nothing waited, so deploy now
waits on its deployment run and the catalogue refuses an async capability without a spec.
"""

from __future__ import annotations

import pytest

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import CAPABILITIES


def test_deploy_is_async_and_now_waits_on_its_deployment():
    """REWRITTEN to the new rule. This asserted the combination that broke — deploy async with
    no await spec — so the guard had to tolerate it. Tolerating it was the second bug: with
    nothing to wait on, deploy went green on `{"status": "planned"}` before anything shipped.
    Deploy now waits on its deployment run, and every async capability has an await spec
    (`test_every_async_capability_is_awaited`)."""
    assert CAPABILITIES["deploy"].is_async is True
    assert runner._AWAIT_KINDS["deploy"][0] == "deployment"


def test_the_guard_never_indexes_a_missing_await_spec():
    """The original lesson still holds for the guard itself: a capability with no await spec —
    there are none in the catalogue now, but a guard must not assume — settles without a
    KeyError rather than stranding a leased step for fifteen hours."""
    import dataclasses

    phantom = dataclasses.replace(CAPABILITIES["deploy"], key="phantom_async")
    assert "phantom_async" not in runner._AWAIT_KINDS
    assert runner._settle_started_work(
        phantom, "phantom_async", {"status": "accepted", "tool": "test_tool"},
    ) == {"status": "accepted", "tool": "test_tool"}


@pytest.mark.parametrize("key", sorted(runner._AWAIT_KINDS))
def test_an_awaitable_step_without_a_handle_fails_with_a_reason(key):
    result = runner._settle_started_work(
        CAPABILITIES[key], key, {"status": "accepted", "tool": "test_tool"},
    )
    assert result["status"] == "failed"
    assert "returned no id to wait on" in result["error"]


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
