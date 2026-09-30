"""Plane sync's lazy imports have to actually resolve.

`on_event` is wrapped in `except Exception` so a Plane or DB hiccup can never break the
pipeline. That guard also swallowed a plain typo: `from .db import begin` names
`boss_engine.services.db`, which does not exist — the module is `boss_engine.db`. Every
code-task lifecycle transition therefore failed with "No module named
'boss_engine.services.db'" and returned `{"synced": False}`, and because the guard did its job
the pipeline carried on and nobody was told. Plane is the system of record for this work, so
"silently never synced" is the worst possible way for it to fail.

An ImportError inside a broad guard is indistinguishable from the outage the guard is for.
These tests separate the two.
"""

from __future__ import annotations

import pytest

from boss_engine.services import plane_sync


@pytest.mark.parametrize("event_type", sorted(plane_sync._EVENT_STATE))
def test_no_mapped_event_fails_on_a_missing_module(event_type):
    """Every mapped event runs the same import block. A failure here must be about Plane or
    the database being unreachable — never about the code not importing."""
    result = plane_sync.on_event(None, None, "crun_does_not_exist", event_type)
    reason = str(result.get("reason") or "")
    assert "No module named" not in reason, f"{event_type} still cannot import: {reason}"
    assert not isinstance(result.get("reason"), ModuleNotFoundError)


def test_the_module_it_imports_is_the_one_that_exists():
    """The specific typo: `services.db` vs `db`."""
    from boss_engine.db import begin  # noqa: F401 — the import IS the assertion

    with pytest.raises(ModuleNotFoundError):
        __import__("boss_engine.services.db")


def test_an_unmapped_event_is_skipped_without_importing_anything():
    """The early return predates the imports and must keep doing so — an event Plane does not
    model should cost nothing."""
    assert plane_sync.on_event(None, None, "crun_x", "code_task.something_else") == {
        "synced": False, "reason": "unmapped"}


def test_sync_still_never_raises_at_the_caller():
    """The guard stays. Fixing the import must not turn a Plane outage into a failed build."""
    result = plane_sync.on_event(object(), object(), "crun_x", "code_task.started")
    assert result["synced"] is False
    assert "reason" in result
