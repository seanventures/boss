"""The activity log, as something you can actually search and read.

Three things were missing before this, and each of them showed up as "the log is empty" rather
than as an error:

  * The transcript and the model calls were not sources at all, so the longest phase of every
    build — the part an operator is watching — contributed nothing to the log.
  * There was no LEVEL. `status` says what happened to a piece of work; it does not say how
    loudly a row should speak, and without that a transcript buries everything else.
  * Search was a Python scan over rows already loaded, capped at 5,000 per source. On a project
    with 46,774 events, "no results" could mean "not in the last 5,000".
"""

from __future__ import annotations

import pytest

from boss_engine.services.activity_log import (
    INFRA_SOURCES,
    LEVELS,
    SOURCES,
    _level_for,
    _load_agent_turns,
    _load_events,
    _load_llm_calls,
)


def test_the_conversation_and_the_calls_are_sources():
    """The whole point. Without these two the log described everything around a build except
    the build."""
    assert "agent_turn" in SOURCES
    assert "llm_call" in SOURCES


def test_infra_is_the_streams_about_boss_itself():
    """"Is BOSS healthy" and "is my project healthy" are different questions asked by different
    people, so they get different filters."""
    assert set(INFRA_SOURCES) == {"job", "deadletter", "receipt"}
    for source in INFRA_SOURCES:
        assert source in SOURCES


def test_the_four_levels_are_ordered_quietest_first():
    assert LEVELS == ("verbose", "info", "warn", "error")


@pytest.mark.parametrize("severity,status,expected", [
    ("error", "info", "error"),
    ("info", "failed", "error"),      # a failure is an error however it was classified
    ("warn", "info", "warn"),
    ("info", "stuck", "warn"),        # stuck work is not yet a failure, but it is not fine
    ("info", "ok", "info"),
    ("info", "waiting", "info"),
])
def test_level_follows_the_severity_the_classifier_already_produced(severity, status, expected):
    """Derived, not invented alongside it.

    Two independent notions of "how bad is this" drift apart, and then the level filter and the
    status filter disagree about the same row in front of the operator.
    """
    assert _level_for(severity, status) == expected


def test_every_loader_takes_the_same_arguments():
    """The dispatch loop passes `q` to all of them blindly. A loader with a different signature
    is a TypeError at request time, on a page that is supposed to be the debugging tool."""
    import inspect

    from boss_engine.services.activity_log import _LOADERS_BY_SOURCE

    for name, loader in _LOADERS_BY_SOURCE.items():
        params = inspect.signature(loader).parameters
        for required in ("since", "until", "cap", "q"):
            assert required in params, f"{name} cannot be called by the dispatcher: no {required}"


def test_source_filter_reaches_every_source_by_name():
    from boss_engine.services.activity_log import _LOADERS_BY_SOURCE

    assert set(_LOADERS_BY_SOURCE) == set(SOURCES)


# ── Search ─────────────────────────────────────────────────────────────────────

def _sql_of(loader) -> str:
    """The SQL a loader would run with a search term, captured without a database."""
    captured: dict[str, str] = {}

    class _Conn:
        def execute(self, statement, params=None):
            captured["sql"] = str(statement)
            raise _Stop()

    class _Stop(Exception):
        pass

    try:
        loader(_Conn(), "p", since=None, until=None, cap=10, q="needle")
    except Exception:
        pass
    return captured.get("sql", "")


@pytest.mark.parametrize("loader", [_load_events, _load_agent_turns])
def test_search_is_pushed_into_sql_where_an_index_exists(loader):
    """Not a Python filter over rows already loaded.

    The old path loaded up to 5,000 rows per source and filtered them in memory, so a search
    could report "nothing" for a project that had matches at row 5,001.
    """
    sql = _sql_of(loader)
    assert "plainto_tsquery" in sql, "search is not reaching the database"
    assert "to_tsvector" in sql


@pytest.mark.parametrize("loader", [_load_events, _load_agent_turns])
def test_the_query_splits_identifiers_exactly_as_the_index_does(loader):
    """`shepherd.proposed` is ONE token under the english config, so a search for "shepherd"
    matched nothing and the log reported no results for an event it was holding.

    `translate` splits it. It must appear in the query and the index identically — an
    expression index is only used for the expression it was built on, so a mismatch here is
    silently a sequential scan.
    """
    sql = _sql_of(loader)
    assert "translate(" in sql
    # BOTH SIDES. Splitting only the indexed document left the query asking for the compound
    # token "zzq-activity-unique-abc123", which by construction is not in an index that stores
    # its parts — so searching for the exact text of a row returned nothing.
    assert "translate(:q" in sql, "the search term is not split the way the index is"


def test_the_index_definition_matches_the_query():
    """Both halves of the same decision live in different files. This is the test that notices
    when one of them moves."""
    import pathlib

    schema = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
    for index in ("idx_events_fts", "idx_agent_turns_fts"):
        block = schema[schema.index(f"CREATE INDEX IF NOT EXISTS {index}"):]
        block = block[: block.index(";")]
        assert "translate(" in block, f"{index} does not split identifiers"
        assert "'._-'" in block


def test_the_call_loader_does_not_pretend_to_search():
    """`llm_calls` has no free text worth indexing — a row is a model name and a duration. It
    accepts `q` for signature compatibility and ignores it, and the Python filter still applies,
    so nothing silently escapes the search."""
    assert "plainto_tsquery" not in _sql_of(_load_llm_calls)


# ── Retention ──────────────────────────────────────────────────────────────────

def test_retention_never_drops_below_a_month(monkeypatch):
    """A retention setting that can be turned down to a day is one that will be, on the
    afternoon somebody is chasing disk space."""
    from boss_engine.services.activity_retention import MINIMUM_DAYS, retention_days

    monkeypatch.setenv("BOSS_ACTIVITY_RETENTION_DAYS", "1")
    assert retention_days() == MINIMUM_DAYS
    monkeypatch.setenv("BOSS_ACTIVITY_RETENTION_DAYS", "90")
    assert retention_days() == 90
    monkeypatch.setenv("BOSS_ACTIVITY_RETENTION_DAYS", "nonsense")
    assert retention_days() == MINIMUM_DAYS


def test_retention_does_not_touch_the_audit_stream():
    """`events` is the audit trail. Other features read it, and quietly ageing out a customer's
    audit history from a routine housekeeping job is not this module's decision to make."""
    from boss_engine.services.activity_retention import PRUNABLE

    assert "events" not in PRUNABLE
    assert PRUNABLE == ("agent_turns",)


def test_retention_deletes_in_batches():
    """One unbounded DELETE over a month of transcripts holds a long lock on the table the
    console reads."""
    import pathlib

    source = pathlib.Path(
        "engine/boss_engine/services/activity_retention.py"
    ).read_text(encoding="utf-8")
    assert "LIMIT :batch" in source
