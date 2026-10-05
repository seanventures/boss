"""A deploy should interrupt the worker, not surprise it.

The worker installed no signal handler at all, so `docker compose up --recreate` — the only
supported way to deploy — sent SIGTERM and Python's default terminated the process instantly,
mid-job, with nothing logged about what was interrupted. Every deploy in this session had to be
held until nothing was running, because there was no way to tell what a deploy would cost.

HANDING THE JOB BACK WAS ORIGINALLY OUT OF SCOPE. That decision is now reversed, because the
cost was measured rather than estimated. R12 argued the lease reclaimer already covers it — the
lease expires within one TTL and the job is claimed again — and that reaching into a main
thread blocked deep inside execute_job to save those minutes is how the next incident gets
written.

The first half held; the second did not. On a real run, a deploy at 18:33 killed an implement
stage and the round did not restart until 18:38:28 — exactly one 300s TTL of a live worker
sitting idle next to a job nobody was serving, on top of the ~20 minutes of in-flight work the
SIGKILL threw away. Repeated across four deploys it produced two phases reported as 27m46s and
27m39s in which nothing whatsoever happened.

The objection is answered without touching the main thread: the lease-heartbeat thread already
runs independently, already holds its own client, and already knows the job and lease token. It
re-heartbeats with a few-seconds TTL, which expires the lease almost at once. It does NOT call
fail() — being interrupted by a deploy is not the job's fault and must not consume a retry
attempt.
"""

import inspect
import signal
import threading

import pytest

from apps.worker.boss_worker import main as worker_main
from apps.worker.boss_worker import runner


def test_sigterm_is_handled_at_all():
    src = inspect.getsource(worker_main._install_signal_handlers)
    assert "SIGTERM" in src


def test_sigint_too_so_local_runs_behave_the_same_way():
    src = inspect.getsource(worker_main._install_signal_handlers)
    assert "SIGINT" in src


def test_the_loop_actually_checks_the_flag():
    """A flag nothing reads is not a shutdown."""
    src = inspect.getsource(worker_main.main)
    assert "while not _STOPPING" in src


def test_an_idle_worker_does_not_hold_the_deploy_open_for_a_poll_interval():
    # The idle wait is one-second steps that stop when asked (worker_main.idle_wait, which
    # the loop calls through observe_idle_wait so the stall clock times only the wait).
    assert "observe_idle_wait(" in inspect.getsource(worker_main.main)
    slept: list[float] = []
    planned, _ = worker_main.idle_wait(20.0, stopping=lambda: len(slept) >= 2,
                                       sleep=slept.append, clock=lambda: 0.0)
    assert slept == [1, 1] and planned == 2.0, (
        "a single long sleep cannot be interrupted, so the deploy waits it out")


def test_requesting_a_stop_sets_the_flag():
    worker_main._STOPPING = False
    try:
        worker_main._request_stop(signal.SIGTERM, None)
        assert worker_main._STOPPING is True
    finally:
        worker_main._STOPPING = False


def test_installing_handlers_never_raises_off_the_main_thread():
    """Tests and embedded runs call main() from worker threads; signal.signal raises there.
    A worker that refuses to start because it could not install a handler is strictly worse."""
    src = inspect.getsource(worker_main._install_signal_handlers)
    assert "except (ValueError, OSError)" in src


def test_handlers_are_installed_before_the_loop_is_entered():
    src = inspect.getsource(worker_main.main)
    assert src.index("_install_signal_handlers()") < src.index("while not _STOPPING")


def test_stopping_is_announced():
    """A worker that exits silently is indistinguishable from one that crashed."""
    assert "worker_stopped" in inspect.getsource(worker_main.main)
    assert "worker_stop_requested" in inspect.getsource(worker_main._request_stop)


# ── Handing the running job back ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_shutdown():
    """SHUTDOWN is module state. A test that leaves it set makes every later one lie."""
    runner.SHUTDOWN.clear()
    yield
    runner.SHUTDOWN.clear()


def test_the_signal_handler_tells_the_runner_too():
    """`_STOPPING` is read by main's own loop, which is not where the running job lives."""
    worker_main._STOPPING = False
    try:
        worker_main._request_stop(signal.SIGTERM, None)
        assert runner.SHUTDOWN.is_set(), "the thread that can act on a stop was never told"
    finally:
        worker_main._STOPPING = False


def test_the_hand_back_ttl_is_short_but_not_zero():
    """Zero (or a lease already in the past) could race the reclaimer against a worker that is
    still, for another instant, alive — which is how two workers end up on one job."""
    assert 0 < runner.SHUTDOWN_LEASE_TTL_SECONDS <= 15


def test_the_hand_back_is_much_shorter_than_the_ttl_it_replaces():
    from apps.worker.boss_worker.config import WorkerSettings

    assert runner.SHUTDOWN_LEASE_TTL_SECONDS < WorkerSettings().lease_ttl_seconds / 10


def test_it_shortens_the_lease_rather_than_failing_the_job():
    """`fail()` consumes a retry attempt and can deadletter the job or fail the code task.
    A deploy is not the job's fault."""
    src = inspect.getsource(runner.run_worker_cycle)
    hand_back = src[src.index("def _hand_back_on_shutdown"):src.index("def _renew_lease")]
    assert "heartbeat(" in hand_back
    assert "SHUTDOWN_LEASE_TTL_SECONDS" in hand_back
    assert ".fail(" not in hand_back, "a deploy must not burn one of the job's attempts"


def test_a_failed_hand_back_still_leaves_the_ttl_as_the_backstop():
    src = inspect.getsource(runner.run_worker_cycle)
    hand_back = src[src.index("def _hand_back_on_shutdown"):src.index("def _renew_lease")]
    assert "except Exception" in hand_back


def test_the_renewal_loop_notices_a_stop_without_waiting_out_its_interval():
    """The renewal interval is half a lease TTL (150s by default) and Docker SIGKILLs about
    10s after SIGTERM. An unsliced wait would mean the process is dead long before the loop
    next looked at anything, and the hand-back would never run."""
    src = inspect.getsource(runner.run_worker_cycle)
    assert "_wait_in_slices(hb_stop" in src
    assert "SHUTDOWN.is_set()" in src


# ── The sliced wait ────────────────────────────────────────────────────────────

def test_a_finished_job_reports_true_so_renewal_simply_stops():
    stop = threading.Event()
    stop.set()
    assert runner._wait_in_slices(stop, 5) is True


def test_a_shutdown_reports_false_so_the_caller_can_hand_the_job_back():
    """The two exits must be distinguishable: one means the work is done, the other means the
    work is being abandoned and somebody else has to pick it up."""
    stop = threading.Event()
    runner.SHUTDOWN.set()
    assert runner._wait_in_slices(stop, 5) is False


def test_it_returns_promptly_on_shutdown_rather_than_sleeping_the_full_interval():
    import time as _t

    stop = threading.Event()
    threading.Timer(0.05, runner.SHUTDOWN.set).start()
    began = _t.monotonic()
    runner._wait_in_slices(stop, 30, slice_s=0.02)
    assert _t.monotonic() - began < 5, "a stop waited out the whole renewal interval"


def test_an_ordinary_elapsed_wait_reports_false_and_renewal_continues():
    stop = threading.Event()
    assert runner._wait_in_slices(stop, 0.03, slice_s=0.01) is False


# ── Saying that work was thrown away ───────────────────────────────────────────

def test_a_redelivered_job_says_the_previous_attempt_lost_its_work():
    """Otherwise the console shows "Engineer implementing 27m" for a stage that was killed at
    minute 20 and started over, with nothing anywhere saying so. WARNING, so the Activity Log
    keeps it without depending on an allowlist."""
    src = inspect.getsource(runner.run_worker_cycle)
    assert "job.attempt > 1" in src
    redelivery = src[src.index("job.attempt > 1"):src.index('"job_leased worker_id')]
    assert "LOGGER.warning" in redelivery
    assert "interrupted" in redelivery
