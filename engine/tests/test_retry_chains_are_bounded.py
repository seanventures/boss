"""A bound must key on something the remedy cannot mint a fresh copy of.

`shepherd_ledger` refuses to apply the same remedy to the same run twice:

    WHERE task_ref = :t AND status = 'used'

and `retry_code_task` creates a NEW run, with a NEW id, for every attempt. So the child is a
fresh `task_ref` with an empty ledger, the same remedy applies again, and the chain has no end.
Bounded per instance, unbounded per lineage (S13).

Unlike the preview loop, this one SPENDS: $4-15 a link, unattended. `MAX_ACTIONS_PER_TICK`
bounds the RATE, not the total. The only thing that stopped it in practice is that autopilot
defaults to off — a feature flag standing in for a limit.

There was already a circuit breaker. It reads:

    elif prev_infra and prev_substrate == "local_subprocess" and attempt >= 3 and not fix_note:

`not fix_note` is the escape hatch for a human saying "I know, proceed" — and the shepherd
passes `fix_note` on EVERY retry, so for automated repair the condition is always False and the
breaker under it can never fire. The escape built for a person was taken by the machine.
"""

import inspect

from boss_engine.services import shepherd_service

from ._source import code_only
from boss_engine.services.code_task_service import retry_code_task


def test_the_bound_exists_and_is_small():
    assert 1 <= shepherd_service.SHEPHERD_MAX_LINEAGE_ATTEMPTS <= 5


def test_the_ceiling_can_be_imposed_by_the_caller():
    """An automated repair and an operator pressing retry are the SAME function. Only one of
    them should be stopped by a limit it cannot argue with, so the caller states the ceiling."""
    assert "attempt_ceiling" in inspect.signature(retry_code_task).parameters


def test_the_bound_is_on_lineage_not_on_the_run_id():
    """`attempt` is carried in finalize_ctx across the whole lineage and cannot be reset by
    minting a new run. A bound on `run_id` is exactly the defect."""
    src = inspect.getsource(retry_code_task)
    assert "attempt > attempt_ceiling" in src
    assert 'attempt = int(fctx_prev.get("attempt", 1)) + 1' in src


def test_the_ceiling_is_checked_before_the_fix_note_escape():
    """The pre-existing breaker is bypassed by any fix_note. The new ceiling must not sit behind
    the same escape, or it inherits the bug it exists to fix."""
    src = code_only(retry_code_task)
    assert src.index("attempt_ceiling is not None") < src.index("not fix_note")


def test_the_ceiling_is_not_itself_bypassable_by_a_fix_note():
    """The whole point: the shepherd supplies a fix_note every time, so any guard standing
    behind `not fix_note` is dead code for automated repair."""
    src = code_only(retry_code_task)
    block = src[src.index("attempt_ceiling is not None"):]
    block = block[:block.index("prev_infra =")]
    assert "fix_note" not in block, (
        "the ceiling must not consult fix_note; that escape is what the machine always takes")


def test_the_shepherd_passes_the_ceiling_explicitly():
    src = inspect.getsource(shepherd_service.act)
    assert "attempt_ceiling=SHEPHERD_MAX_LINEAGE_ATTEMPTS" in src


def test_hitting_the_ceiling_escalates_rather_than_going_quiet():
    """A bound that just stops is indistinguishable from a bound that never fired."""
    src = inspect.getsource(shepherd_service.act)
    assert "_escalate_exhausted_lineage" in src
    assert "except ValueError" in src


def test_the_escalation_is_what_makes_the_bound_stick():
    """`sweep` skips runs with an open escalation, so escalating removes the run from the
    candidate set instead of it being reconsidered on every 180s tick."""
    esc = inspect.getsource(shepherd_service._escalate_exhausted_lineage)
    assert "open_question(" in esc
    # The one door refuses a second open question about the same build.
    door = inspect.getsource(shepherd_service.open_question)
    assert "task_ref = :t" in door and "status = 'open'" in door, \
        "a second escalation per tick is the loop wearing a new hat"
    sweep = inspect.getsource(shepherd_service.sweep)
    assert "SWEEP_CANDIDATES_SQL" in sweep
    candidates = shepherd_service.SWEEP_CANDIDATES_SQL
    assert "shepherd_escalations" in candidates and "status = 'open'" in candidates


def test_the_refusal_says_what_was_tried_and_what_failed():
    """'Ceiling reached' with no evidence sends the operator back to the database."""
    src = inspect.getsource(retry_code_task)
    seg = src[src.index("retry ceiling reached"): src.index("retry ceiling reached") + 700]
    assert "root" in seg and "attempt" in seg
    assert "prev.get('error')" in seg or 'prev.get("error")' in seg


def test_no_automated_caller_retries_without_a_ceiling():
    """The shape guard: any shepherd-initiated retry must state a ceiling. A new automated
    caller that forgets one reintroduces the unbounded chain."""
    src = inspect.getsource(shepherd_service)
    for block in src.split("retry_code_task(")[1:]:
        head = block[:400]
        if "job_priority=JOB_PRIORITY_BACKGROUND" in head:
            assert "attempt_ceiling" in head, (
                "an automated (BACKGROUND) retry with no ceiling is the unbounded chain:\n"
                + " ".join(head.split())[:160])
