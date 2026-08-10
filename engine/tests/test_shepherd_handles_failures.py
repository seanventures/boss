"""The shepherd answers for failures, not only for stuck work.

A BugBox build was killed at seventeen minutes -- `claude code agent exited 143`, SIGTERM,
empty output, no gate reached. It stayed failed until a person noticed and asked why nothing
had caught it.

Three reasons, all structural:

  field of view   the sweep queried `status IN ('running','queued')`, so a failed run was
                  invisible to it. It was built to unstick live work.
  vocabulary      cancel / requeue / fail / escalate. There was no retry, and none of the
                  other three means anything to a run that is already terminal.
  classification  the failure taxonomy keyed off a list of KNOWN infrastructure strings, and
                  `exited 143` was not on it.

The last one mattered most and was the subtlest. An allowlist only recognises failures someone
has already seen, which is the wrong shape for a watchdog whose entire job is the unexpected:
the first occurrence of any new failure mode would be misfiled and left alone. So the default
is inverted -- an unclassified failure is treated as the platform's doing and retried.

Both kinds of failure are now acted on, for different reasons and with different bounds. A
platform kill reached no verdict, so the work is merely unfinished. A code verdict IS
actionable, because retry_code_task carries the failing gates forward and resumes on the
branch the attempt already wrote -- the next agent starts from the failure rather than meeting
it again. Nothing that fails is simply left; what changes is how many attempts it gets before
a person is asked.
"""

from __future__ import annotations

import pytest

from boss_engine.services.shepherd_watch import (
    KILLED_BY_PLATFORM,
    LIVE,
    MAX_PLATFORM_RETRIES,
    MAX_VERDICT_REPAIRS,
    REPAIRABLE_VERDICT,
    RunSnapshot,
    assess,
    mode_for,
)


def failed(error: str, retries: int = 0, **over) -> RunSnapshot:
    base = dict(
        run_id="crun_x", project_id="boss-dogfood", status="failed", phase="implement",
        title="Web app — BugBox", minutes_running=17.0, minutes_since_update=1.0,
        run_error=error, platform_retries=retries,
    )
    base.update(over)
    return RunSnapshot(**base)


# --------------------------------------------------------------------------------------
# The failure that prompted this
# --------------------------------------------------------------------------------------

def test_the_sigterm_that_killed_bugbox_is_retried():
    verdict = assess(failed("claude code agent exited 143: [substrate=local_subprocess]"))
    assert verdict.klass == KILLED_BY_PLATFORM
    assert verdict.action == "retry"
    assert verdict.autonomous is True
    assert verdict.evidence["platform_cause"] == "exited 143"


def test_an_unrecognised_failure_is_also_retried():
    """The inversion. An allowlist would leave every new failure mode sitting."""
    verdict = assess(failed("Kernel panic in the widget frobnicator"))
    assert verdict.action == "retry"
    assert verdict.evidence["platform_cause"] == "unclassified"


@pytest.mark.parametrize("error", [
    "claude code agent exited 137: killed",
    "claude code agent timed out after 2700s",
    "lease expired",
    "502 Bad Gateway",
    "connection reset by peer",
])
def test_the_named_infrastructure_causes_are_retried(error):
    assert assess(failed(error)).action == "retry"


# --------------------------------------------------------------------------------------
# A verdict about the code is REPAIRED, not filed
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    'claude code agent exited 1: {"subtype":"error_max_turns","num_turns":41}',
    "npm run verify: tests failed",
    "gate failed: acceptance criteria not met",
    "review refused: unsafe SQL construction",
    "merge conflict in src/App.tsx",
])
def test_a_verdict_about_the_code_is_repaired(error):
    """An earlier version returned "none" here, on the reasoning that a retry reruns the same
    thinking over the same code. That is wrong about what a retry IS: retry_code_task carries
    the failing gates forward and resumes on the branch the attempt wrote, so the next agent
    starts FROM the failure. Declining threw that away and left fixable work sitting."""
    verdict = assess(failed(error))
    assert verdict.action == "retry"
    assert verdict.klass == REPAIRABLE_VERDICT
    assert verdict.evidence["repair"] is True


def test_repair_is_bounded_more_tightly_than_an_infrastructure_retry():
    """A verdict surviving repeated repair is saying something a third attempt will not."""
    assert MAX_VERDICT_REPAIRS <= MAX_PLATFORM_RETRIES
    verdict = assess(failed("tests failed", retries=MAX_VERDICT_REPAIRS))
    assert verdict.action == "escalate"
    assert verdict.autonomous is False


def test_every_kind_of_failure_gets_a_response():
    """The property the whole change exists for: nothing that fails is simply left."""
    for error in ("exited 143", "tests failed", "something nobody has named",
                  "gate failed", "504 gateway timeout"):
        assert assess(failed(error)).action in {"retry", "escalate"}, error


# --------------------------------------------------------------------------------------
# Bounds -- an automatic remedy must not become an unbounded bill
# --------------------------------------------------------------------------------------

def test_repeated_platform_kills_escalate_instead_of_retrying_forever():
    """A box that OOMs once OOMs again. At some point the environment is the problem."""
    verdict = assess(failed("exited 143", retries=MAX_PLATFORM_RETRIES))
    assert verdict.action == "escalate"
    assert verdict.autonomous is False


def test_the_last_permitted_retry_still_happens():
    verdict = assess(failed("exited 143", retries=MAX_PLATFORM_RETRIES - 1))
    assert verdict.action == "retry"


@pytest.mark.parametrize("klass", [KILLED_BY_PLATFORM, REPAIRABLE_VERDICT])
def test_retrying_is_live_because_it_only_ever_creates_work(klass):
    """Cancel and fail destroy; a retry cannot. Its worst case is bounded waste."""
    assert mode_for(klass) == LIVE


# --------------------------------------------------------------------------------------
# Field of view and vocabulary
# --------------------------------------------------------------------------------------

def test_the_sweep_looks_at_failed_runs():
    """The reason none of the above would ever have run."""
    import inspect

    from boss_engine.services import shepherd_service

    query = inspect.getsource(shepherd_service._inflight_snapshots)
    assert "status = 'failed'" in query
    assert "interval" in query, "the failure window must be bounded"


def test_retry_is_an_action_the_shepherd_can_take():
    from boss_engine.services.shepherd_watch import AUTONOMOUS_ACTIONS

    assert "retry" in AUTONOMOUS_ACTIONS


def test_a_live_run_is_still_judged_the_old_way():
    """Widening the query must not change how in-flight work is assessed."""
    healthy = RunSnapshot(
        run_id="crun_live", project_id="p", status="running", phase="implement",
        title="t", minutes_running=12.0, minutes_since_update=2.0,
        has_live_worker=True, minutes_since_heartbeat=0.4, job_status="running",
        has_job=True, was_leased=True,
    )
    assert assess(healthy).action == "none"
