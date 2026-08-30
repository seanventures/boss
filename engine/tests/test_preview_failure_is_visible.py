"""A preview that fails has to say so where people look.

Reported as "I can't find why the preview failed in the Activity Logs" — and the log was
right, because there was nothing to find. `app_preview.started` was emitted on success and
NOTHING was emitted on failure: a failed preview wrote its reason into `app_previews` and
stopped there. The Activity Log is a projection of events, so it cannot show what was never
emitted; an operator sent there to find out why saw agent turns and no preview at all.

The failure that prompted this had a perfectly good explanation sitting in the row the whole
time — failure_kind=port_timeout, "app did not serve HTTP within timeout" — which is exactly
the sentence that should have been one click away.
"""

import inspect

from boss_engine.services import preview_service


def test_both_failure_paths_emit_an_event():
    src = inspect.getsource(preview_service)
    # Host process and docker each have their own `_fail`; a fix to one is not a fix.
    assert src.count("def _fail(") == 2
    assert src.count('"app_preview.failed"') == 2


def test_the_event_carries_what_the_operator_needs_to_act():
    """"Preview failed" is not actionable. The kind, the suggested action and the error are."""
    src = inspect.getsource(preview_service)
    start = src.index('"app_preview.failed"')
    window = src[start:start + 700]
    for field in ('"failure_kind"', '"suggested_action"', '"error"', '"run_id"'):
        assert field in window, f"{field} missing from the failure event"


def test_the_event_is_written_in_the_same_transaction_as_the_row():
    """Otherwise a crash between the two leaves a failed preview with no trace, which is the
    state this exists to remove."""
    src = inspect.getsource(preview_service)
    bodies = []
    at = 0
    for _ in range(2):
        start = src.index("def _fail(", at)
        # The body ends at its `return`, which every _fail has.
        end = src.index("return {", start)
        bodies.append(src[start:end])
        at = end
    for body in bodies:
        assert "INSERT INTO app_previews" in body
        assert "app_preview.failed" in body
        # ONE transaction opened, covering both writes.
        assert body.count("with begin(engine)") == 1
