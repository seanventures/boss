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

from boss_engine.services import activity_sql
from boss_engine.services.activity_log import (
    INFRA_SOURCES,
    LEVELS,
    SOURCES,
    _level_for,
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


def test_every_source_is_one_sql_projection():
    """A stream is reachable by `source=` only if it has a projection — and every projection
    names the fields a filter or a facet reads, so no stream is silently unfilterable."""
    assert set(activity_sql.SOURCES) == set(SOURCES)
    for name, spec in activity_sql.SOURCES.items():
        for field in ("table", "ts", "level", "status", "kind", "actor", "raw"):
            assert getattr(spec, field), f"{name} has no {field}"


# ── Search ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["event", "agent_turn"])
def test_search_is_pushed_into_sql_where_an_index_exists(source):
    """Not a Python filter over rows already loaded: full-text, on the index."""
    sql = activity_sql.SOURCES[source].fts or ""
    assert "plainto_tsquery" in sql, "search is not reaching the database"
    assert "to_tsvector" in sql


@pytest.mark.parametrize("source", ["event", "agent_turn"])
def test_the_query_splits_identifiers_exactly_as_the_index_does(source):
    """`shepherd.proposed` is ONE token under the english config, so a search for "shepherd"
    matched nothing. `translate` splits it, in the query and the index identically (an
    expression index is only used for the expression it was built on), on BOTH sides."""
    sql = activity_sql.SOURCES[source].fts or ""
    assert "translate(" in sql
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


def test_the_call_source_does_not_pretend_to_search():
    """`llm_calls` has no free text worth indexing — a row is a model name and a duration. Its
    search is a plain-text match, confirmed against the row afterwards."""
    assert activity_sql.SOURCES["llm_call"].fts is None


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
    # High-volume TRANSCRIPT tables only. `app_logs` joined for the same reason `agent_turns`
    # is here — one row per interesting log line across the API and every worker — and for the
    # same reason `events` is not: neither is an audit trail another feature reads.
    assert set(PRUNABLE) == {"agent_turns", "app_logs"}


def test_retention_deletes_in_batches():
    """One unbounded DELETE over a month of transcripts holds a long lock on the table the
    console reads."""
    import pathlib

    source = pathlib.Path(
        "engine/boss_engine/services/activity_retention.py"
    ).read_text(encoding="utf-8")
    assert "LIMIT :batch" in source
