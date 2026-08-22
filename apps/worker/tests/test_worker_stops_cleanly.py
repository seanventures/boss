"""A deploy should interrupt the worker, not surprise it.

The worker installed no signal handler at all, so `docker compose up --recreate` — the only
supported way to deploy — sent SIGTERM and Python's default terminated the process instantly,
mid-job, with nothing logged about what was interrupted. Every deploy in this session had to be
held until nothing was running, because there was no way to tell what a deploy would cost.

Deliberately NOT in scope: handing the running job back from the signal handler. The lease
reclaimer already covers that — the lease expires within one TTL and the job is claimed again —
and interrupting a main thread blocked deep inside execute_job to save those minutes is how the
next incident gets written. See R12 in the resiliency PRD.
"""

import inspect
import signal

from apps.worker.boss_worker import main as worker_main


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
    src = inspect.getsource(worker_main.main)
    assert "for _ in range(max(1, int(settings.poll_interval_seconds)))" in src, (
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
