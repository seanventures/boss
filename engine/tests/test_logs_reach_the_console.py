"""Container logs belong in the console, not only on the box.

"Engineer implementing — 27m46s — no model call" was accurate and unhelpful: it said the model
was not the cost without saying what was. What it was — the sandbox's per-command durations,
how long a stage queued for a model lane, why a preview refused — was all being written to a
container log an operator cannot open.

The danger in fixing that is obvious: a logging handler that writes to Postgres on every record
is a good way to take down the thing it observes. So the rules matter as much as the feature.
"""

import inspect
import logging
import time

from boss_engine.services import log_capture
from boss_engine.services.activity_log import SOURCES, _LOADERS_BY_SOURCE
from boss_engine.services.activity_retention import PRUNABLE


def test_it_is_a_first_class_source_not_a_special_case():
    assert "app_log" in SOURCES
    assert set(SOURCES) == set(_LOADERS_BY_SOURCE), "source list and loader map disagree"


def test_volume_is_bounded_at_the_handler():
    """Everything else is dropped before any work is done — not filtered after the write."""
    handler = log_capture.ActivityLogHandler.__dict__["_keeps"]
    src = inspect.getsource(handler)
    assert "levelno >= logging.WARNING" in src
    assert "_INFO_ALLOWLIST" in src
    # INFO is kept only for the loggers whose lines ARE the diagnosis.
    assert "boss_engine.services.sandbox" in log_capture._INFO_ALLOWLIST
    assert "boss_engine.services.code_agent" in log_capture._INFO_ALLOWLIST


def test_it_never_writes_in_the_request_path():
    src = inspect.getsource(log_capture.ActivityLogHandler)
    assert "queue.Queue(maxsize=_MAX_QUEUE)" in src
    assert "daemon=True" in src
    # A slow database delays the logs, never the run.
    assert "put_nowait" in src


def test_a_full_queue_drops_the_oldest_and_counts_it():
    """A gap an operator can see beats silent truncation, and the newest lines are the ones
    describing what is happening now."""
    src = inspect.getsource(log_capture.ActivityLogHandler.emit)
    assert "except queue.Full:" in src
    assert "self._q.get_nowait()" in src
    assert "self._dropped += 1" in src


def test_logging_can_never_raise_or_recurse():
    emit = inspect.getsource(log_capture.ActivityLogHandler.emit)
    assert "except Exception:" in emit
    assert "_boss_activity_writer" in emit
    write = inspect.getsource(log_capture.ActivityLogHandler._write)
    assert "except Exception:" in write


def test_records_are_attributed_to_a_run_when_there_is_one():
    """Otherwise it is a wall of text. The LLM context already carries project and run."""
    src = inspect.getsource(log_capture.ActivityLogHandler.emit)
    assert "get_llm_context" in src
    assert '"run_id": ctx.get("run_id")' in src


def test_records_with_no_project_are_still_shown():
    """An error raised before any run was resolved belongs in the log precisely BECAUSE
    nothing owns it — excluding it would hide the failures that have no home."""
    from boss_engine.services import activity_log

    src = inspect.getsource(activity_log._load_app_logs)
    assert "project_id = :project_id OR project_id IS NULL" in src


def test_python_level_names_are_translated_to_this_logs_names():
    from boss_engine.services import activity_log

    src = inspect.getsource(activity_log._load_app_logs)
    assert '"warning": "warn"' in src and '"critical": "error"' in src


def test_the_log_is_aged_out_like_everything_else():
    """One row per interesting line across the API and every worker is the highest-volume
    table in the console. A log nobody ages out becomes the thing that fills the disk."""
    assert "app_logs" in PRUNABLE


def test_install_is_idempotent():
    src = inspect.getsource(log_capture.install)
    assert "if _handler is not None:" in src
    assert "return _handler" in src


def test_the_handler_survives_a_record_it_cannot_format():
    """A logging handler that raises breaks the caller, which is never worth a log line."""
    handler = log_capture.ActivityLogHandler.__new__(log_capture.ActivityLogHandler)
    logging.Handler.__init__(handler, level=logging.INFO)
    handler._q = None          # force an AttributeError inside emit
    handler._service = "test"
    record = logging.LogRecord("boss_engine.services.sandbox", logging.ERROR,
                               __file__, 1, "boom", None, None)
    handler.emit(record)       # must not raise


# ── Silence, made visible ──────────────────────────────────────────────────────

def test_quiet_time_is_reported_long_before_it_is_acted_on():
    """A run went silent for 27m46s — no model call, no turn, no event — and recovered on its
    own. So the 25-minute intervention was arguably right to hold off. What was NOT right is
    that the console said nothing for 27 minutes, and a screen that says nothing is
    indistinguishable from a dead system.
    """
    from boss_engine.services import shepherd_service as sh

    assert sh.QUIET_WARN_MINUTES == 5
    assert sh.QUIET_WARN_MINUTES < sh.NO_PROGRESS_MINUTES, (
        "reporting must come first; a warning that arrives with the intervention is not a "
        "warning"
    )


def test_reporting_does_not_change_when_the_shepherd_intervenes():
    """Observation only. Making silence visible must not make BOSS trigger-happy about it."""
    import inspect

    from boss_engine.services import shepherd_service as sh

    src = inspect.getsource(sh)
    # The decision still reads the same constant it always did.
    assert "return float(row['quiet_min'] or 0) > NO_PROGRESS_MINUTES" in src.replace('"', "'")
    # And the report is called before it, not instead of it.
    sweep = src[src.index("report_quiet(engine, blocker"):]
    assert "_made_no_progress(engine, blocker" in sweep[:400]


def test_the_warning_repeats_at_steps_not_on_every_sweep():
    """The sweep runs far more often than every five minutes. Without this a long stage would
    bury the log in identical rows, which is how an operator learns to ignore warnings."""
    import inspect

    from boss_engine.services.shepherd_service import report_quiet

    src = inspect.getsource(report_quiet)
    assert "int(minutes // QUIET_WARN_MINUTES) * QUIET_WARN_MINUTES" in src
    assert "(data->>'quiet_minutes')::int = :s" in src


def test_the_warning_says_what_it_might_be_and_when_boss_will_act():
    """"No activity" alone sends someone to a shell. Naming the likely causes and the
    intervention deadline is the difference between a warning and an alarm."""
    import inspect

    from boss_engine.services.shepherd_service import report_quiet

    src = inspect.getsource(report_quiet)
    assert "installing" in src and "running tests" in src and "stuck" in src
    assert "intervenes_at_minutes" in src


def test_install_raises_the_level_on_the_loggers_it_wants():
    """A handler on the root logger is NOT enough, and the failure is silent.

    `logger.info(...)` is discarded by the LOGGER before any handler is consulted when that
    logger's effective level is higher — and the default is WARNING. So the allowlisted INFO
    lines, which are the entire point of this, were never created: `app_logs` stayed empty and
    looked like a broken handler rather than a filtered one.
    """
    src = inspect.getsource(log_capture.install)
    assert "for name in _INFO_ALLOWLIST:" in src
    assert "setLevel(logging.INFO)" in src


def test_it_actually_captures_what_it_claims_to(monkeypatch):
    """The end-to-end check the level bug slipped past: allowlisted INFO kept, other INFO
    dropped, WARNING kept whatever its logger."""
    import boss_engine.db as db

    written: list[dict] = []

    class _Conn:
        def execute(self, _stmt, params=None):
            written.append(params or {})

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(db, "begin", lambda _engine: _Ctx())
    monkeypatch.setattr(log_capture, "_handler", None)

    handler = log_capture.install(object(), service="test")
    assert handler is not None
    try:
        logging.getLogger("boss_engine.services.sandbox").info("sandbox cmd 4200ms npm install")
        logging.getLogger("boss_engine.services.nowhere").info("chatter")
        logging.getLogger("anything.at.all").warning("a real warning")
        deadline = time.time() + 6
        while len(written) < 2 and time.time() < deadline:
            time.sleep(0.2)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    messages = [str(w.get("m")) for w in written]
    assert any("npm install" in m for m in messages), "the allowlisted INFO line was not captured"
    assert any("a real warning" in m for m in messages)
    assert not any("chatter" in m for m in messages), "un-allowlisted INFO leaked in"
