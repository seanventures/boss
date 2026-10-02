"""A failed preview says what it ran, and a crash is labelled a crash.

The incident (boss-1, 2026-10-02): a Preview step's app died on an ImportError at import time.
The row said failure_kind='port_timeout' with "ensure HOST/PORT are honored" — the message was
"app did not serve HTTP within timeout … ImportError …" and the "did not serve" rule matched
first — and app_previews.command was empty, so nobody could say what had been run to reproduce
it. The agent asked about it went looking at ports and hosts.
"""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.services import preview_service as ps

SOURCE = Path(ps.__file__).read_text(encoding="utf-8")


def test_an_import_error_inside_a_timeout_message_is_a_crash():
    message = ("app did not serve HTTP within timeout. error: ImportError: cannot import name "
               "'create_app' from 'app' | log tail: Traceback (most recent call last): ...")
    kind, advice = ps._classify_start_failure(message)
    assert kind == "boot_crash"
    assert "HOST/PORT" not in advice


def test_a_process_that_exited_is_a_crash_whatever_the_words():
    assert ps._classify_start_failure("app did not serve HTTP within timeout.", exited=True)[0] \
        == "boot_crash"
    assert ps._classify_start_failure(
        "the app exited (code 1) before serving HTTP: log was empty.")[0] == "boot_crash"


def test_a_slow_app_that_never_bound_is_still_a_timeout():
    # The fix must not turn every timeout into a crash: no exception, still running.
    kind, _ = ps._classify_start_failure("app did not serve HTTP within timeout. log tail: "
                                         "compiling... compiled 412 modules", exited=False)
    assert kind == "port_timeout"


def test_the_other_kinds_are_unchanged():
    assert ps._classify_start_failure("git clone failed: auth")[0] == "clone_failed"
    assert ps._classify_start_failure("no page at the site root")[0] == "no_page_at_root"
    assert ps._classify_start_failure("could not detect a start command")[0] == "command_missing"


def _inserts() -> list[str]:
    """Every INSERT INTO app_previews in the service, as the SQL text it writes."""
    out = []
    for match in re.finditer(r"INSERT INTO app_previews", SOURCE):
        # The column list and VALUES follow within a few hundred characters.
        out.append(SOURCE[match.start():match.start() + 600])
    return out


def test_every_row_that_starts_or_fails_an_app_records_its_command():
    # A 'starting' placeholder (folded from its child) is the one row that knows no command yet.
    rows = _inserts()
    assert len(rows) >= 7, "the scan found too few inserts to be a real check"
    missing = []
    for sql in rows:
        values = sql[sql.index("VALUES"):sql.index("VALUES") + 200] if "VALUES" in sql else ""
        if "'starting'" in values:
            continue
        columns = sql[: sql.index("VALUES")] if "VALUES" in sql else sql
        if "command" not in columns:
            missing.append(sql[:160])
    assert missing == []


def test_the_host_launch_hands_the_command_and_the_exit_to_the_failure():
    assert "proc=proc)" in SOURCE[SOURCE.index("port = _discover_working_port(log_path, port_hint, timeout=180.0"):][:120]
    assert 'return _fail(why, command=" ".join(command), exited=exit_code is not None)' in SOURCE


def test_the_async_fold_keeps_the_command_and_kind_the_attempt_reported():
    assert "command=COALESCE(:cmd, command)" in SOURCE
    assert "failure_kind=COALESCE(:fk, failure_kind)" in SOURCE
