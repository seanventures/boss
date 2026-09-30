"""Starting a preview must not be a 120-second guess.

Cloning, installing dependencies, building and launching took minutes and was done inside the
request. The console gives up at 120s, so the operator saw "the control plane did not answer
within 120s" under a heading reading "Preview / tests failed" — while the build was still
running and would often go on to succeed. Until the attempt finished there was no row at all:
nothing in the previews list, nothing in the Activity Log, nothing to poll. Blind by
construction.
"""

import inspect

from boss_engine.services import preview_service
from boss_engine.services.deployment_steps import StepRecorder


def test_the_row_exists_before_the_work_starts():
    src = inspect.getsource(preview_service.start_preview_async)
    insert_at = src.index("INSERT INTO app_previews")
    thread_at = src.index("threading.Thread(")
    assert insert_at < thread_at, "the row must exist before the build is handed to a thread"
    assert "'starting'" in src[:thread_at]
    # `starting` was already a documented status that nothing used.
    assert "app_preview.starting" in src


def test_it_returns_without_waiting_for_the_build():
    src = inspect.getsource(preview_service.start_preview_async)
    assert "daemon=True" in src
    assert 'return {"id": preview_id, "status": "starting"' in src
    # No join: returning would then be as slow as the thing it replaced.
    assert ".join(" not in src


def test_exactly_one_row_survives_an_attempt():
    """The substrate creates its own row. Two rows for one click is worse than none: the
    console would poll the id it was handed and watch it sit at 'starting' forever."""
    src = inspect.getsource(preview_service.start_preview_async)
    assert "DELETE FROM app_previews WHERE id=:sid" in src
    assert "UPDATE app_previews AS t SET status=s.status" in src


def test_a_crash_in_the_thread_is_recorded_not_swallowed():
    src = inspect.getsource(preview_service.start_preview_async)
    assert "start_crashed" in src and "ambiguous_app" in src and "not_ready" in src
    assert "app_preview.failed" in src


def test_each_substrate_attempt_is_a_step():
    """`auto` tries three substrates. "preview could not start" could not say whether one was
    tried or three."""
    src = inspect.getsource(preview_service.run_preview_attempts)
    assert "steps.step(name, label=label)" in src
    # A substrate that cannot serve is a SKIP with a reason — the next one may work — and only
    # a crash is a failure.
    assert "st.skip(" in src and "st.failed(" in src
    assert set(preview_service._SUBSTRATE_LABELS) == {
        "start_rootfs_preview", "start_container_preview", "start_preview"}


def test_the_step_ledger_is_the_one_deployments_already_use():
    """A second implementation would drift from the first the moment either was touched."""
    src = inspect.getsource(StepRecorder.__init__)
    assert 'table: str = "deployment_runs"' in src
    # Previews have neither updated_at nor duration_ms; only the ledger is shared.
    flush = inspect.getsource(StepRecorder._flush)
    assert 'self._table == "deployment_runs"' in flush


def test_progress_reporting_never_breaks_the_thing_it_reports_on():
    assert "except Exception:" in inspect.getsource(StepRecorder._flush)
