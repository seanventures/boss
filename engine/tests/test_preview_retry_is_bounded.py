"""A repeated failure is evidence, not a reason to try again.

One run produced 28 consecutive failed previews over four hours — every ~3 minutes, matching
the autopilot tick — each a full clone, npm install and build, every one the identical rollup
parse error. All of it on the single worker lane, while the operator's actual work queued
behind it.

Nothing was broken in isolation. `restart_preview` is genuinely the cheapest remedy and
genuinely idempotent, which is why it is tried first. The gap was that the ONLY thing
suppressing it was an open escalation, and a failing preview never created one. Cheap per
attempt, unbounded in aggregate.
"""

import inspect

from boss_engine.services import shepherd_service


def test_there_is_a_limit_at_all():
    assert shepherd_service.PREVIEW_MAX_ATTEMPTS >= 1


def test_the_limit_is_small_because_the_second_failure_is_the_evidence():
    assert shepherd_service.PREVIEW_MAX_ATTEMPTS <= 3, (
        "a third attempt has never told us anything the second did not")


def test_the_candidate_query_counts_failures_rather_than_taking_the_latest():
    """DISTINCT ON returned the most recent failure and discarded the fact that there were 27
    before it — the count IS the signal."""
    src = inspect.getsource(shepherd_service.autopilot_candidates) \
        if hasattr(shepherd_service, "autopilot_candidates") else _act_source()
    assert "count(*)" in src
    assert "PREVIEW_MAX_ATTEMPTS" in src


def _act_source() -> str:
    """The candidate query lives in whichever function assembles remedy candidates."""
    for name in dir(shepherd_service):
        obj = getattr(shepherd_service, name)
        if callable(obj) and getattr(obj, "__module__", "") == shepherd_service.__name__:
            try:
                src = inspect.getsource(obj)
            except (OSError, TypeError):
                continue
            if "app_previews p" in src and "shepherd_escalations" in src:
                return src
    raise AssertionError("no function assembles preview-failure candidates")


def test_exceeding_the_limit_escalates_instead_of_going_quiet():
    """Stopping silently would trade a retry storm for an app nobody is told is broken."""
    src = _act_source()
    # Every question goes through Shepherd's one door (shepherd_service.open_question).
    assert "open_question(" in src
    assert "PREVIEW_MAX_ATTEMPTS" in src


def test_the_escalation_suppresses_further_retries():
    """The open-escalation clause already existed; escalating is what finally engages it,
    so the loop closes itself rather than needing a second mechanism."""
    src = _act_source()
    assert "NOT EXISTS" in src and "shepherd_escalations" in src
    assert src.index("NOT EXISTS") < src.index("open_question(")


def test_the_operator_is_offered_something_other_than_retry():
    src = _act_source()
    assert "stop previewing" in src
    assert "fix task" in src
