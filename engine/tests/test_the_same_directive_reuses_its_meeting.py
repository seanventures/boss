""""ValueError: Task with dedupe_key already exists in this project: prd-meeting:e516b325…"

A run deadlettered at Convene a panel, on a directive whose earlier run had been cancelled.

The dedupe key is right and the intent behind it is right: same project, same directive, same
meeting — `make_id()` there once meant a double-submitted convene paid for a second identical
panel. What was missing is that the key OUTLIVES the meeting it was made for.
`active_duplicate_meeting` catches a panel that is still going and hands it back, which is the
whole idempotency story while a run is live. Once that run is cancelled there is no active
duplicate to find, and the task row it created still holds the key — so the next run on the
same directive is refused by the very mechanism that exists to say "this is the same work".

"Stop and run again" walks into it every time, because stopping and re-running one directive
is exactly that case.

So the run adopts the task instead of colliding with it. Refusing stays correct for an
accidental double-submit through `create_task`; a caller that MEANT "this is the same piece of
work" can now say so.
"""
from __future__ import annotations

import inspect

from boss_engine.services import meeting_service
from boss_engine.services.core import stable_dedupe_key
from boss_engine.services.intake import CommandIntakeService


def test_the_key_is_still_an_identity():
    """Unchanged, and the reason this bug is worth fixing carefully rather than by removing
    the key: two panels on one directive is the failure it was added for."""
    assert (stable_dedupe_key("prd-meeting", "p", "Build LinkNest")
            == stable_dedupe_key("prd-meeting", "p", "  Build LinkNest\n"))
    assert (stable_dedupe_key("prd-meeting", "p", "a")
            != stable_dedupe_key("prd-meeting", "p", "b"))


def test_a_run_adopts_the_task_that_already_has_its_identity():
    source = inspect.getsource(meeting_service)
    assert "intake.task_by_dedupe_key(connection, project_id, dedupe_key)" in source
    assert "task_id = existing_task" in source


def test_and_only_creates_one_when_there_is_none():
    source = inspect.getsource(meeting_service)
    at = source.index("existing_task = intake.task_by_dedupe_key")
    after = source[at:at + 900]
    assert "else:" in after
    assert "intake.create_task(" in after


def test_the_lookup_is_the_one_create_task_already_did():
    """Not a second way of asking the same question — that is how two answers drift apart."""
    lookup = inspect.getsource(CommandIntakeService.task_by_dedupe_key)
    create = inspect.getsource(CommandIntakeService.create_task)
    for fragment in ("project_id = :project_id", "dedupe_key = :dedupe_key"):
        assert fragment in lookup, fragment
        assert fragment in create, fragment


def test_an_empty_key_adopts_nothing():
    """A blank dedupe key means "no identity", and matching every task with no key would hand
    a run somebody else's work."""
    source = inspect.getsource(CommandIntakeService.task_by_dedupe_key)
    assert "if not dedupe_key:" in source
    assert "return None" in source


def test_create_task_still_refuses_a_genuine_duplicate():
    """The protection is unchanged for callers that did not ask to adopt: pressing "Send to
    PRD meeting" twice must still not produce two meetings."""
    source = inspect.getsource(CommandIntakeService.create_task)
    assert "Task with dedupe_key already exists in this project" in source
    assert "IntegrityError" in source


def test_a_live_panel_is_still_returned_rather_than_restarted():
    """Adoption is for a task whose run is gone. A run still going is caught earlier and comes
    back as a duplicate, which costs nothing."""
    source = inspect.getsource(meeting_service)
    assert "active_duplicate_meeting(connection, project_id, fingerprint)" in source
    assert "meeting.duplicate_suppressed" in source
