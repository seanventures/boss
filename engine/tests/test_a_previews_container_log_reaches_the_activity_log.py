"""The container log of an app BOSS builds must land where an operator reads logs.

`read_preview_log` could always fetch it — from a file on the preview host, or by shelling out
to `docker logs`. That makes it available to an agent holding a `prev_` id and to nobody else.
It never reached `app_logs`, so the Activity log could not filter it, search it, or show it
beside the workflow step that caused the build. An operator looking at a failed preview on that
screen saw BOSS's own records and none of the app's output.

These tests pin the copy-across: what gets written, at what level, and that it cannot take the
preview down with it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import preview_log_ingest
from boss_engine.services.preview_log_ingest import ingest_preview_log

PROJECT = "proj_preview_logs"
PREVIEW = "prev_abc123"
RUN = "crun_abc123"


@pytest.fixture()
def engine():
    """An in-memory database holding just the table the ingest writes to."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE app_logs ("
            " id TEXT PRIMARY KEY, project_id TEXT, run_id TEXT, service TEXT,"
            " level TEXT NOT NULL, logger TEXT NOT NULL, message TEXT NOT NULL,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
    return eng


def _snapshot(monkeypatch, payload: dict) -> None:
    """Stand in for the real snapshot, which reads a file on the preview host."""
    from boss_engine.services import preview_service

    monkeypatch.setattr(preview_service, "preview_log_snapshot",
                        lambda *args, **kwargs: payload)


def _rows(engine) -> list[dict]:
    with engine.begin() as conn:
        return [dict(row) for row in conn.execute(text(
            "SELECT project_id, run_id, service, level, logger, message "
            "FROM app_logs ORDER BY rowid"
        )).mappings()]


def test_the_log_body_becomes_rows_the_activity_log_can_read(engine, monkeypatch):
    _snapshot(monkeypatch, {
        "log": "vite v5 building\nbuilt in 412ms\nlistening on 3000\n",
        "error": "",
        "failure_kind": "",
    })

    written = ingest_preview_log(
        engine, PREVIEW, project_id=PROJECT, run_id=RUN, app_name="checkout")

    assert written == 3
    rows = _rows(engine)
    assert [row["message"] for row in rows] == [
        "vite v5 building", "built in 412ms", "listening on 3000"]
    # Attributed, or it cannot be found again: the project scopes the Activity query and the
    # run is what joins the line to the build that produced it.
    assert {row["project_id"] for row in rows} == {PROJECT}
    assert {row["run_id"] for row in rows} == {RUN}
    assert {row["logger"] for row in rows} == {"preview.container"}
    assert {row["service"] for row in rows} == {"checkout"}


def test_the_failing_line_is_an_error_so_the_level_filter_finds_it(engine, monkeypatch):
    """A failed build's log is mostly ordinary output with two lines that matter."""
    _snapshot(monkeypatch, {
        "log": "installing\nError: Could not resolve entry module index.html\ndone\n",
        "error": "",
        "failure_kind": "",
    })

    ingest_preview_log(engine, PREVIEW, project_id=PROJECT, run_id=RUN)

    by_message = {row["message"]: row["level"] for row in _rows(engine)}
    assert by_message["Error: Could not resolve entry module index.html"] == "error"
    assert by_message["installing"] == "info"


def test_a_preview_that_died_before_logging_still_records_why(engine, monkeypatch):
    """The common case for a crash, and the one where 'no log body is readable from here'
    would otherwise be the entire answer."""
    _snapshot(monkeypatch, {
        "log": "",
        "error": "app did not serve HTTP within timeout",
        "failure_kind": "port_timeout",
    })

    written = ingest_preview_log(engine, PREVIEW, project_id=PROJECT, run_id=RUN)

    assert written == 1
    row = _rows(engine)[0]
    assert row["level"] == "error"
    # The failure kind is the classification an operator scans for; keeping it on the line
    # means the message reads on its own in a table cell.
    assert row["message"] == "[port_timeout] app did not serve HTTP within timeout"


def test_nothing_to_say_writes_nothing(engine, monkeypatch):
    _snapshot(monkeypatch, {"log": "   \n\n", "error": "", "failure_kind": ""})

    assert ingest_preview_log(engine, PREVIEW, project_id=PROJECT, run_id=RUN) == 0
    assert _rows(engine) == []


def test_one_runaway_build_cannot_flood_the_screen(engine, monkeypatch):
    """Bounded on purpose. A log meant to be read must not be able to become ten thousand
    rows because one build looped."""
    _snapshot(monkeypatch, {
        "log": "\n".join(f"line {n}" for n in range(5000)),
        "error": "",
        "failure_kind": "",
    })

    written = ingest_preview_log(engine, PREVIEW, project_id=PROJECT, run_id=RUN)

    assert written == preview_log_ingest._MAX_LINES
    # The TAIL is what was kept — the end of a build is where the failure is.
    assert _rows(engine)[-1]["message"] == "line 4999"


def test_an_unreadable_preview_never_takes_the_preview_down(engine, monkeypatch):
    """This runs inside the preview's finishing thread, where an exception would lose the
    status update — which matters more than the log does."""
    from boss_engine.services import preview_service

    def _boom(*args, **kwargs):
        raise RuntimeError("preview host unreachable")

    monkeypatch.setattr(preview_service, "preview_log_snapshot", _boom)

    assert ingest_preview_log(engine, PREVIEW, project_id=PROJECT, run_id=RUN) == 0


def test_a_preview_with_no_app_name_is_still_attributable(engine, monkeypatch):
    """Falls back to the preview id rather than writing a nameless row: `service=` is how an
    operator narrows to one app, and an empty service would silently drop out of that filter."""
    _snapshot(monkeypatch, {"log": "hello\n", "error": "", "failure_kind": ""})

    ingest_preview_log(engine, PREVIEW, project_id=PROJECT, run_id=RUN)

    assert _rows(engine)[0]["service"] == f"preview:{PREVIEW}"
