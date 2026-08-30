"""BOSS's own housekeeping must not consume the window an operator's work has to fit in.

This has now happened twice. The console fetches the N newest runs and drops internal ones, so
a burst of maintenance does not merely add noise -- it fills the window. The limit was raised
50 -> 200 in August for exactly this reason, and it happened again at 200: every one of the 200
newest runs was "Captain: QUALITY IMPROVE PASS", the Factory rendered an empty grid under a
heading reading "200 code tasks", and the operator reasonably concluded their apps were gone.

Raising the limit a third time only moves it, and the response is already 1.41 MB at 200. So
the filter belongs before the LIMIT.
"""

from boss_engine.services.code_task_service import (
    INTERNAL_RUN_PREFIXES,
    list_code_task_runs,
)


class RecordingConn:
    """Captures the SQL and params so the WHERE clause can be asserted without a database."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.sql = ""
        self.params = {}

    def execute(self, statement, params=None):
        self.sql = " ".join(str(statement).split())
        self.params = dict(params or {})
        outer = self

        class _R:
            def mappings(self):
                return self

            def all(self):
                return outer.rows

        return _R()


def test_the_filter_is_applied_before_the_limit_not_after():
    """The whole point. Filtering after LIMIT is what let housekeeping eat the window."""
    conn = RecordingConn()
    list_code_task_runs(conn, "proj", 200, exclude_internal=True)
    where = conn.sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "NOT LIKE" in where
    assert "LIMIT" in conn.sql.split("ORDER BY", 1)[1]     # limit comes after the filtering


def test_every_internal_prefix_the_console_knows_about_is_excluded():
    """The console and the query must agree about what "internal" means, or a run hidden in one
    place shows up in the other.

    The prefixes are named EXPLICITLY here rather than looped from the constant: iterating the
    same tuple the code uses passes just as happily when someone deletes an entry from it, which
    is exactly the drift this test exists to catch.
    """
    conn = RecordingConn()
    list_code_task_runs(conn, "proj", 50, exclude_internal=True)
    values = set(conn.params.values())
    assert "captain:%" in values
    assert "quality improve pass%" in values


def test_a_deployment_can_add_its_own_housekeeping_prefix(monkeypatch):
    """One operator's platform runs "<platform> profile check" against itself. That name is
    deployment-specific and does not belong in the OSS engine, so it arrives by configuration.
    """
    monkeypatch.setenv("BOSS_INTERNAL_RUN_PREFIXES", "acme profile check, nightly sweep")
    conn = RecordingConn()
    list_code_task_runs(conn, "proj", 50, exclude_internal=True)
    values = set(conn.params.values())
    assert "acme profile check%" in values
    assert "nightly sweep%" in values
    assert "captain:%" in values          # the defaults are not replaced


def test_the_match_is_case_insensitive_and_anchored_at_the_start():
    """Live titles are "Captain: QUALITY IMPROVE PASS ..." -- capitalised, and the words appear
    mid-title in legitimate runs too, so an unanchored match would hide real work."""
    conn = RecordingConn()
    list_code_task_runs(conn, "proj", 50, exclude_internal=True)
    assert "lower(coalesce(title, ''))" in conn.sql
    for value in conn.params.values():
        if isinstance(value, str) and value.endswith("%"):
            assert not value.startswith("%"), f"{value} would match mid-title"


def test_asking_for_everything_still_returns_everything():
    """The filter is opt-in. Other callers -- and anyone auditing what BOSS did to itself --
    must still be able to see the housekeeping."""
    conn = RecordingConn()
    list_code_task_runs(conn, "proj", 50)
    assert "NOT LIKE" not in conn.sql
    assert conn.params == {"p": "proj", "lim": 50}
